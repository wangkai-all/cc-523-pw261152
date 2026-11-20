"""Unit tests for the pure statistical functions."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift_monitor import metrics


class QuantileTests(unittest.TestCase):
    def test_median_and_quartiles(self):
        vals = sorted(range(1, 101))  # 1..100
        self.assertEqual(metrics.quantile(vals, 0.5), 50.5)
        self.assertAlmostEqual(metrics.quantile(vals, 0.25), 25.75)
        self.assertAlmostEqual(metrics.quantile(vals, 0.75), 75.25)

    def test_single_value(self):
        self.assertEqual(metrics.quantile([7.0], 0.5), 7.0)

    def test_empty(self):
        self.assertIsNone(metrics.quantile([], 0.5))


class DistanceTests(unittest.TestCase):
    def test_psi_identical_distribution_is_zero(self):
        self.assertAlmostEqual(metrics.psi([10, 20, 30], [100, 200, 300]),
                               0.0, places=6)

    def test_psi_detects_shift(self):
        stable = metrics.psi([40, 35, 20, 5], [40, 35, 20, 5])
        shifted = metrics.psi([40, 35, 20, 5], [5, 20, 35, 40])
        self.assertAlmostEqual(stable, 0.0, places=6)
        self.assertGreater(shifted, 0.5)

    def test_js_bounded(self):
        js = metrics.jensen_shannon([1, 2, 3], [3, 2, 1])
        self.assertGreaterEqual(js, 0)
        self.assertLessEqual(js, math.log(2))

    def test_categorical_alignment_handles_new_labels(self):
        keys, exp, act = metrics.categorical_histograms(
            {"a": 5, "b": 5}, {"b": 5, "c": 5})
        self.assertEqual(keys, ["a", "b", "c"])
        self.assertEqual(exp, [5, 5, 0])
        self.assertEqual(act, [0, 5, 5])


class SummaryTests(unittest.TestCase):
    def test_numeric_summary_outliers(self):
        values = [10, 11, 10, 12, 9, 10, 11, 500]
        s = metrics.numeric_summary(values)
        self.assertEqual(s["n"], 8)
        self.assertGreaterEqual(s["n_outliers"], 1)
        self.assertEqual(len(s["histogram_counts"]), 10)

    def test_baseline_fence_reuse(self):
        base = metrics.numeric_summary(list(range(20)))
        current = metrics.numeric_summary(
            list(range(20)) + [999],
            outlier_fences=(base["lower_fence"], base["upper_fence"]))
        self.assertEqual(current["n_outliers"], 1)

    def test_categorical_summary(self):
        s = metrics.categorical_summary(["x", "y", "x", "z"])
        self.assertEqual(s["category_counts"], {"x": 2, "y": 1, "z": 1})

    def test_compare_numeric_detects_mean_shift(self):
        baseline = metrics.numeric_summary(
            [100 + (i % 5) for i in range(50)])
        current = metrics.numeric_summary(
            [130 + (i % 5) for i in range(50)])
        current["missing_rate"] = baseline["missing_rate"] = 0.0
        cmp = metrics.compare_numeric(baseline, current)
        self.assertGreater(abs(cmp["mean_z"]), 3.0)
        self.assertIn("psi", cmp)


if __name__ == "__main__":
    unittest.main()
