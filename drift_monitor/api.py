"""Dependency-free HTTP API + static dashboard server.

Run with::

    python -m drift_monitor.api --db drift.db --port 8080

The JSON API mirrors the Store/Engine methods; the dashboard is a static
vanilla-JS app served from ``drift_monitor/web``.
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import DriftEngine
from .store import Store

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _json_default(obj):
    return str(obj)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DriftMonitor/1.0"

    # ------------------------------------------------------------------ helpers
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def log_message(self, fmt, *args):  # quieter console
        pass

    @property
    def store(self) -> Store:
        return self.server.store

    @property
    def engine(self) -> DriftEngine:
        return self.server.engine

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        with self.store.lock:
            return self._handle_get()

    def _handle_get(self):
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path == "/api/health":
                return self._send_json({"status": "ok"})
            if path == "/api/dimensions":
                return self._send_json(self.store.list_dimensions())
            if path == "/api/windows":
                dirty = qs.get("dirty", ["0"])[0] == "1"
                return self._send_json(self.store.list_windows(dirty))
            if path == "/api/baselines":
                active = qs.get("active", ["0"])[0] == "1"
                return self._send_json(self.store.list_baselines(active))
            if path == "/api/policies":
                rows = [dict(r) for r in self.store.conn.execute(
                    "SELECT * FROM alert_policies ORDER BY id DESC")]
                return self._send_json(rows)
            if path == "/api/alerts":
                return self._send_json(self.engine.list_alerts(
                    status=qs.get("status", [None])[0],
                    feature=qs.get("feature", [None])[0],
                    segment=qs.get("segment", [None])[0],
                    business_version=qs.get("business_version", [None])[0]))
            if path.startswith("/api/alerts/") and path.endswith("/events"):
                alert_id = int(path.split("/")[3])
                return self._send_json(self.engine.alert_events(alert_id))
            if path.startswith("/api/alerts/"):
                alert_id = int(path.split("/")[3])
                alert = self.engine.get_alert(alert_id)
                return self._send_json(alert or {}, 200 if alert else 404)
            if path == "/api/runs":
                return self._send_json(self.engine.list_runs())
            if path == "/api/revisions":
                rows = [dict(r) for r in self.store.conn.execute(
                    "SELECT * FROM revisions ORDER BY id")]
                return self._send_json(rows)
            if path == "/api/distribution":
                required = ("feature", "segment", "business_version", "window")
                if any(k not in qs for k in required):
                    return self._send_error(400, f"required params: {required}")
                baseline_id = qs.get("baseline_id", [None])[0]
                payload = self.engine.distribution_compare(
                    qs["feature"][0], qs["segment"][0],
                    qs["business_version"][0], qs["window"][0],
                    int(baseline_id) if baseline_id else None)
                return self._send_json(payload)
            return self._serve_static(path)
        except KeyError as exc:
            return self._send_error(404, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface as JSON
            return self._send_error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        with self.store.lock:
            return self._handle_post()

    def _handle_post(self):
        try:
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/ingest":
                return self._send_json(self.store.ingest(
                    body["records"], body.get("note", "ingest")))
            if path == "/api/revise":
                return self._send_json(self.store.revise(
                    body["replacements"], body.get("note", "revision")))
            if path == "/api/baselines":
                baseline = self.store.create_baseline(
                    body["feature"], body["segment"],
                    body["business_version"], body["window_from"],
                    body["window_to"], body.get("note", ""))
                return self._send_json(baseline, 201)
            if path == "/api/policies":
                params = {k: v for k, v in body.items()
                          if k in ("psi_threshold", "js_threshold",
                                   "missing_rate_delta", "outlier_rate_delta",
                                   "mean_z_threshold", "min_sample_size",
                                   "notify_empty_window", "feature",
                                   "segment", "business_version")}
                return self._send_json(
                    self.store.upsert_policy(**params), 201)
            if path == "/api/evaluate":
                result = self.engine.evaluate(
                    only_dirty=bool(body.get("only_dirty", False)),
                    window_from=body.get("window_from"),
                    window_to=body.get("window_to"),
                    kind=body.get("kind", "scheduled"),
                    note=body.get("note", ""))
                return self._send_json(result)
            if path == "/api/backfill":
                result = self.engine.backfill(
                    body["window_from"], body["window_to"],
                    baseline_id=body.get("baseline_id"),
                    note=body.get("note", ""))
                return self._send_json(result)
            return self._send_error(404, f"no POST route for {path}")
        except KeyError as exc:
            return self._send_error(400, f"missing field: {exc}")
        except ValueError as exc:
            return self._send_error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._send_error(500, f"{type(exc).__name__}: {exc}")

    def do_PATCH(self):
        with self.store.lock:
            return self._handle_patch()

    def _handle_patch(self):
        try:
            path = urlparse(self.path).path
            body = self._body()
            if path.startswith("/api/alerts/"):
                alert_id = int(path.split("/")[3])
                action = body.get("action")
                if action == "acknowledge":
                    return self._send_json(
                        self.engine.acknowledge(alert_id, body.get("note", "")))
                if action == "suppress":
                    return self._send_json(
                        self.engine.suppress(alert_id, body.get("note", "")))
            return self._send_error(404, f"no PATCH route for {path}")
        except KeyError as exc:
            return self._send_error(404, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._send_error(500, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ static
    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        file_path = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not file_path.startswith(WEB_DIR) or not os.path.isfile(file_path):
            return self._send_error(404, "not found")
        ext = os.path.splitext(file_path)[1]
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        with open(file_path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(db_path, port=8080):
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    server.store = Store(db_path)
    server.engine = DriftEngine(server.store)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Feature-drift monitor server")
    parser.add_argument("--db", default=os.environ.get("DRIFT_DB", "drift.db"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args(argv)
    server = build_server(args.db, args.port)
    print(f"drift monitor listening on http://localhost:{args.port} "
          f"(db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.store.close()


if __name__ == "__main__":
    main()
