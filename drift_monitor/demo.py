"""End-to-end demo / seeding CLI.

Creates a deterministic scenario that exercises every product requirement:

* two features (numeric + categorical), two population segments and two
  business versions;
* a versioned baseline, a drifted window (mean shift + missingness + outliers),
  an *empty* segment window, and a stable window;
* an alerting policy with explicit thresholds;
* a data revision that repairs ONE window -> only that window is re-evaluated;
* a new baseline version followed by a historical backfill that preserves
  prior alerts.

Usage::

    python -m drift_monitor.demo --db demo.db            # seed + evaluate
    python -m drift_monitor.demo --db demo.db --serve 8080
"""
from __future__ import annotations

import argparse
import random

from .api import build_server
from .engine import DriftEngine
from .store import Store

BASE_WINDOWS = [f"2026-11-{d:02d}T00:00:00" for d in range(1, 8)]
LATE_WINDOWS = [f"2026-11-{d:02d}T00:00:00" for d in range(8, 14)]


def _numeric(rng, feature, segment, version, windows, n, mu, sigma,
             missing_rate=0.0, outlier_injection=0):
    out = []
    for window in windows:
        for i in range(n):
            is_missing = rng.random() < missing_rate
            value = None
            if not is_missing:
                value = rng.gauss(mu, sigma)
                if outlier_injection and rng.random() < outlier_injection:
                    value += rng.choice((-1, 1)) * 6 * sigma
            out.append({"feature": feature, "dtype": "numeric",
                        "segment": segment, "business_version": version,
                        "window_start": window, "value": value,
                        "is_missing": is_missing,
                        "tags": {"row": i, "segment": segment}})
    return out


def _categorical(rng, feature, segment, version, windows, probs, n):
    labels, weights = list(probs), list(probs.values())
    out = []
    for window in windows:
        for _ in range(n):
            out.append({"feature": feature, "dtype": "categorical",
                        "segment": segment, "business_version": version,
                        "window_start": window,
                        "value_text": rng.choices(labels, weights=weights)[0]})
    return out


def seed(store: Store):
    rng = random.Random(20261120)
    # ---- dimensions + baseline-era data (business version v1)
    store.ingest(
        _numeric(rng, "order_amount", "tier_a", "v1", BASE_WINDOWS,
                 40, 100, 10), note="v1 baseline: tier_a order_amount")
    store.ingest(
        _numeric(rng, "order_amount", "tier_b", "v1", BASE_WINDOWS,
                 30, 100, 10), note="v1 baseline: tier_b order_amount")
    store.ingest(
        _categorical(rng, "pay_channel", "tier_a", "v1", BASE_WINDOWS,
                     {"web": 0.6, "app": 0.3, "mini": 0.1}, 40),
        note="v1 baseline: pay_channel")

    # ---- v2 business version: stable + drifted + empty windows
    store.ingest(
        _numeric(rng, "order_amount", "tier_a", "v2", LATE_WINDOWS[:2],
                 40, 100, 10), note="v2 stable windows")
    store.ingest(
        _numeric(rng, "order_amount", "tier_a", "v2", LATE_WINDOWS[2:4],
                 40, 145, 14, missing_rate=0.28, outlier_injection=0.08),
        note="v2 DRIFTED windows: mean shift, missingness, outliers")
    store.ingest(
        _categorical(rng, "pay_channel", "tier_a", "v2", LATE_WINDOWS[:3],
                     {"web": 0.15, "app": 0.25, "mini": 0.60}, 40),
        note="v2 channel mix shift")
    # tier_b v2: one populated window then a fully empty window
    store.ingest(
        _numeric(rng, "order_amount", "tier_b", "v2", LATE_WINDOWS[:1],
                 30, 100, 10), note="v2 tier_b normal")
    store.ensure_segment("tier_c", {"tier": "c", "region": "west"})
    # create tier_c window state then wipe it -> empty sub-population
    store.ingest(
        _numeric(rng, "order_amount", "tier_c", "v2", LATE_WINDOWS[4:5],
                 8, 100, 10), note="v2 tier_c initial")
    store.revise([{
        "feature": "order_amount", "segment": "tier_c",
        "business_version": "v2", "window_start": LATE_WINDOWS[4],
        "rows": [],
    }], note="v2 tier_c pipeline outage -> empty window")

    # ---- v2 baselines learned from the *stable* v2 windows, plus policies.
    # A baseline is always tied to (feature, segment, business_version), so
    # the v2 traffic gets its own baseline even though training data is short.
    store.create_baseline("order_amount", "tier_a", "v2",
                          LATE_WINDOWS[0], LATE_WINDOWS[1],
                          note="v2 stable-window baseline")
    store.create_baseline("pay_channel", "tier_a", "v2",
                          LATE_WINDOWS[0], LATE_WINDOWS[1],
                          note="v2 channel baseline (pre-mix-shift)")
    for feature, segment in (("order_amount", "tier_a"),
                             ("order_amount", "tier_b"),
                             ("pay_channel", "tier_a")):
        store.create_baseline(feature, segment, "v1",
                              BASE_WINDOWS[0], BASE_WINDOWS[-1],
                              note="initial production baseline")
    store.upsert_policy(psi_threshold=1.0, js_threshold=0.15,
                        missing_rate_delta=0.15, outlier_rate_delta=0.1,
                        mean_z_threshold=3.0, min_sample_size=5)
    engine = DriftEngine(store)
    first_run = engine.evaluate(note="initial scheduled evaluation")

    # ---- data revision: repair the first drifted window only
    repaired_window = LATE_WINDOWS[2]
    store.revise([{
        "feature": "order_amount", "segment": "tier_a",
        "business_version": "v2", "window_start": repaired_window,
        "rows": _numeric(rng, "order_amount", "tier_a", "v2",
                         [repaired_window], 40, 100, 10),
    }], note="data correction: pipeline fix for " + repaired_window)
    revision_run = engine.evaluate(only_dirty=True, kind="revision",
                                   note="affected-window re-evaluation only")

    # ---- new baseline version (history preserved) + backfill
    new_baseline = store.create_baseline(
        "order_amount", "tier_a", "v2",
        LATE_WINDOWS[0], LATE_WINDOWS[3],
        note="retrained v2 baseline absorbing corrected windows")
    backfill_run = engine.backfill(
        LATE_WINDOWS[0], LATE_WINDOWS[3], baseline_id=new_baseline["id"],
        note="retroactive backfill against retrained baseline")

    return {"first_run": first_run, "revision_run": revision_run,
            "backfill_run": backfill_run,
            "new_baseline_id": new_baseline["id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed demo scenario")
    parser.add_argument("--db", default="demo.db")
    parser.add_argument("--serve", type=int, metavar="PORT",
                        help="start the HTTP server after seeding")
    args = parser.parse_args(argv)
    store = Store(args.db)
    summary = seed(store)
    print("seeded scenario:")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"  total alerts: {len(DriftEngine(store).list_alerts())}")
    if args.serve:
        server = build_server(args.db, args.serve)
        print(f"dashboard: http://localhost:{args.serve}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.store.close()
    else:
        store.close()


if __name__ == "__main__":
    main()
