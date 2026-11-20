"""SQLite schema for the pointwise feature-drift monitor.

Design guarantees encoded in the schema:

* Baselines are **versioned rows** — updating a baseline inserts a new row and
  never overwrites prior versions (``baselines.version`` keeps growing).
* Alerts are **immutable fact rows**: their identity (feature/segment/
  business_version/window/baseline) is unique, while lifecycle transitions are
  appended to ``alert_events``. Nothing is ever deleted.
* Raw observations are append-only; a data revision supersedes old rows with a
  new revision_id instead of mutating or deleting them.
* ``window_states.dirty`` marks exactly the windows touched by a revision so
  re-evaluation can be scoped to affected windows only.
"""

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- features
CREATE TABLE IF NOT EXISTS features (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    dtype       TEXT NOT NULL CHECK (dtype IN ('numeric','categorical')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- segments
-- A segment is a named pointwise sub-population, e.g. {"city":"SH"}.
CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    definition  TEXT NOT NULL,            -- JSON predicates over observation tags
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- business versions
CREATE TABLE IF NOT EXISTS business_versions (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,     -- e.g. 'v2026.11.0'
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- data revisions
-- Every batch of observations carries a revision. Revising data inserts a new
-- revision row and supersedes the old observation rows rather than deleting
-- them, leaving an auditable lineage.
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY,
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- observations (append only)
CREATE TABLE IF NOT EXISTS observations (
    id                 INTEGER PRIMARY KEY,
    feature_id         INTEGER NOT NULL REFERENCES features(id),
    segment_id         INTEGER NOT NULL REFERENCES segments(id),
    business_version_id INTEGER NOT NULL REFERENCES business_versions(id),
    window_start       TEXT NOT NULL,          -- ISO timestamp, hour/day bucket
    value              REAL,                   -- NULL => missing value
    value_text         TEXT,                   -- category label (categorical)
    is_missing         INTEGER NOT NULL DEFAULT 0,
    tags               TEXT NOT NULL DEFAULT '{}', -- JSON: evidence attributes
    revision_id        INTEGER NOT NULL REFERENCES revisions(id),
    superseded_by      INTEGER REFERENCES revisions(id),
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup
    ON observations(feature_id, segment_id, business_version_id, window_start, revision_id);
CREATE INDEX IF NOT EXISTS idx_obs_active
    ON observations(superseded_by, window_start);

-- ---------------------------------------------------------------- window materialised state
CREATE TABLE IF NOT EXISTS window_states (
    feature_id          INTEGER NOT NULL REFERENCES features(id),
    segment_id          INTEGER NOT NULL REFERENCES segments(id),
    business_version_id INTEGER NOT NULL REFERENCES business_versions(id),
    window_start        TEXT NOT NULL,
    n                   INTEGER NOT NULL DEFAULT 0,
    empty_window        INTEGER NOT NULL DEFAULT 1,
    dirty               INTEGER NOT NULL DEFAULT 1,   -- needs re-evaluation
    last_revision_id    INTEGER REFERENCES revisions(id),
    stats_revision_id   INTEGER REFERENCES revisions(id),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feature_id, segment_id, business_version_id, window_start)
);

-- ---------------------------------------------------------------- window statistics (materialised from active observations)
CREATE TABLE IF NOT EXISTS window_stats (
    feature_id          INTEGER NOT NULL,
    segment_id          INTEGER NOT NULL,
    business_version_id INTEGER NOT NULL,
    window_start        TEXT NOT NULL,
    n                   INTEGER NOT NULL,
    n_missing           INTEGER NOT NULL,
    missing_rate        REAL NOT NULL,
    n_outliers          INTEGER NOT NULL,
    outlier_rate        REAL NOT NULL,
    mean                REAL,
    std                 REAL,
    min_value           REAL,
    max_value           REAL,
    q1                  REAL,
    median              REAL,
    q3                  REAL,
    histogram_edges     TEXT,               -- JSON numeric bin edges
    histogram_counts    TEXT,               -- JSON counts
    category_counts     TEXT,               -- JSON {label: count}
    PRIMARY KEY (feature_id, segment_id, business_version_id, window_start)
);

-- ---------------------------------------------------------------- baselines (versioned, never overwritten)
CREATE TABLE IF NOT EXISTS baselines (
    id                  INTEGER PRIMARY KEY,
    feature_id          INTEGER NOT NULL REFERENCES features(id),
    segment_id          INTEGER NOT NULL REFERENCES segments(id),
    business_version_id INTEGER NOT NULL REFERENCES business_versions(id),
    version             INTEGER NOT NULL,
    window_from         TEXT NOT NULL,
    window_to           TEXT NOT NULL,
    n                   INTEGER NOT NULL,
    n_missing           INTEGER NOT NULL,
    missing_rate        REAL NOT NULL,
    n_outliers          INTEGER NOT NULL,
    outlier_rate        REAL NOT NULL,
    mean                REAL,
    std                 REAL,
    q1                  REAL,
    median              REAL,
    q3                  REAL,
    iqr                 REAL,
    lower_fence         REAL,
    upper_fence         REAL,
    histogram_edges     TEXT,
    histogram_counts    TEXT,
    category_counts     TEXT,
    note                TEXT NOT NULL DEFAULT '',
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (feature_id, segment_id, business_version_id, version)
);

-- ---------------------------------------------------------------- alert policies (versioned)
CREATE TABLE IF NOT EXISTS alert_policies (
    id          INTEGER PRIMARY KEY,
    feature_id  INTEGER REFERENCES features(id),      -- NULL => all features
    segment_id  INTEGER REFERENCES segments(id),      -- NULL => all segments
    business_version_id INTEGER REFERENCES business_versions(id), -- NULL => all
    version     INTEGER NOT NULL,
    psi_threshold       REAL NOT NULL,
    js_threshold        REAL NOT NULL DEFAULT 0.15,
    missing_rate_delta  REAL NOT NULL,
    outlier_rate_delta  REAL NOT NULL,
    mean_z_threshold    REAL NOT NULL,
    min_sample_size     INTEGER NOT NULL,
    notify_empty_window INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- alerts (immutable identity, status transitions only)
CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY,
    feature_id          INTEGER NOT NULL,
    segment_id          INTEGER NOT NULL,
    business_version_id INTEGER NOT NULL,
    window_start        TEXT NOT NULL,
    baseline_id         INTEGER REFERENCES baselines(id), -- NULL only for info/empty alerts
    policy_id           INTEGER NOT NULL REFERENCES alert_policies(id),
    severity            TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    status              TEXT NOT NULL CHECK (status IN ('open','acknowledged','resolved','suppressed')),
    first_seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at         TEXT,
    is_backfill         INTEGER NOT NULL DEFAULT 0,
    -- snapshot of the metric values that produced the latest state
    metrics_snapshot    TEXT NOT NULL DEFAULT '{}',
    evidence_samples    TEXT NOT NULL DEFAULT '[]',
    UNIQUE (feature_id, segment_id, business_version_id, window_start, baseline_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, severity);

-- ---------------------------------------------------------------- alert lifecycle event log (append only)
CREATE TABLE IF NOT EXISTS alert_events (
    id          INTEGER PRIMARY KEY,
    alert_id    INTEGER NOT NULL REFERENCES alerts(id),
    event_type  TEXT NOT NULL CHECK (event_type IN
                  ('created','updated','severity_changed','acknowledged',
                   'resolved','reopened','suppressed','backfilled')),
    detail      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_alert ON alert_events(alert_id, id);

-- ---------------------------------------------------------------- evaluation runs (audit trail)
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('scheduled','backfill','revision')),
    window_from         TEXT,
    window_to           TEXT,
    windows_considered  INTEGER NOT NULL,
    windows_empty       INTEGER NOT NULL,
    alerts_created      INTEGER NOT NULL,
    alerts_updated      INTEGER NOT NULL,
    alerts_resolved     INTEGER NOT NULL,
    revision_id         INTEGER REFERENCES revisions(id),
    note                TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def apply_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()
