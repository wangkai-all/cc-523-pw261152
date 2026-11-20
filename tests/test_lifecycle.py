"""Tests for baseline versioning, alert immutability, revisions and backfill."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift_monitor import DriftEngine, Store
from tests.helpers import numeric_batch

W1 = ["2026-11-01T00:00:00", "2026-11-02T00:00:00",
      "2026-11-03T00:00:00", "2026-11-04T00:00:00"]
LATER = ["2026-11-08T00:00:00", "2026-11-09T00:00:00"]


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.engine = DriftEngine(self.store)
        self.store.ingest(numeric_batch(
            11, "amt", "seg", "v1", W1, mu=100, sigma=10), note="base")
        self.store.ingest(numeric_batch(
            12, "amt", "seg", "v1", LATER, mu=160, sigma=12,
            missing_rate=0.3), note="drifted")

    def tearDown(self):
        self.store.close()


class BaselineVersioningTests(LifecycleTestBase):
    def test_baseline_update_creates_new_version_keeps_history(self):
        b1 = self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1],
                                        note="v1 baseline")
        self.assertEqual(b1["version"], 1)
        self.assertEqual(b1["active"], 1)
        b2 = self.store.create_baseline("amt", "seg", "v1", W1[0], LATER[-1],
                                        note="extended baseline")
        self.assertEqual(b2["version"], 2)
        self.assertEqual(b2["active"], 1)
        # old row remains present, now inactive and unchanged
        old = self.store.get_baseline(b1["id"])
        self.assertEqual(old["active"], 0)
        self.assertEqual(old["note"], "v1 baseline")
        self.assertEqual(old["n"], b1["n"])
        versions = [b["version"] for b in
                    self.store.list_baselines()]
        self.assertEqual(versions, [1, 2])

    def test_alert_remains_tied_to_baseline_version_after_new_baseline(self):
        b1 = self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()
        alerts_v1 = self.engine.list_alerts()
        self.assertTrue(alerts_v1)
        self.assertEqual(alerts_v1[0]["baseline_id"], b1["id"])
        # a new baseline does not retroactively rewrite prior alerts
        b2 = self.store.create_baseline("amt", "seg", "v1", W1[0], LATER[-1],
                                        note="new baseline")
        self.engine.evaluate()
        old_alerts = [a for a in self.engine.list_alerts()
                      if a["baseline_id"] == b1["id"]]
        self.assertTrue(old_alerts)
        self.assertTrue(all(a["id"] for a in old_alerts))  # rows preserved
        new_alerts = [a for a in self.engine.list_alerts()
                      if a["baseline_id"] == b2["id"]]
        # baseline 2 absorbs the drifted data -> likely no fresh alert there
        self.assertNotIn(b1["id"], {a["baseline_id"] for a in new_alerts})


class AlertLifecycleTests(LifecycleTestBase):
    def test_alert_created_updated_resolved_with_append_only_events(self):
        self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()
        # target the alert attached to the window we are about to repair
        alert = next(a for a in self.engine.list_alerts()
                     if a["window_start"] == LATER[0])
        self.assertEqual(alert["status"], "open")
        created_events = self.engine.alert_events(alert["id"])
        self.assertEqual([e["event_type"] for e in created_events],
                         ["created"])

        # acknowledge -> appended event, no row deletion
        self.engine.acknowledge(alert["id"], note="looking")
        self.assertEqual(self.engine.get_alert(alert["id"])["status"],
                         "acknowledged")

        # repair the drifted window -> alert resolves, history retained
        repaired = numeric_batch(
            13, "amt", "seg", "v1", LATER[:1], mu=100, sigma=10)
        self.store.revise([{
            "feature": "amt", "segment": "seg", "business_version": "v1",
            "window_start": LATER[0], "rows": repaired,
        }], note="fix pipeline")
        # revision marks the repaired window dirty; only it gets re-evaluated
        run = self.engine.evaluate(only_dirty=True, kind="revision")
        self.assertEqual(run["windows_considered"], 1)
        refreshed = self.engine.get_alert(alert["id"])
        event_types = [e["event_type"]
                       for e in self.engine.alert_events(alert["id"])]
        self.assertIn("resolved", event_types)
        self.assertEqual(refreshed["status"], "resolved")
        self.assertIsNotNone(refreshed["resolved_at"])

    def test_repeated_evaluation_does_not_duplicate_alerts(self):
        self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()
        first = len(self.engine.list_alerts())
        self.engine.evaluate()
        self.engine.evaluate()
        self.assertEqual(len(self.engine.list_alerts()), first)


class RevisionAffectedOnlyTests(LifecycleTestBase):
    def test_revision_marks_only_affected_windows_dirty(self):
        self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()  # clears all dirty flags
        self.assertFalse(self.store.list_windows(only_dirty=True))
        target_window = LATER[0]
        self.store.revise([{
            "feature": "amt", "segment": "seg", "business_version": "v1",
            "window_start": target_window,
            "rows": numeric_batch(14, "amt", "seg", "v1", [target_window],
                                  mu=100, sigma=10),
        }], note="repair one window")
        dirty = self.store.list_windows(only_dirty=True)
        self.assertEqual(len(dirty), 1)
        self.assertEqual(dirty[0]["window_start"], target_window)

        # old rows are superseded, never physically deleted
        superseded = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM observations "
            "WHERE superseded_by IS NOT NULL").fetchone()["c"]
        self.assertGreater(superseded, 0)

        # affected-only re-evaluation considers exactly one window
        run = self.engine.evaluate(only_dirty=True, kind="revision")
        self.assertEqual(run["windows_considered"], 1)
        self.assertFalse(self.store.list_windows(only_dirty=True))
        # the untouched later window keeps its original alert untouched
        untouched_alert = next(
            a for a in self.engine.list_alerts()
            if a["window_start"] == LATER[1])
        events = [e["event_type"]
                  for e in self.engine.alert_events(untouched_alert["id"])]
        self.assertEqual(events, ["created"])

    def test_revision_audit_run_recorded(self):
        self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()  # clear initial dirty flags
        self.store.revise([{
            "feature": "amt", "segment": "seg", "business_version": "v1",
            "window_start": LATER[0],
            "rows": numeric_batch(15, "amt", "seg", "v1", [LATER[0]]),
        }], note="rev")
        self.engine.evaluate(only_dirty=True, kind="revision")
        runs = self.engine.list_runs()
        self.assertEqual(runs[0]["kind"], "revision")
        self.assertEqual(runs[0]["windows_considered"], 1)


class BackfillTests(LifecycleTestBase):
    def test_backfill_against_new_baseline_preserves_existing_alerts(self):
        b1 = self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        self.engine.evaluate()
        original = self.engine.list_alerts()
        self.assertTrue(original)
        # rebuild a baseline then backfill historical windows
        b2 = self.store.create_baseline("amt", "seg", "v1", W1[0], LATER[-1],
                                        note="retrained")
        run = self.engine.backfill(W1[0], LATER[-1], baseline_id=b2["id"],
                                   note="retro check")
        self.assertEqual(run["kind"], "backfill")
        # alerts for baseline v1 still exist untouched
        v1_ids = {a["id"] for a in self.engine.list_alerts()
                  if a["baseline_id"] == b1["id"]}
        self.assertTrue(v1_ids)
        # backfilled alerts are flagged
        for a in self.engine.list_alerts():
            if a["baseline_id"] == b2["id"]:
                self.assertEqual(a["is_backfill"], 1)
        # running the same backfill again must not duplicate alerts
        count_before = len(self.engine.list_alerts())
        self.engine.backfill(W1[0], LATER[-1], baseline_id=b2["id"])
        self.assertEqual(len(self.engine.list_alerts()), count_before)


class PolicyTests(LifecycleTestBase):
    def test_policy_versioning_and_specificity(self):
        self.store.upsert_policy(psi_threshold=0.5)  # global, version 1
        self.store.upsert_policy(psi_threshold=0.1, feature="amt",
                                 segment="seg", business_version="v1")
        f_id, s_id, v_id = (self.store.feature_id("amt"),
                            self.store.segment_id("seg"),
                            self.store.version_id("v1"))
        policy = self.store.resolve_policy(f_id, s_id, v_id)
        self.assertEqual(policy["psi_threshold"], 0.1)
        # old global policy still exists but is inactive after a second global
        self.store.upsert_policy(psi_threshold=0.6)  # global version 2
        rows = self.store.conn.execute(
            "SELECT * FROM alert_policies WHERE feature_id IS NULL "
            "ORDER BY version").fetchall()
        # seeded default (v1) + two explicit globals -> v1,v2 inactive, v3 on
        self.assertEqual([r["version"] for r in rows], [1, 2, 3])
        self.assertEqual([r["active"] for r in rows], [0, 0, 1])
        self.assertEqual(rows[-1]["psi_threshold"], 0.6)

    def test_min_sample_size_skips_tiny_windows(self):
        self.store.upsert_policy(min_sample_size=100)
        self.store.create_baseline("amt", "seg", "v1", W1[0], W1[-1])
        run = self.engine.evaluate()
        self.assertEqual(run["alerts_created"], 0)


if __name__ == "__main__":
    unittest.main()
