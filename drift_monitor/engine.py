"""Drift evaluation engine.

Responsibilities:

* compare each window's materialised stats against its **active baseline**;
* open / update / resolve alerts while preserving their identity and appending
  every lifecycle transition to ``alert_events`` (history is never rewritten);
* restrict post-revision evaluation to the windows marked dirty;
* backfill historical windows against a freshly created baseline without
  duplicating alerts that already exist for that baseline.
"""
from __future__ import annotations

from . import metrics
from .store import Store, _dumps, _loads, _now


class DriftEngine:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ helpers
    def _window_metrics(self, feature, baseline, stats):
        """Compute drift metrics for one window vs one baseline."""
        current = dict(stats)
        rows = self.store._active_rows(
            stats["feature_id"], stats["segment_id"],
            stats["business_version_id"], stats["window_start"])
        if feature["dtype"] == "numeric":
            values = [r["value"] for r in rows
                      if not r["is_missing"] and r["value"] is not None]
            # rebuild the current histogram on baseline edges: a shared support
            # is what makes PSI / JS meaningful across versions
            if baseline.get("histogram_edges"):
                _, counts = metrics.histogram(
                    values, edges=baseline["histogram_edges"])
                current["histogram_counts"] = counts
            current["_raw_values"] = values
            return metrics.compare_numeric(baseline, current)
        current["category_counts"] = stats.get("category_counts") or {}
        return metrics.compare_categorical(baseline, current)

    @staticmethod
    def _violations(policy, m):
        """Return (reasons, severity) for a non-empty window."""
        reasons = []
        severity = "warning"
        if m.get("psi", 0.0) >= policy["psi_threshold"]:
            reasons.append(f"psi={m['psi']:.3f}>={policy['psi_threshold']}")
        js_limit = policy.get("js_threshold", 0.15)
        if m.get("js_divergence", 0.0) >= js_limit:
            reasons.append(
                f"js={m['js_divergence']:.3f}>={js_limit}")
        # rate degradations only matter in the worsening direction: a drop in
        # missingness / outlier rate is an improvement, not drift
        if m.get("missing_rate_delta", 0.0) >= policy["missing_rate_delta"]:
            reasons.append(
                f"missing_delta={m['missing_rate_delta']:+.3f}")
        if m.get("outlier_rate_delta", 0.0) >= policy["outlier_rate_delta"]:
            reasons.append(
                f"outlier_delta={m['outlier_rate_delta']:+.3f}")
            severity = "critical"
        if abs(m.get("mean_z", 0.0)) >= policy["mean_z_threshold"]:
            reasons.append(f"mean_z={m['mean_z']:+.2f}")
        # large PSI escalates to critical
        if m.get("psi", 0.0) >= policy["psi_threshold"] * 2:
            severity = "critical"
        return reasons, severity

    def _evidence(self, feature, stats, baseline, limit=10):
        """Pull a small, deterministic set of rows that explain the alert:
        missing rows, fence-breaching outliers, then a capped sample."""
        rows = self.store._active_rows(
            stats["feature_id"], stats["segment_id"],
            stats["business_version_id"], stats["window_start"])
        evidence = []
        lower = baseline.get("lower_fence")
        upper = baseline.get("upper_fence")
        for r in rows:
            reason = None
            if r["is_missing"]:
                reason = "missing"
            elif feature["dtype"] == "numeric" and r["value"] is not None:
                if lower is not None and (r["value"] < lower
                                          or r["value"] > upper):
                    reason = "outlier"
            if reason:
                evidence.append({
                    "observation_id": r["id"],
                    "value": r["value"],
                    "value_text": r["value_text"],
                    "is_missing": r["is_missing"],
                    "reason": reason,
                    "tags": _loads(r["tags"], {}),
                })
            if len(evidence) >= limit:
                break
        # if nothing flag-worthy, keep a capped ordinary sample
        if not evidence:
            for r in rows[:limit]:
                evidence.append({
                    "observation_id": r["id"],
                    "value": r["value"],
                    "value_text": r["value_text"],
                    "is_missing": r["is_missing"],
                    "reason": "sample",
                    "tags": _loads(r["tags"], {}),
                })
        return evidence

    # ------------------------------------------------------------------ alert transitions
    def _log_event(self, alert_id, event_type, detail):
        self.store.conn.execute(
            """INSERT INTO alert_events(alert_id, event_type, detail)
               VALUES(?,?,?)""",
            (alert_id, event_type, _dumps(detail)),
        )

    def _upsert_alert(self, *, key, baseline_id, policy_id, severity,
                      metrics_snapshot, evidence, is_backfill, empty=False):
        f_id, s_id, v_id, window = key
        existing = self.store.conn.execute(
            """SELECT * FROM alerts
               WHERE feature_id=? AND segment_id=? AND business_version_id=?
                 AND window_start=? AND baseline_id=?""",
            (f_id, s_id, v_id, window, baseline_id),
        ).fetchone()
        if existing is None:
            cur = self.store.conn.execute(
                """INSERT INTO alerts(feature_id, segment_id,
                       business_version_id, window_start, baseline_id,
                       policy_id, severity, status, first_seen_at,
                       last_seen_at, is_backfill, metrics_snapshot,
                       evidence_samples)
                   VALUES(?,?,?,?,?,?,?, 'open', ?, ?, ?, ?, ?)""",
                (f_id, s_id, v_id, window, baseline_id, policy_id, severity,
                 _now(), _now(), 1 if is_backfill else 0,
                 _dumps(metrics_snapshot), _dumps(evidence)),
            )
            self._log_event(cur.lastrowid,
                            "backfilled" if is_backfill else "created",
                            {"severity": severity, "empty_window": empty,
                             "metrics": metrics_snapshot})
            return "created"
        # identity already exists -> never rewrite history, only transition
        changes = {"last_seen_at": _now(),
                   "metrics_snapshot": _dumps(metrics_snapshot),
                   "evidence_samples": _dumps(evidence)}
        transition = None
        if existing["status"] == "resolved":
            changes["status"] = "open"
            changes["resolved_at"] = None
            transition = "reopened"
        if existing["severity"] != severity:
            changes["severity"] = severity
            self._log_event(existing["id"], "severity_changed",
                            {"from": existing["severity"], "to": severity,
                             "metrics": metrics_snapshot})
        if transition:
            self._log_event(existing["id"], transition,
                            {"metrics": metrics_snapshot})
        else:
            self._log_event(existing["id"], "updated",
                            {"metrics": metrics_snapshot})
        sets = ", ".join(f"{k}=?" for k in changes)
        self.store.conn.execute(
            f"UPDATE alerts SET {sets} WHERE id=?",
            (*changes.values(), existing["id"]))
        return transition or "updated"

    def _resolve_alert(self, alert_id, detail):
        row = self.store.conn.execute(
            "SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if row and row["status"] != "resolved":
            self.store.conn.execute(
                """UPDATE alerts SET status='resolved', resolved_at=?
                   WHERE id=?""", (_now(), alert_id))
            self._log_event(alert_id, "resolved", detail)
            return True
        return False

    # ------------------------------------------------------------------ main entry
    def evaluate(self, *, only_dirty=False, window_from=None, window_to=None,
                 baseline_id=None, is_backfill=False, kind="scheduled",
                 revision_id=None, note=""):
        """Evaluate windows. Returns a summary dict and persists an audit run."""
        windows = self.store.list_windows(only_dirty=only_dirty)
        if window_from or window_to:
            windows = [w for w in windows
                       if (not window_from or w["window_start"] >= window_from)
                       and (not window_to or w["window_start"] <= window_to)]
        counters = {"windows_considered": 0, "windows_empty": 0,
                    "alerts_created": 0, "alerts_updated": 0,
                    "alerts_resolved": 0}
        for w in windows:
            key = (w["feature_id"], w["segment_id"],
                   w["business_version_id"], w["window_start"])
            counters["windows_considered"] += 1
            feature = self.store.get_feature(w["feature_id"])
            baseline = (self.store.get_baseline(baseline_id)
                        if baseline_id else
                        self.store.active_baseline(*key[:3]))
            policy = self.store.resolve_policy(*key[:3])
            stats = self.store.recompute_window(
                *key, outlier_fences=(baseline.get("lower_fence"),
                                      baseline.get("upper_fence"))
                if baseline and feature["dtype"] == "numeric" else None)

            # ----- empty sub-population contract (independent of baseline)
            if stats.get("empty_window"):
                counters["windows_empty"] += 1
                if policy["notify_empty_window"]:
                    bid = baseline["id"] if baseline else None
                    existing_alerts = self.store.conn.execute(
                        """SELECT * FROM alerts
                           WHERE feature_id=? AND segment_id=?
                             AND business_version_id=? AND window_start=?
                             AND COALESCE(baseline_id,-1)=COALESCE(?,-1)""",
                        (*key, bid),
                    ).fetchall()
                    if not existing_alerts:  # no spam on repeated runs
                        result = self._upsert_alert(
                            key=key, baseline_id=bid,
                            policy_id=policy["id"], severity="info",
                            metrics_snapshot={"empty_window": True},
                            evidence=[], is_backfill=is_backfill, empty=True)
                        counters[self._bucket(result)] += 1
                continue

            if baseline is None:
                continue  # non-empty window but nothing to compare against
            existing_alerts = self.store.conn.execute(
                """SELECT * FROM alerts
                   WHERE feature_id=? AND segment_id=? AND business_version_id=?
                     AND window_start=? AND baseline_id=?""",
                (*key, baseline["id"]),
            ).fetchall()

            # ----- too small to judge: skip but do not auto-resolve
            if stats["n"] < policy["min_sample_size"]:
                continue

            m = self._window_metrics(feature, baseline, stats)
            reasons, severity = self._violations(policy, m)
            snapshot = {**m, "reasons": reasons,
                        "missing_rate": stats["missing_rate"],
                        "outlier_rate": stats["outlier_rate"]}
            if reasons:
                evidence = self._evidence(feature, stats, baseline)
                result = self._upsert_alert(
                    key=key, baseline_id=baseline["id"], policy_id=policy["id"],
                    severity=severity, metrics_snapshot=snapshot,
                    evidence=evidence, is_backfill=is_backfill)
                counters[self._bucket(result)] += 1
            elif existing_alerts:
                # window recovered -> close the lifecycle, keep the row + log
                for a in existing_alerts:
                    if self._resolve_alert(a["id"], {"metrics": m}):
                        counters["alerts_resolved"] += 1

        cur = self.store.conn.execute(
            """INSERT INTO evaluation_runs(kind, window_from, window_to,
                   windows_considered, windows_empty, alerts_created,
                   alerts_updated, alerts_resolved, revision_id, note)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (kind, window_from, window_to, counters["windows_considered"],
             counters["windows_empty"], counters["alerts_created"],
             counters["alerts_updated"], counters["alerts_resolved"],
             revision_id, note),
        )
        self.store.conn.commit()
        counters["run_id"] = cur.lastrowid
        counters["kind"] = kind
        return counters

    @staticmethod
    def _bucket(result):
        return {"created": "alerts_created",
                "updated": "alerts_updated",
                "reopened": "alerts_updated"}.get(result, "alerts_updated")

    def backfill(self, window_from, window_to, baseline_id=None, note=""):
        """Re-evaluate a historical range against a baseline; existing alerts
        for that baseline are preserved (unique constraint + transition logic)."""
        return self.evaluate(window_from=window_from, window_to=window_to,
                             baseline_id=baseline_id, is_backfill=True,
                             kind="backfill", note=note)

    # ------------------------------------------------------------------ lifecycle actions
    def acknowledge(self, alert_id, note=""):
        row = self.store.conn.execute(
            "SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise KeyError(alert_id)
        if row["status"] == "open":
            self.store.conn.execute(
                "UPDATE alerts SET status='acknowledged' WHERE id=?",
                (alert_id,))
            self._log_event(alert_id, "acknowledged", {"note": note})
            self.store.conn.commit()
        return self.get_alert(alert_id)

    def suppress(self, alert_id, note=""):
        self.store.conn.execute(
            "UPDATE alerts SET status='suppressed' WHERE id=?", (alert_id,))
        self._log_event(alert_id, "suppressed", {"note": note})
        self.store.conn.commit()
        return self.get_alert(alert_id)

    # ------------------------------------------------------------------ queries
    def list_alerts(self, status=None, feature=None, segment=None,
                    business_version=None):
        sql = """SELECT a.*, f.name AS feature, f.dtype, s.name AS segment,
                        bv.name AS business_version,
                        b.version AS baseline_version
                 FROM alerts a
                 JOIN features f ON f.id=a.feature_id
                 JOIN segments s ON s.id=a.segment_id
                 JOIN business_versions bv ON bv.id=a.business_version_id
                 LEFT JOIN baselines b ON b.id=a.baseline_id WHERE 1=1"""
        params = []
        if status:
            sql += " AND a.status=?"
            params.append(status)
        if feature:
            sql += " AND f.name=?"
            params.append(feature)
        if segment:
            sql += " AND s.name=?"
            params.append(segment)
        if business_version:
            sql += " AND bv.name=?"
            params.append(business_version)
        sql += " ORDER BY a.last_seen_at DESC, a.id DESC"
        out = []
        for r in self.store.conn.execute(sql, params):
            d = dict(r)
            d["metrics_snapshot"] = _loads(d["metrics_snapshot"], {})
            d["evidence_samples"] = _loads(d["evidence_samples"], [])
            out.append(d)
        return out

    def get_alert(self, alert_id):
        rows = self.list_alerts()
        for r in rows:
            if r["id"] == alert_id:
                r["events"] = self.alert_events(alert_id)
                return r
        return None

    def alert_events(self, alert_id):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM alert_events WHERE alert_id=? ORDER BY id",
            (alert_id,))]

    def list_runs(self):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM evaluation_runs ORDER BY id DESC")]

    def distribution_compare(self, feature, segment, business_version,
                             window, baseline_id=None):
        """Full side-by-side distribution payload for the UI."""
        f_id = self.store.feature_id(feature)
        s_id = self.store.segment_id(segment)
        v_id = self.store.version_id(business_version)
        baseline = (self.store.get_baseline(baseline_id)
                    if baseline_id
                    else self.store.active_baseline(f_id, s_id, v_id))
        stats = self.store.get_window_stats(f_id, s_id, v_id, window)
        if stats is None:  # compute on demand
            stats = self.store.recompute_window(f_id, s_id, v_id, window)
        feature_row = self.store.get_feature(f_id)
        # rebuild the current histogram on the baseline edges so the chart
        # overlays two series on a shared support (never two different binnings)
        if baseline and feature_row["dtype"] == "numeric" \
                and baseline.get("histogram_edges") \
                and not stats.get("empty_window"):
            rows = self.store._active_rows(f_id, s_id, v_id, window)
            values = [r["value"] for r in rows
                      if not r["is_missing"] and r["value"] is not None]
            _, aligned = metrics.histogram(
                values, edges=baseline["histogram_edges"])
            stats = dict(stats)
            stats["histogram_edges"] = baseline["histogram_edges"]
            stats["histogram_counts"] = aligned
        payload = {"feature": feature, "dtype": feature_row["dtype"],
                   "segment": segment, "business_version": business_version,
                   "window_start": window, "baseline": baseline,
                   "current": stats}
        if baseline and not stats.get("empty_window"):
            payload["drift"] = self._window_metrics(feature_row, baseline, stats)
        return payload
