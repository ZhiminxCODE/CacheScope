import unittest

from cachescope_runner.observations import assess_baselines, assess_observation


def residency(counts):
    # Eight one-page bins make the half-file and distant-quarter criteria exact.
    return {"resident_percent": 100 * sum(counts) / 8,
            "bins": [{"offset_bytes": i * 4096, "length_bytes": 4096,
                      "page_count": 1, "resident_pages": count} for i, count in enumerate(counts)]}


class ObservationTests(unittest.TestCase):
    def test_all_failed_baselines_have_measured_reasons(self):
        result = assess_baselines({"resident_percent": 2}, {"resident_percent": 3}, {"resident_percent": 4})
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("control started at 3.000% resident, above 1%", result["reason"])
        self.assertEqual(result["measurements"]["observer_before_percent"], 4)

    def test_threshold_inclusive_baselines_qualify(self):
        self.assertEqual(assess_baselines(*[{"resident_percent": 1}] * 3)["status"], "ready")

    def test_shared_file_success_and_individual_failures(self):
        warm = residency([1, 1, 1, 1, 0, 0, 0, 0])
        result = assess_observation("shared_file", warm, warm, 32768, 16384)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["measurements"]["observer_requested_region_percent"], 100)
        cold = residency([0] * 8)
        result = assess_observation("shared_file", warm, cold, 32768, 16384)
        self.assertIn("observer requested region reached 0.000%", result["reason"])
        all_warm = residency([1] * 8)
        result = assess_observation("shared_file", warm, all_warm, 32768, 16384)
        self.assertIn("observer distant region reached 100.000%", result["reason"])

    def test_control_requires_both_reader_warmth_and_observer_coldness(self):
        warm, cold = residency([1, 1, 1, 1, 0, 0, 0, 0]), residency([0] * 8)
        self.assertEqual(assess_observation("separate_inode_control", warm, cold, 32768, 16384)["status"], "observed")
        result = assess_observation("separate_inode_control", cold, warm, 32768, 16384)
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("reader requested region reached 0.000%", result["reason"])
        self.assertIn("control ended at 50.000%", result["reason"])
        self.assertEqual(result["measurements"]["control_after_percent"], 50)

    def test_missing_whole_bins_is_explained(self):
        sample = residency([0] * 8)
        result = assess_observation("shared_file", sample, sample, 32768, 100)
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("no whole bins", result["reason"])
