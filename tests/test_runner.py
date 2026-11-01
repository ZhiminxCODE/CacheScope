import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cachescope_runner.cli import (
    ExperimentError, cold_baseline, fio_command, fio_metrics, percentage, prepare,
    read_range, region_percent, run_fio, size_arg, summarize,
)


def payload(error=0):
    return {"jobs": [{"error": error, "read": {"bw_bytes": 104857600, "io_bytes": 1048576,
            "runtime": 10, "clat_ns": {"mean": 12500}, "lat_ns": {"mean": 16000}}}]}


class RunnerTests(unittest.TestCase):
    def test_size_and_threshold_validation(self):
        self.assertEqual(size_arg("256MiB"), 256 * 1024**2)
        self.assertEqual(size_arg("4097"), 4097)
        for invalid in ("-1", "1MB", "1.5GiB", "1MiBgarbage"):
            with self.assertRaises(argparse.ArgumentTypeError):
                size_arg(invalid)
        for invalid in ("nan", "inf", "-1", "101"):
            with self.assertRaises(argparse.ArgumentTypeError):
                percentage(invalid)

    def test_fio_units_and_identical_job_settings(self):
        metrics = fio_metrics(payload())
        self.assertEqual(metrics["bandwidth_mib_s"], 100)
        self.assertEqual(metrics["clat_mean_us"], 12.5)
        self.assertEqual(metrics["fio_elapsed_ms"], 10)
        buffered = fio_command("fio", Path("/data/a:b"), 1048576, 4096, False)
        direct = fio_command("fio", Path("/data/a:b"), 1048576, 4096, True)
        self.assertIn("--filename=/data/a\\:b", buffered)
        self.assertIn("--invalidate=0", buffered)
        self.assertIn("--readonly", buffered)
        self.assertEqual([s for s in buffered if not s.startswith("--direct=")],
                         [s for s in direct if not s.startswith("--direct=")])

    def test_legacy_fio_units_and_job_errors(self):
        data = payload()
        read = data["jobs"][0]["read"]
        del read["bw_bytes"]
        read["bw"] = 102400
        read["clat_us"] = read.pop("clat_ns")
        self.assertEqual(fio_metrics(data)["bandwidth_mib_s"], 100)
        self.assertEqual(fio_metrics(data)["clat_mean_us"], 12500)
        with self.assertRaises(ExperimentError):
            fio_metrics(payload(error=22))

    def test_eviction_is_measured_and_retried(self):
        with patch("cachescope_runner.cli.evict") as drop, \
             patch("cachescope_runner.cli.inspect", side_effect=[
                 {"resident_percent": 100}, {"resident_percent": 2}, {"resident_percent": 0}]), \
             patch("cachescope_runner.cli.time.sleep"):
            result, attempts, qualified = cold_baseline("binary", "file", 1, 3)
        self.assertTrue(qualified)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result["resident_percent"], 0)
        self.assertEqual(drop.call_count, 3)
        with patch("cachescope_runner.cli.evict"), \
             patch("cachescope_runner.cli.inspect", return_value={"resident_percent": 99}), \
             patch("cachescope_runner.cli.time.sleep"):
            self.assertFalse(cold_baseline("binary", "file", 1, 2)[2])

    def test_prepare_creates_real_independent_identical_files_and_preserves_existing(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            prepare(directory, 65537)
            a, b = Path(directory) / "sample.bin", Path(directory) / "control.bin"
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(a.stat().st_size, 65537)
            self.assertNotEqual(a.stat().st_ino, b.stat().st_ino)
            original = a.read_bytes()
            with self.assertRaises(FileExistsError):
                prepare(directory, 10)
            self.assertEqual(a.read_bytes(), original)
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "control.bin"
            control.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                prepare(directory, 10)
            self.assertEqual(control.read_bytes(), b"keep me")
            self.assertFalse((Path(directory) / "sample.bin").exists())

    def test_range_bounds_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data"
            path.write_bytes(b"abcdef")
            self.assertEqual(read_range(path, 2, 3)["length_bytes"], 3)
            for offset, length in ((0, 0), (-1, 2), (4, 3)):
                with self.assertRaises(ExperimentError):
                    read_range(path, offset, length)

    def test_median_excludes_unqualified_trials(self):
        runs = []
        for status, bw in (("ok", 10), ("ok", 30), ("inconclusive", 9999)):
            runs.append({"trial": "first_buffered", "status": status,
                         "before": {"resident_percent": 0}, "after": {"resident_percent": 100},
                         "bandwidth_mib_s": bw, "fio_elapsed_ms": 4, "wall_elapsed_ms": 20,
                         "clat_mean_us": 1, "lat_mean_us": 2})
        rows = summarize(runs)
        self.assertEqual(rows[0]["median_bandwidth_mib_s"], 20)
        self.assertEqual(rows[0]["valid_runs"], 2)
        self.assertIsNone(rows[2]["median_bandwidth_mib_s"])

    def test_partial_bins_are_not_guessed(self):
        snapshot = {"bins": [
            {"offset_bytes": 0, "length_bytes": 8192, "page_count": 2, "resident_pages": 1},
            {"offset_bytes": 8192, "length_bytes": 8192, "page_count": 2, "resident_pages": 2}]}
        self.assertEqual(region_percent(snapshot, 8192, 16384), 100)
        self.assertIsNone(region_percent(snapshot, 4096, 8192))

    def test_direct_unsupported_is_preserved_and_other_failures_are_errors(self):
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("cachescope_runner.cli.subprocess.run", return_value=
                       CompletedProcess([], 1, json.dumps(payload(error=22)), "invalid argument")):
                result = run_fio(["fio"], output, "direct", True)
                self.assertEqual(result["status"], "unsupported")
                self.assertEqual((output / "direct.fio.stderr.txt").read_text(), "invalid argument")
                self.assertEqual(run_fio(["fio"], output, "buffered", False)["status"], "error")
            with patch("cachescope_runner.cli.subprocess.run", return_value=
                       CompletedProcess([], 1, json.dumps(payload(error=13)), "permission denied")):
                self.assertEqual(run_fio(["fio"], output, "permission", True)["status"], "error")


if __name__ == "__main__":
    unittest.main()
