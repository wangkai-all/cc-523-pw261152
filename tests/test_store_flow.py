"""Integration tests: ingestion, baselines, empty segments, time switching."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift_monitor import DriftEngine, Store
from tests.helpers import categorical_batch, numeric_batch

WINDOWS_DAY1 = [f"2026-11-{d:02d}T00:00:00" for d in range(1, 8)]
WINDOWS_DAY2 = [f"2026-11-{d:02d}T00:00:00" for d in range(8, 15)]


class DriftFlowTestBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.engine = DriftEngine(self.store)

    def tearDown(self):
        self.store.close()

    def _seed_baseline_window(self, feature="order_amount", segment="tier_a",
                              version="v1", windows=None, **kw):
        windows = windows or WINDOWS_DAY1
        batch = numeric_batch(1, feature, segment, version, windows, **kw)
        self.store.ingest(batch, note="baseline data")
        return self.store.create_baseline(
            feature, segment, version, windows[0], windows[-1],
            note="initial baseline")


class IngestionTests(DriftFlowTestBase):
    def test_ingest_marks_windows_dirty_and_stats_empty_flag(self):
        self.store.ingest(numeric_batch(2, "f", "seg", "v1", WINDOWS_DAY1),
                          note="seed")
        windows = self.store.list_windows()
        self.assertEqual(len(windows), 7)
        self.assertTrue(all(w["dirty"] for w in windows))
        # recompute clears the dirty flag for exactly that window
        w0 = windows[0]
        stats = self.store.recompute_window(
            w0["feature_id"], w0["segment_id"],
            w0["business_version_id"], w0["window_start"])
        self.assertEqual(stats["n"], 40)
        self.assertFalse(stats["empty_window"])
        remaining = self.store.list_windows(only_dirty=True)
        self.assertEqual(len(remaining), 6)
        self.assertNotIn(w0["window_start"],
                         [w["window_start"] for w in remaining])

    def test_missing_rate_tracked(self):
        records = []
        for i in range(40):
            records.append({
                "feature": "f", "dtype": "numeric", "segment": "seg",
                "business_version": "v1", "window_start": WINDOWS_DAY1[0],
                "value": None if i < 10 else float(i),
                "is_missing": i < 10,
            })
        self.store.ingest(records)
        w = self.store.list_windows()[0]
        stats = self.store.recompute_window(
            w["feature_id"], w["segment_id"],
            w["business_version_id"], w["window_start"])
        self.assertAlmostEqual(stats["missing_rate"], 0.25, places=6)
        self.assertEqual(stats["n_missing"], 10)


class EmptySegmentTests(DriftFlowTestBase):
    def test_empty_segment_does_not_crash_and_flags_info_alert(self):
        # baseline exists for tier_a
        self._seed_baseline_window(segment="tier_a")
        # tier_b exists as a dimension but has NO observations -> empty window
        self.store.ensure_segment("tier_b", {"tier": "b"})
        s_b = self.store.segment_id("tier_b")
        # create a window state with no rows by revising nothing: simulate by
        # inserting then superseding all rows of that window
        self.store.ingest(numeric_batch(
            4, "order_amount", "tier_b", "v1", WINDOWS_DAY2[:1], n=6))
        self.store.revise([{
            "feature": "order_amount", "segment": "tier_b",
            "business_version": "v1", "window_start": WINDOWS_DAY2[0],
            "rows": [],
        }], note="wipe tier_b window")
        result = self.engine.evaluate()
        self.assertGreaterEqual(result["windows_empty"], 1)
        alerts = self.engine.list_alerts(segment="tier_b")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "info")
        self.assertTrue(alerts[0]["metrics_snapshot"]["empty_window"])
        # re-running evaluation must not duplicate the info alert
        self.engine.evaluate()
        self.assertEqual(len(self.engine.list_alerts(segment="tier_b")), 1)

    def test_completely_unknown_window_returns_empty_stats_on_demand(self):
        self.store.ensure_feature("lonely", "numeric")
        self.store.ensure_segment("void")
        self.store.ensure_business_version("v9")
        stats = self.store.recompute_window(
            self.store.feature_id("lonely"),
            self.store.segment_id("void"),
            self.store.version_id("v9"), "2026-11-20T00:00:00")
        self.assertTrue(stats["empty_window"])
        self.assertEqual(stats["n"], 0)


class TimeSwitchTests(DriftFlowTestBase):
    def test_switching_time_window_compares_against_same_baseline(self):
        baseline = self._seed_baseline_window()
        # two later windows with distinct distributions
        self.store.ingest(numeric_batch(
            5, "order_amount", "tier_a", "v1", WINDOWS_DAY2[:1],
            mu=100, sigma=10), note="stable window")
        self.store.ingest(numeric_batch(
            6, "order_amount", "tier_a", "v1", WINDOWS_DAY2[1:2],
            mu=180, sigma=10), note="shifted window")
        self.engine.evaluate()
        stable = self.engine.distribution_compare(
            "order_amount", "tier_a", "v1", WINDOWS_DAY2[0])
        shifted = self.engine.distribution_compare(
            "order_amount", "tier_a", "v1", WINDOWS_DAY2[1])
        self.assertEqual(stable["baseline"]["id"], baseline["id"])
        self.assertEqual(shifted["baseline"]["id"], baseline["id"])
        # JS divergence is bounded and stable even for small windows; PSI is
        # still reported but noisier, so assertions key off JS + mean shift
        self.assertLess(stable["drift"].get("js_divergence", 0), 0.1)
        self.assertGreater(shifted["drift"]["js_divergence"],
                           stable["drift"]["js_divergence"])
        self.assertGreater(shifted["drift"]["psi"], stable["drift"]["psi"])
        # time switch: only the shifted window raises an alert
        alerts = self.engine.list_alerts()
        windows = {a["window_start"] for a in alerts}
        self.assertIn(WINDOWS_DAY2[1], windows)
        self.assertNotIn(WINDOWS_DAY2[0], windows)


class CategoricalTests(DriftFlowTestBase):
    def test_categorical_drift_and_missing(self):
        base_windows = WINDOWS_DAY1
        self.store.ingest(categorical_batch(
            7, "channel", "tier_a", "v1", base_windows,
            {"web": 0.6, "app": 0.3, "mini": 0.1}), note="base")
        self.store.create_baseline(
            "channel", "tier_a", "v1", base_windows[0], base_windows[-1])
        self.store.ingest(categorical_batch(
            8, "channel", "tier_a", "v1", WINDOWS_DAY2[:1],
            {"web": 0.1, "app": 0.2, "mini": 0.7}), note="shift")
        result = self.engine.evaluate()
        self.assertGreaterEqual(result["alerts_created"], 1)
        alert = self.engine.list_alerts(feature="channel")[0]
        self.assertGreater(alert["metrics_snapshot"]["psi"], 0.25)


if __name__ == "__main__":
    unittest.main()
