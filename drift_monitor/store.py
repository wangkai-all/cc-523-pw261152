"""Persistence + domain logic for the drift monitor.

Everything the HTTP layer and the CLI need lives here. The class is deliberately
framework-agnostic: it only knows SQLite and the pure functions in ``metrics``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from . import metrics
from .schema import apply_schema


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(text, default):
    if text is None or text == "":
        return default
    return json.loads(text)


class Store:
    # ------------------------------------------------------------------ setup
    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False + an RLock serialising every request lets the
        # threaded HTTP server share one connection safely
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        apply_schema(self.conn)
        self._ensure_default_policy()

    def _ensure_default_policy(self):
        """Seed one global policy so every alert can reference a real row;
        users can still override it with a scoped (versioned) policy."""
        row = self.conn.execute(
            "SELECT id FROM alert_policies LIMIT 1").fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO alert_policies(feature_id, segment_id,
                       business_version_id, version, psi_threshold,
                       js_threshold, missing_rate_delta,
                       outlier_rate_delta, mean_z_threshold, min_sample_size,
                       notify_empty_window, active)
                   VALUES(NULL,NULL,NULL,1,1.0,0.15,0.1,0.1,3.0,5,1,1)""")
            self.conn.commit()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ dimensions
    def ensure_feature(self, name, dtype):
        assert dtype in ("numeric", "categorical")
        row = self.conn.execute(
            "SELECT id FROM features WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO features(name, dtype) VALUES(?,?)", (name, dtype)
        )
        return cur.lastrowid

    def ensure_segment(self, name, definition=None):
        row = self.conn.execute(
            "SELECT id FROM segments WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO segments(name, definition) VALUES(?,?)",
            (name, _dumps(definition or {})),
        )
        return cur.lastrowid

    def ensure_business_version(self, name):
        row = self.conn.execute(
            "SELECT id FROM business_versions WHERE name=?", (name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO business_versions(name) VALUES(?)", (name,)
        )
        return cur.lastrowid

    def _create_revision(self, note):
        cur = self.conn.execute(
            "INSERT INTO revisions(note) VALUES(?)", (note,)
        )
        return cur.lastrowid

    # ------------------------------------------------------------------ lookups
    def _id(self, table, name):
        row = self.conn.execute(
            f"SELECT id FROM {table} WHERE name=?", (name,)
        ).fetchone()
        if not row:
            raise KeyError(f"{table} {name!r} does not exist")
        return row["id"]

    def feature_id(self, name):
        return self._id("features", name)

    def segment_id(self, name):
        return self._id("segments", name)

    def version_id(self, name):
        return self._id("business_versions", name)

    def get_feature(self, feature_id):
        return dict(self.conn.execute(
            "SELECT * FROM features WHERE id=?", (feature_id,)
        ).fetchone())

    def list_dimensions(self):
        return {
            "features": [dict(r) for r in self.conn.execute(
                "SELECT * FROM features ORDER BY name")],
            "segments": [dict(r) for r in self.conn.execute(
                "SELECT id,name,definition FROM segments ORDER BY name")],
            "business_versions": [dict(r) for r in self.conn.execute(
                "SELECT * FROM business_versions ORDER BY name")],
            "revisions": [dict(r) for r in self.conn.execute(
                "SELECT * FROM revisions ORDER BY id")],
        }

    # ------------------------------------------------------------------ ingestion
    def ingest(self, records, note: str = "ingest"):
        """Insert a batch of observation dicts under a fresh revision.

        Record keys: feature, dtype, segment, business_version, window_start,
        value (numeric or None), value_text (categorical), is_missing, tags.
        """
        revision_id = self._create_revision(note)
        touched = set()
        for rec in records:
            f_id = self.ensure_feature(rec["feature"], rec["dtype"])
            s_id = self.ensure_segment(rec.get("segment", "all"),
                                       rec.get("segment_definition"))
            v_id = self.ensure_business_version(rec["business_version"])
            is_missing = 1 if rec.get("is_missing") or (
                rec.get("value") is None and not rec.get("value_text")) else 0
            self.conn.execute(
                """INSERT INTO observations(feature_id, segment_id,
                       business_version_id, window_start, value, value_text,
                       is_missing, tags, revision_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (f_id, s_id, v_id, rec["window_start"], rec.get("value"),
                 rec.get("value_text"), is_missing,
                 _dumps(rec.get("tags") or {}), revision_id),
            )
            touched.add((f_id, s_id, v_id, rec["window_start"]))
        self._upsert_window_states(touched, revision_id, dirty=1)
        self.conn.commit()
        return {"revision_id": revision_id, "windows_touched": len(touched)}

    def revise(self, replacements, note: str = "data revision"):
        """Replace observations in affected windows with corrected records.

        Old rows are *superseded* (never deleted), so the lineage stays intact.
        Only windows referenced by ``replacements`` are marked dirty, which is
        what scopes the subsequent re-evaluation.
        """
        revision_id = self._create_revision(note)
        touched = set()
        for rec in replacements:
            f_id = self.feature_id(rec["feature"])
            s_id = self.segment_id(rec.get("segment", "all"))
            v_id = self.version_id(rec["business_version"])
            window = rec["window_start"]
            # 1) supersede every currently-active row of this exact window
            self.conn.execute(
                """UPDATE observations SET superseded_by=?
                   WHERE feature_id=? AND segment_id=? AND business_version_id=?
                     AND window_start=? AND superseded_by IS NULL""",
                (revision_id, f_id, s_id, v_id, window),
            )
            # 2) insert corrected rows under the new revision
            for row in rec["rows"]:
                is_missing = 1 if row.get("is_missing") or (
                    row.get("value") is None and not row.get("value_text")) else 0
                self.conn.execute(
                    """INSERT INTO observations(feature_id, segment_id,
                           business_version_id, window_start, value, value_text,
                           is_missing, tags, revision_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (f_id, s_id, v_id, window, row.get("value"),
                     row.get("value_text"), is_missing,
                     _dumps(row.get("tags") or {}), revision_id),
                )
            touched.add((f_id, s_id, v_id, window))
        self._upsert_window_states(touched, revision_id, dirty=1)
        self.conn.commit()
        return {"revision_id": revision_id, "windows_touched": len(touched)}

    def _upsert_window_states(self, touched, revision_id, dirty):
        for f_id, s_id, v_id, window in touched:
            self.conn.execute(
                """INSERT INTO window_states(feature_id, segment_id,
                       business_version_id, window_start, n, empty_window,
                       dirty, last_revision_id, stats_revision_id, updated_at)
                   VALUES(?,?,?,?,0,1,?,?,NULL,?)
                   ON CONFLICT(feature_id, segment_id, business_version_id,
                       window_start) DO UPDATE SET
                       dirty=excluded.dirty,
                       last_revision_id=excluded.last_revision_id,
                       updated_at=excluded.updated_at""",
                (f_id, s_id, v_id, window, dirty, revision_id, _now()),
            )

    # ------------------------------------------------------------------ window stats
    def _active_rows(self, f_id, s_id, v_id, window):
        return self.conn.execute(
            """SELECT * FROM observations
               WHERE feature_id=? AND segment_id=? AND business_version_id=?
                 AND window_start=? AND superseded_by IS NULL
               ORDER BY id""",
            (f_id, s_id, v_id, window),
        ).fetchall()

    def recompute_window(self, f_id, s_id, v_id, window, outlier_fences=None):
        """Rebuild one window's stats from its active observations and persist.

        Returns the stored stats dict (with ``empty_window`` flag). A window
        whose segment has no active rows is recorded as empty rather than
        crashing — that is the 'empty sub-population' contract.
        """
        feature = self.get_feature(f_id)
        rows = self._active_rows(f_id, s_id, v_id, window)
        n = len(rows)
        n_missing = sum(r["is_missing"] for r in rows)
        state = self.conn.execute(
            """SELECT * FROM window_states WHERE feature_id=? AND segment_id=?
               AND business_version_id=? AND window_start=?""",
            (f_id, s_id, v_id, window),
        ).fetchone()
        rev_id = state["last_revision_id"] if state else None

        # make sure a state row exists even for a window that has never held
        # data (the 'empty sub-population' case)
        if state is None:
            self.conn.execute(
                """INSERT OR IGNORE INTO window_states(feature_id, segment_id,
                       business_version_id, window_start, n, empty_window,
                       dirty, last_revision_id, stats_revision_id, updated_at)
                   VALUES(?,?,?,?,0,1,1,NULL,NULL,?)""",
                (f_id, s_id, v_id, window, _now()))

        if n == 0:
            stats = {
                "feature_id": f_id, "segment_id": s_id,
                "business_version_id": v_id, "window_start": window,
                "n": 0, "n_missing": 0, "missing_rate": 0.0,
                "n_outliers": 0, "outlier_rate": 0.0,
                "empty_window": True,
            }
            self._persist_window_stats(f_id, s_id, v_id, window, stats, rev_id)
            return stats

        present = [r for r in rows if not r["is_missing"]]
        stats = {
            "feature_id": f_id, "segment_id": s_id,
            "business_version_id": v_id, "window_start": window,
            "n": n,
            "n_missing": n_missing,
            "missing_rate": n_missing / n,
            "empty_window": False,
        }
        if feature["dtype"] == "numeric":
            values = [r["value"] for r in present if r["value"] is not None]
            summary = metrics.numeric_summary(values,
                                              outlier_fences=outlier_fences)
            stats.update({
                "n_outliers": summary.get("n_outliers", 0),
                "outlier_rate": summary.get("outlier_rate", 0.0),
                "mean": summary.get("mean"),
                "std": summary.get("std"),
                "min_value": summary.get("min"),
                "max_value": summary.get("max"),
                "q1": summary.get("q1"),
                "median": summary.get("median"),
                "q3": summary.get("q3"),
                "histogram_edges": summary.get("histogram_edges"),
                "histogram_counts": summary.get("histogram_counts"),
                "category_counts": None,
            })
        else:
            labels = [r["value_text"] for r in present
                      if r["value_text"] is not None]
            summary = metrics.categorical_summary(labels)
            stats.update({
                "n_outliers": 0, "outlier_rate": 0.0,
                "mean": None, "std": None, "min_value": None,
                "max_value": None, "q1": None, "median": None, "q3": None,
                "histogram_edges": None, "histogram_counts": None,
                "category_counts": summary.get("category_counts"),
            })
        self._persist_window_stats(f_id, s_id, v_id, window, stats, rev_id)
        return stats

    def _persist_window_stats(self, f_id, s_id, v_id, window, stats, rev_id):
        self.conn.execute(
            """INSERT INTO window_stats(feature_id, segment_id,
                   business_version_id, window_start, n, n_missing,
                   missing_rate, n_outliers, outlier_rate, mean, std,
                   min_value, max_value, q1, median, q3, histogram_edges,
                   histogram_counts, category_counts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(feature_id, segment_id, business_version_id,
                   window_start) DO UPDATE SET
                 n=excluded.n, n_missing=excluded.n_missing,
                 missing_rate=excluded.missing_rate,
                 n_outliers=excluded.n_outliers,
                 outlier_rate=excluded.outlier_rate, mean=excluded.mean,
                 std=excluded.std, min_value=excluded.min_value,
                 max_value=excluded.max_value, q1=excluded.q1,
                 median=excluded.median, q3=excluded.q3,
                 histogram_edges=excluded.histogram_edges,
                 histogram_counts=excluded.histogram_counts,
                 category_counts=excluded.category_counts""",
            (f_id, s_id, v_id, window, stats["n"], stats["n_missing"],
             stats["missing_rate"], stats["n_outliers"], stats["outlier_rate"],
             stats.get("mean"), stats.get("std"), stats.get("min_value"),
             stats.get("max_value"), stats.get("q1"), stats.get("median"),
             stats.get("q3"),
             _dumps(stats.get("histogram_edges")),
             _dumps(stats.get("histogram_counts")),
             _dumps(stats.get("category_counts"))),
        )
        self.conn.execute(
            """UPDATE window_states SET dirty=0, stats_revision_id=?,
                   n=?, empty_window=?, updated_at=?
               WHERE feature_id=? AND segment_id=? AND business_version_id=?
                 AND window_start=?""",
            (rev_id, stats["n"], 1 if stats.get("empty_window") else 0,
             _now(), f_id, s_id, v_id, window),
        )

    def list_windows(self, only_dirty=False):
        sql = """SELECT ws.*, f.name AS feature, f.dtype,
                        s.name AS segment, bv.name AS business_version
                 FROM window_states ws
                 JOIN features f ON f.id=ws.feature_id
                 JOIN segments s ON s.id=ws.segment_id
                 JOIN business_versions bv ON bv.id=ws.business_version_id"""
        if only_dirty:
            sql += " WHERE ws.dirty=1"
        sql += " ORDER BY ws.window_start"
        return [dict(r) for r in self.conn.execute(sql)]

    def get_window_stats(self, f_id, s_id, v_id, window):
        row = self.conn.execute(
            """SELECT ws.*, st.empty_window AS state_empty, st.dirty
               FROM window_stats ws
               JOIN window_states st
                 ON st.feature_id=ws.feature_id
                AND st.segment_id=ws.segment_id
                AND st.business_version_id=ws.business_version_id
                AND st.window_start=ws.window_start
               WHERE ws.feature_id=? AND ws.segment_id=?
                 AND ws.business_version_id=? AND ws.window_start=?""",
            (f_id, s_id, v_id, window),
        ).fetchone()
        if row:
            out = self._hydrate_stats(dict(row))
            # empty_window is a property of the window state, not the stats row
            out["empty_window"] = bool(out.pop("state_empty", 0))
            return out
        return None

    @staticmethod
    def _hydrate_stats(row):
        for col in ("histogram_edges", "histogram_counts", "category_counts"):
            row[col] = _loads(row.get(col), None)
        return row

    # ------------------------------------------------------------------ baselines
    def create_baseline(self, feature, segment, business_version,
                        window_from, window_to, note=""):
        """Pool every active observation in [from, to] into a NEW baseline
        version. Prior baseline rows stay untouched."""
        f_id, s_id, v_id = (self.feature_id(feature),
                            self.segment_id(segment),
                            self.version_id(business_version))
        version = self._next_baseline_version(f_id, s_id, v_id)
        rows = self.conn.execute(
            """SELECT * FROM observations
               WHERE feature_id=? AND segment_id=? AND business_version_id=?
                 AND window_start BETWEEN ? AND ?
                 AND superseded_by IS NULL ORDER BY id""",
            (f_id, s_id, v_id, window_from, window_to),
        ).fetchall()
        n = len(rows)
        n_missing = sum(r["is_missing"] for r in rows)
        present = [r for r in rows if not r["is_missing"]]
        feature_row = self.get_feature(f_id)
        payload = {
            "n": n,
            "n_missing": n_missing,
            "missing_rate": (n_missing / n) if n else 0.0,
        }
        if feature_row["dtype"] == "numeric":
            values = [r["value"] for r in present if r["value"] is not None]
            summary = metrics.numeric_summary(values)
            payload.update({
                "n_outliers": summary.get("n_outliers", 0),
                "outlier_rate": summary.get("outlier_rate", 0.0),
                "mean": summary.get("mean"), "std": summary.get("std"),
                "q1": summary.get("q1"), "median": summary.get("median"),
                "q3": summary.get("q3"), "iqr": summary.get("iqr"),
                "lower_fence": summary.get("lower_fence"),
                "upper_fence": summary.get("upper_fence"),
                "histogram_edges": summary.get("histogram_edges"),
                "histogram_counts": summary.get("histogram_counts"),
                "category_counts": None,
            })
        else:
            labels = [r["value_text"] for r in present
                      if r["value_text"] is not None]
            summary = metrics.categorical_summary(labels)
            payload.update({
                "n_outliers": 0, "outlier_rate": 0.0,
                "mean": None, "std": None, "q1": None, "median": None,
                "q3": None, "iqr": None, "lower_fence": None,
                "upper_fence": None, "histogram_edges": None,
                "histogram_counts": None,
                "category_counts": summary.get("category_counts"),
            })
        # deactivate previous versions, keep rows intact
        self.conn.execute(
            """UPDATE baselines SET active=0
               WHERE feature_id=? AND segment_id=? AND business_version_id=?""",
            (f_id, s_id, v_id),
        )
        cur = self.conn.execute(
            """INSERT INTO baselines(feature_id, segment_id,
                   business_version_id, version, window_from, window_to, n,
                   n_missing, missing_rate, n_outliers, outlier_rate, mean,
                   std, q1, median, q3, iqr, lower_fence, upper_fence,
                   histogram_edges, histogram_counts, category_counts, note,
                   active)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (f_id, s_id, v_id, version, window_from, window_to,
             payload["n"], payload["n_missing"], payload["missing_rate"],
             payload["n_outliers"], payload["outlier_rate"], payload["mean"],
             payload["std"], payload["q1"], payload["median"], payload["q3"],
             payload["iqr"], payload["lower_fence"], payload["upper_fence"],
             _dumps(payload.get("histogram_edges")),
             _dumps(payload.get("histogram_counts")),
             _dumps(payload.get("category_counts")), note),
        )
        self.conn.commit()
        return self.get_baseline(cur.lastrowid)

    def _next_baseline_version(self, f_id, s_id, v_id):
        row = self.conn.execute(
            """SELECT COALESCE(MAX(version),0)+1 AS v FROM baselines
               WHERE feature_id=? AND segment_id=? AND business_version_id=?""",
            (f_id, s_id, v_id),
        ).fetchone()
        return row["v"]

    def get_baseline(self, baseline_id):
        row = self.conn.execute(
            "SELECT * FROM baselines WHERE id=?", (baseline_id,)
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for col in ("histogram_edges", "histogram_counts", "category_counts"):
            out[col] = _loads(out.get(col), None)
        return out

    def list_baselines(self, active_only=False):
        sql = """SELECT b.*, f.name AS feature, s.name AS segment,
                        bv.name AS business_version
                 FROM baselines b
                 JOIN features f ON f.id=b.feature_id
                 JOIN segments s ON s.id=b.segment_id
                 JOIN business_versions bv ON bv.id=b.business_version_id"""
        if active_only:
            sql += " WHERE b.active=1"
        sql += " ORDER BY b.feature_id, b.segment_id, b.version"
        out = []
        for r in self.conn.execute(sql):
            d = dict(r)
            d["histogram_edges"] = _loads(d.get("histogram_edges"), None)
            d["histogram_counts"] = _loads(d.get("histogram_counts"), None)
            d["category_counts"] = _loads(d.get("category_counts"), None)
            out.append(d)
        return out

    def active_baseline(self, f_id, s_id, v_id):
        row = self.conn.execute(
            """SELECT * FROM baselines
               WHERE feature_id=? AND segment_id=? AND business_version_id=?
                 AND active=1 ORDER BY version DESC LIMIT 1""",
            (f_id, s_id, v_id),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for col in ("histogram_edges", "histogram_counts", "category_counts"):
            out[col] = _loads(out.get(col), None)
        return out

    # ------------------------------------------------------------------ policies
    def upsert_policy(self, *, psi_threshold=1.0, js_threshold=0.15,
                      missing_rate_delta=0.1, outlier_rate_delta=0.1,
                      mean_z_threshold=3.0, min_sample_size=5,
                      notify_empty_window=1,
                      feature=None, segment=None, business_version=None):
        """Create a new policy *version*; older rows remain for audit."""
        f_id = self.feature_id(feature) if feature else None
        s_id = self.segment_id(segment) if segment else None
        v_id = self.version_id(business_version) if business_version else None
        row = self.conn.execute(
            """SELECT COALESCE(MAX(version),0)+1 AS v FROM alert_policies
               WHERE COALESCE(feature_id,-1)=COALESCE(?,-1)
                 AND COALESCE(segment_id,-1)=COALESCE(?,-1)
                 AND COALESCE(business_version_id,-1)=COALESCE(?,-1)""",
            (f_id, s_id, v_id),
        ).fetchone()
        existing = self.conn.execute(
            """SELECT id FROM alert_policies
               WHERE COALESCE(feature_id,-1)=COALESCE(?,-1)
                 AND COALESCE(segment_id,-1)=COALESCE(?,-1)
                 AND COALESCE(business_version_id,-1)=COALESCE(?,-1)
               LIMIT 1""",
            (f_id, s_id, v_id),
        ).fetchone()
        if existing:  # only deactivate when a prior version actually exists
            self.conn.execute(
                """UPDATE alert_policies SET active=0
                   WHERE COALESCE(feature_id,-1)=COALESCE(?,-1)
                     AND COALESCE(segment_id,-1)=COALESCE(?,-1)
                     AND COALESCE(business_version_id,-1)=COALESCE(?,-1)""",
                (f_id, s_id, v_id),
            )
        cur = self.conn.execute(
            """INSERT INTO alert_policies(feature_id, segment_id,
                   business_version_id, version, psi_threshold, js_threshold,
                   missing_rate_delta, outlier_rate_delta, mean_z_threshold,
                   min_sample_size, notify_empty_window, active)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
            (f_id, s_id, v_id, row["v"], psi_threshold, js_threshold,
             missing_rate_delta, outlier_rate_delta, mean_z_threshold,
             min_sample_size, notify_empty_window),
        )
        self.conn.commit()
        return self.get_policy(cur.lastrowid)

    def get_policy(self, policy_id):
        row = self.conn.execute(
            "SELECT * FROM alert_policies WHERE id=?", (policy_id,)
        ).fetchone()
        return dict(row) if row else None

    def resolve_policy(self, f_id, s_id, v_id):
        """Most specific active policy wins: exact scope > partial > global."""
        candidates = self.conn.execute(
            "SELECT * FROM alert_policies WHERE active=1"
        ).fetchall()

        def specificity(row):
            return (1 if row["feature_id"] == f_id else 0,
                    1 if row["segment_id"] == s_id else 0,
                    1 if row["business_version_id"] == v_id else 0)

        best, best_score = None, (-1, -1, -1)
        for row in candidates:
            if (row["feature_id"] not in (None, f_id)
                    or row["segment_id"] not in (None, s_id)
                    or row["business_version_id"] not in (None, v_id)):
                continue
            score = specificity(row)
            if score > best_score:
                best, best_score = dict(row), score
        if best is None:  # defensive: seeded policy should always match
            best = {
                "id": self.conn.execute(
                    "SELECT id FROM alert_policies LIMIT 1").fetchone()["id"],
                "psi_threshold": 1.0, "js_threshold": 0.15,
                "missing_rate_delta": 0.1, "outlier_rate_delta": 0.1,
                "mean_z_threshold": 3.0, "min_sample_size": 5,
                "notify_empty_window": 1,
            }
        return best
