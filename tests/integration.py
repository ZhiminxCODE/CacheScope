#!/usr/bin/env python3
"""Real Linux syscall tests; cache-dependent checks skip only on failed eviction."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachescope_runner.cli import cold_baseline, read_range, region_percent
from ownership_fixture import change_to_mapped_owner

BIN = os.environ.get("CACHESCOPE_BIN", "cachescope")


@unittest.skipUnless(sys.platform == "linux", "Linux mmap/mincore required")
class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'file "cache" café.bin'
        self.page = os.sysconf("SC_PAGE_SIZE")

    def command(self, *args):
        return subprocess.run([BIN, *map(str, args)], capture_output=True, text=True, timeout=30)

    def snapshot(self, path=None, width=64):
        result = self.command("inspect", "--json", "--width", width, "--", path or self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_empty_one_byte_and_partial_page(self):
        for size in (0, 1, self.page + 1):
            self.path.write_bytes(b"x" * size)
            snapshot = self.snapshot()
            self.assertEqual(snapshot["file"], str(self.path))
            self.assertEqual(snapshot["size_bytes"], size)
            self.assertEqual(snapshot["total_pages"], (size + self.page - 1) // self.page)
            self.assertEqual(sum(b["length_bytes"] for b in snapshot["bins"]), size)
            self.assertEqual(sum(b["resident_pages"] for b in snapshot["bins"]), snapshot["resident_pages"])

    def test_errors_and_option_parsing(self):
        self.assertEqual(self.command("inspect", self.path).returncode, 1)
        self.assertEqual(self.command("inspect", self.directory.name).returncode, 1)
        self.path.write_bytes(b"x")
        for value in (0, -1, 4097, "3x", "", "+5", " 2"):
            self.assertEqual(self.command("inspect", "--width", value, self.path).returncode, 2)
        self.assertEqual(self.command("inspect", "--wat", self.path).returncode, 2)
        self.assertEqual(self.command("inspect", self.path, self.path).returncode, 2)
        fifo = Path(self.directory.name) / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(self.command("inspect", fifo).returncode, 1)

    def test_json_handles_arbitrary_path_bytes(self):
        for raw in (b"invalid-\xff\xfe", "astral-🦊".encode(), b"control-\t\n\\"):
            path = Path(self.directory.name) / os.fsdecode(raw)
            path.write_bytes(b"x")
            self.assertEqual(self.snapshot(path)["file"], str(path))

    def test_zoom_uses_absolute_offsets_and_counts_partial_final_page(self):
        self.path.write_bytes(b"x" * (self.page * 5 + 1))
        result = self.command("inspect", "--json", "--offset", self.page * 2,
                              "--length", self.page + 1, self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["size_bytes"], self.page * 5 + 1)
        self.assertEqual(data["offset_bytes"], self.page * 2)
        self.assertEqual(data["length_bytes"], self.page + 1)
        self.assertEqual(data["total_pages"], 2)
        self.assertEqual([b["start_page"] for b in data["bins"]], [2, 3])
        self.assertEqual([b["length_bytes"] for b in data["bins"]], [self.page, 1])
        self.assertEqual([b["offset_bytes"] for b in data["bins"]], [self.page * 2, self.page * 3])
        from cachescope_runner.snapshots import validate_snapshot
        validate_snapshot(data)

    def test_zoom_boundaries_units_and_invalid_ranges(self):
        self.path.write_bytes(b"x" * (2 * 1024**2))
        result = self.command("inspect", "--json", "--offset", "1MiB", "--length", "1KiB", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["length_bytes"], 1024)
        for flag, value in (("--length", "0"), ("--length", "1MB"), ("--offset", "-1"),
                            ("--offset", "18446744073709551616"), ("--length", "18446744073709551615GiB")):
            self.assertEqual(self.command("inspect", flag, value, self.path).returncode, 2)
        for flags in (("--offset", "1"), ("--offset", "3MiB"),
                      ("--offset", "1MiB", "--length", "2MiB")):
            self.assertEqual(self.command("inspect", *flags, self.path).returncode, 1)
        result = self.command("inspect", "--json", "--offset", "2MiB", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_pages"], 0)
        self.assertEqual(self.command("inspect", "--help").returncode, 0)

    def test_live_monitor_observes_a_reader_and_saves_a_valid_diff(self):
        from cachescope_runner.snapshots import compare_snapshots
        size = 32 * 1024**2
        with self.path.open("wb") as stream:
            for _ in range(32):
                stream.write(os.urandom(1024**2))
            stream.flush()
            os.fsync(stream.fileno())
        _, _, cold = cold_baseline(BIN, self.path, 1, 3)
        if not cold:
            self.skipTest("filesystem did not honor advisory eviction")
        saved = Path(self.directory.name) / "live"
        process = subprocess.Popen([sys.executable, "-m", "cachescope_runner", "watch", str(self.path),
                                    "--cachescope", BIN, "--jsonl", "--count", "2", "--interval", "1",
                                    "--save", str(saved)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0], "monitor did not emit baseline")
            first_line = process.stdout.readline()
            self.assertTrue(first_line, "monitor exited before its baseline")
            before = json.loads(first_line)["snapshot"]
            self.assertLessEqual(before["resident_percent"], 1)
            read_range(self.path, 0, size // 2)
            rest, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            event = json.loads(rest)
            self.assertGreater(event["net_resident_pages"], 0)
            self.assertGreaterEqual(region_percent(event["snapshot"], 0, size // 2), 90)
            left, right = [json.loads(p.read_text()) for p in sorted(saved.glob("snapshot-*.json"))]
            diff = compare_snapshots(left, right)
            self.assertIn("+", diff["delta_map"][:32])
            zoom = self.command("inspect", "--json", "--offset", "8MiB", "--length", "8MiB", self.path)
            self.assertEqual(zoom.returncode, 0, zoom.stderr)
            self.assertGreaterEqual(json.loads(zoom.stdout)["resident_percent"], 90)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_ownership_masking_is_not_silently_reported(self):
        self.path.write_bytes(b"x")
        self.path.chmod(0o644)
        uid, _gid = change_to_mapped_owner(self.path)
        self.assertEqual(self.path.stat().st_uid, uid)
        self.assertEqual(self.command("inspect", self.path).returncode, 1)
        result = self.command("inspect", "--json", "--allow-unverified", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["ownership_verified"])
        self.assertIn("WARNING", result.stderr)

    def test_inspection_does_not_warm_and_spatial_ranges_move(self):
        size = 64 * 1024**2
        with self.path.open("wb") as stream:
            block = os.urandom(1024**2)
            for _ in range(64):
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        before, _, cold = cold_baseline(BIN, self.path, 1, 3)
        if not cold:
            self.skipTest("filesystem did not honor advisory eviction")
        again = self.snapshot()
        self.assertLessEqual(again["resident_percent"], 1, "inspector populated file cache")
        read_range(self.path, 0, size // 2)
        first = self.snapshot()
        self.assertGreaterEqual(region_percent(first, 0, size // 2), 90)
        self.assertLessEqual(region_percent(first, size * 3 // 4, size), 10)
        _, _, cold = cold_baseline(BIN, self.path, 1, 3)
        if not cold:
            self.skipTest("filesystem did not honor advisory eviction for second range")
        read_range(self.path, size // 4, size // 2)
        middle = self.snapshot()
        self.assertGreaterEqual(region_percent(middle, size // 4, size * 3 // 4), 90)
        self.assertLessEqual(region_percent(middle, 0, size // 8), 10)
        self.assertLessEqual(region_percent(middle, size * 7 // 8, size), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
