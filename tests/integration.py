#!/usr/bin/env python3
"""Real Linux syscall tests; cache-dependent checks skip only on failed eviction."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cachescope_runner.cli import cold_baseline, read_range, region_percent

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

    def test_ownership_masking_is_not_silently_reported(self):
        if os.geteuid() != 0:
            self.skipTest("requires root to create a differently owned fixture")
        self.path.write_bytes(b"x")
        os.chown(self.path, 12345, 12345)
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
