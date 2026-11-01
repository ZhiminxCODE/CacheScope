import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cachescope_runner.cli import ExperimentError, main, parser
from cachescope_runner.snapshots import compare_snapshots, diff_command, validate_snapshot, watch


def snapshot(resident=(0, 0, 0, 0), pages_per_bin=4, offset=0, inode=10):
    page = 4096
    bins = [{"start_page": offset // page + i * pages_per_bin, "page_count": pages_per_bin,
             "offset_bytes": offset + i * pages_per_bin * page, "length_bytes": pages_per_bin * page,
             "resident_pages": count} for i, count in enumerate(resident)]
    total = pages_per_bin * len(resident)
    return {"schema_version": 1, "file": "/data/sample.bin", "size_bytes": offset + total * page,
            "offset_bytes": offset, "length_bytes": total * page, "page_size": page,
            "total_pages": total, "resident_pages": sum(resident),
            "resident_percent": 100 * sum(resident) / total if total else 0,
            "ownership_verified": True, "device": 1, "inode": inode,
            "map": "".join("." if not n else "#" if n == pages_per_bin else "+" for n in resident),
            "bins": bins}


class SnapshotTests(unittest.TestCase):
    def test_published_examples_replay_and_show_equal_percent_different_regions(self):
        root = Path(__file__).resolve().parent.parent / "examples"
        before, first, middle = [json.loads((root / name).read_text())
                                 for name in ("before.json", "after.json", "middle.json")]
        warmed = compare_snapshots(before, first)
        moved = compare_snapshots(first, middle)
        self.assertGreater(warmed["net_resident_pages"], 0)
        self.assertEqual(moved["net_resident_pages"], 0)
        self.assertEqual(first["resident_percent"], middle["resident_percent"])
        self.assertNotEqual(first["map"], middle["map"])
        self.assertGreater(moved["warmer_bins"], 0)
        self.assertGreater(moved["cooler_bins"], 0)

    def test_diff_detects_regions_even_when_total_residency_is_unchanged(self):
        result = compare_snapshots(snapshot((0, 4, 2, 0)), snapshot((4, 0, 2, 0)))
        self.assertEqual(result["net_resident_pages"], 0)
        self.assertEqual(result["delta_map"], "+-..")
        self.assertEqual(result["warmer_bins"], 1)
        self.assertEqual(result["cooler_bins"], 1)
        self.assertEqual(result["changes"][1]["offset_bytes"], 16384)
        self.assertEqual(result["changes"][1]["net_resident_pages"], -4)

    def test_diff_rejects_different_files_ranges_and_bin_layouts(self):
        before = snapshot()
        for after in (snapshot(inode=11), snapshot((0, 0), pages_per_bin=8),
                      snapshot((0, 0), offset=32768)):
            with self.assertRaises(ExperimentError):
                compare_snapshots(before, after)
        # Both snapshots are well formed but have different system page sizes.
        after = snapshot((0, 0), pages_per_bin=4)
        after["page_size"] *= 2
        for bin in after["bins"]:
            bin["offset_bytes"] *= 2
            bin["length_bytes"] *= 2
        after["size_bytes"] *= 2
        after["length_bytes"] *= 2
        with self.assertRaisesRegex(ExperimentError, "page_size"):
            compare_snapshots(before, after)

    def test_legacy_snapshots_and_empty_files_remain_compatible(self):
        before = snapshot()
        before.pop("offset_bytes")
        before.pop("length_bytes")
        self.assertEqual(compare_snapshots(before, snapshot())["delta_map"], "....")
        self.assertEqual(compare_snapshots(snapshot(()), snapshot(()))["changes"], [])

    def test_bad_input_is_rejected_before_comparison(self):
        invalid = [None, [], {}, {"schema_version": 99}]
        for key, value in (("resident_pages", 1), ("ownership_verified", False),
                           ("resident_percent", float("nan")), ("page_size", 0),
                           ("total_pages", True), ("map", "####"), ("offset_bytes", 1)):
            data = snapshot()
            data[key] = value
            invalid.append(data)
        data = snapshot()
        data["bins"][1]["offset_bytes"] = 0
        invalid.append(data)
        data = snapshot()
        data["bins"][0]["resident_pages"] = 5
        invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ExperimentError):
                validate_snapshot(data)

    def test_offline_diff_works_on_macos_and_json_output_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            before, after = Path(directory) / "before.json", Path(directory) / "after.json"
            before.write_text(json.dumps(snapshot()))
            after.write_text(json.dumps(snapshot((0, 4, 0, 0))))
            output = io.StringIO()
            with patch("cachescope_runner.cli.sys.platform", "darwin"), contextlib.redirect_stdout(output):
                self.assertEqual(main(["diff", str(before), str(after), "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["delta_map"], ".+..")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                diff_command(argparse.Namespace(before=before, after=after, json=False, limit=1))
            self.assertIn("[16,384, 32,768)", output.getvalue())
            self.assertIn("page turnover", output.getvalue())

    def test_watch_saves_replayable_snapshots_and_jsonl_without_ansi(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "live"
            args = parser().parse_args(["watch", "/data/sample.bin", "--count", "2", "--interval", "0.01",
                                        "--save", str(saved), "--jsonl"])
            output = io.StringIO()
            with patch("cachescope_runner.snapshots.owned_file", return_value=Path("/data/sample.bin")), \
                 patch("cachescope_runner.snapshots.binary_path", return_value="cachescope"), \
                 patch("cachescope_runner.snapshots.inspect", side_effect=[snapshot(), snapshot((4, 0, 0, 0))]), \
                 patch("cachescope_runner.snapshots.time.sleep") as sleep, \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(watch(args), 0)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(events), 2)
            self.assertIsNone(events[0]["net_resident_pages"])
            self.assertEqual(events[1]["delta_map"], "+...")
            self.assertEqual(sleep.call_count, 1)
            self.assertNotIn("\033", output.getvalue())
            self.assertEqual(len((saved / "timeline.jsonl").read_text().splitlines()), 2)
            before, after = [json.loads(p.read_text()) for p in sorted(saved.glob("snapshot-*.json"))]
            self.assertEqual(compare_snapshots(before, after)["net_resident_pages"], 4)

    def test_watch_starts_new_baseline_when_file_is_replaced(self):
        args = parser().parse_args(["watch", "/data/sample.bin", "--count", "2", "--plain"])
        output = io.StringIO()
        with patch("cachescope_runner.snapshots.owned_file", return_value=Path("/data/sample.bin")), \
             patch("cachescope_runner.snapshots.binary_path", return_value="cachescope"), \
             patch("cachescope_runner.snapshots.inspect", side_effect=[snapshot(), snapshot(inode=11)]), \
             patch("cachescope_runner.snapshots.time.sleep"), contextlib.redirect_stdout(output):
            watch(args)
        self.assertIn("different underlying files", output.getvalue())
        self.assertNotIn("\033", output.getvalue())

    def test_watch_interrupt_preserves_completed_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "live"
            with patch("cachescope_runner.cli.sys.platform", "linux"), \
                 patch("cachescope_runner.snapshots.owned_file", return_value=Path("/data/sample.bin")), \
                 patch("cachescope_runner.snapshots.binary_path", return_value="cachescope"), \
                 patch("cachescope_runner.snapshots.inspect", side_effect=[snapshot(), KeyboardInterrupt]), \
                 patch("cachescope_runner.snapshots.time.sleep"), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["watch", "/data/sample.bin", "--save", str(saved)]), 130)
            validate_snapshot(json.loads((saved / "snapshot-000001.json").read_text()))
            self.assertEqual(len((saved / "timeline.jsonl").read_text().splitlines()), 1)

    def test_watch_cli_rejects_invalid_intervals_counts_and_widths(self):
        for flag, value in (("--interval", "nan"), ("--interval", "0"), ("--interval", "-1"),
                            ("--count", "0"), ("--width", "4097")):
            with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as error:
                parser().parse_args(["watch", "file", flag, value])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
