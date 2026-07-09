-- Macro enrichment tables
-- Run once; safe to re-run (CREATE TABLE IF NOT EXISTS)

CREATE TABLE IF NOT EXISTS macro_releases (
    release_id       TEXT PRIMARY KEY,
    series_id        TEXT NOT NULL,
    event_name       TEXT NOT NULL,
    released_at      TEXT NOT NULL,
    period_label     TEXT,
    actual_value     REAL,
    prior_value      REAL,
    consensus_value  REAL,
    surprise         REAL,
    surprise_severity INTEGER DEFAULT 0,
    yoy_change       REAL,
    mom_change       REAL,
    source           TEXT DEFAULT 'fred',
    raw_data         TEXT,
    UNIQUE(series_id, released_at)
);

CREATE INDEX IF NOT EXISTS idx_macro_releases_event
    ON macro_releases(event_name, released_at);
CREATE INDEX IF NOT EXISTS idx_macro_releases_severity
    ON macro_releases(surprise_severity, released_at);

CREATE TABLE IF NOT EXISTS macro_calendar (
    calendar_id     TEXT PRIMARY KEY,
    event_name      TEXT NOT NULL,
    scheduled_at    TEXT NOT NULL,
    period_label    TEXT,
    consensus_value REAL,
    importance      TEXT DEFAULT 'high',
    released        INTEGER DEFAULT 0,
    release_id      TEXT,
    source          TEXT DEFAULT 'computed',
    UNIQUE(event_name, scheduled_at)
);

CREATE INDEX IF NOT EXISTS idx_macro_calendar_upcoming
    ON macro_calendar(scheduled_at, released);
