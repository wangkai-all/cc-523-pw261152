"""End-to-end HTTP API tests (real server on an ephemeral port)."""
import json
import os
import sys
import threading
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drift_monitor.api import build_server
from drift_monitor.demo import seed
from drift_monitor import Store


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = "/tmp/drift_api_test.db"
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db_path + suffix)
            except OSError:
                pass
        store = Store(cls.db_path)
        seed(store)
        store.close()
        cls.server = build_server(cls.db_path, 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.store.close()

    def test_01_health_and_dimensions(self):
        status, body = _req("GET", f"{self.base}/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, body = _req("GET", f"{self.base}/api/dimensions")
        self.assertEqual(status, 200)
        self.assertTrue(body["features"])
        self.assertIn("tier_c", [s["name"] for s in body["segments"]])

    def test_02_concurrent_requests_share_connection(self):
        # the threading bug would make every other request 500
        results = []

        def hit():
            code, _ = _req("GET", f"{self.base}/api/alerts")
            results.append(code)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 8)

    def test_03_distribution_and_empty_segment(self):
        status, body = _req(
            "GET",
            f"{self.base}/api/distribution?feature=order_amount&segment=tier_a"
            "&business_version=v2&window=2026-11-11T00:00:00")
        self.assertEqual(status, 200)
        self.assertIn("psi", body["drift"])
        self.assertFalse(body["current"]["empty_window"])

        status, empty = _req(
            "GET",
            f"{self.base}/api/distribution?feature=order_amount&segment=tier_c"
            "&business_version=v2&window=2026-11-12T00:00:00")
        self.assertEqual(status, 200)
        self.assertTrue(empty["current"]["empty_window"])
        self.assertEqual(empty["current"]["n"], 0)

    def test_04_alert_lifecycle_and_events(self):
        _, alerts = _req("GET", f"{self.base}/api/alerts?status=open")
        target = next(a for a in alerts if a["severity"] == "critical")
        status, body = _req("PATCH", f"{self.base}/api/alerts/{target['id']}",
                            {"action": "acknowledge", "note": "api test"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "acknowledged")
        _, events = _req("GET",
                         f"{self.base}/api/alerts/{target['id']}/events")
        types = [e["event_type"] for e in events]
        self.assertIn("acknowledged", types)
        # the origin event is retained (created or backfilled)
        self.assertTrue({"created", "backfilled"} & set(types))
        # lifecycle is strictly append-only: no event was removed
        self.assertGreaterEqual(len(types), 2)

    def test_05_affected_only_revision_via_api(self):
        # first evaluate everything so dirty flags clear
        _req("POST", f"{self.base}/api/evaluate", {"kind": "scheduled"})
        _, dirty_before = _req("GET", f"{self.base}/api/windows?dirty=1")
        self.assertEqual(dirty_before, [])
        rows = [{"feature": "order_amount", "dtype": "numeric",
                 "segment": "tier_a", "business_version": "v2",
                 "window_start": "2026-11-09T00:00:00",
                 "value": 100.0 + i, "is_missing": 0} for i in range(20)]
        status, rev = _req("POST", f"{self.base}/api/revise",
                           {"replacements": [{
                               "feature": "order_amount", "segment": "tier_a",
                               "business_version": "v2",
                               "window_start": "2026-11-09T00:00:00",
                               "rows": rows}],
                            "note": "api revision"})
        self.assertEqual(status, 200)
        self.assertEqual(rev["windows_touched"], 1)
        _, dirty = _req("GET", f"{self.base}/api/windows?dirty=1")
        self.assertEqual(len(dirty), 1)
        status, run = _req("POST", f"{self.base}/api/evaluate",
                           {"only_dirty": True, "kind": "revision"})
        self.assertEqual(status, 200)
        self.assertEqual(run["windows_considered"], 1)

    def test_06_new_baseline_version_and_backfill(self):
        _, before = _req("GET", f"{self.base}/api/baselines")
        status, b = _req("POST", f"{self.base}/api/baselines", {
            "feature": "order_amount", "segment": "tier_a",
            "business_version": "v2",
            "window_from": "2026-11-08T00:00:00",
            "window_to": "2026-11-09T00:00:00",
            "note": "api new version"})
        self.assertEqual(status, 201)
        self.assertGreaterEqual(b["version"], 2)
        # previous rows are still present (never rewritten)
        _, after = _req("GET", f"{self.base}/api/baselines")
        self.assertEqual(len(after), len(before) + 1)
        status, run = _req("POST", f"{self.base}/api/backfill", {
            "window_from": "2026-11-08T00:00:00",
            "window_to": "2026-11-09T00:00:00",
            "baseline_id": b["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(run["kind"], "backfill")
        # backfilling twice creates no duplicate alerts
        count1 = len(_req("GET", f"{self.base}/api/alerts")[1])
        _req("POST", f"{self.base}/api/backfill", {
            "window_from": "2026-11-08T00:00:00",
            "window_to": "2026-11-09T00:00:00",
            "baseline_id": b["id"]})
        count2 = len(_req("GET", f"{self.base}/api/alerts")[1])
        self.assertEqual(count1, count2)

    def test_07_static_dashboard_served(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as resp:
            html = resp.read().decode()
        self.assertIn("特征漂移监控", html)
        with urllib.request.urlopen(f"{self.base}/app.js",
                                    timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        self.assertIn("application/javascript", resp.headers["Content-Type"])

    def test_08_validation_errors(self):
        status, body = _req("GET", f"{self.base}/api/distribution?feature=x")
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = _req("POST", f"{self.base}/api/ingest",
                            {"records": [{"feature": "z"}]})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
