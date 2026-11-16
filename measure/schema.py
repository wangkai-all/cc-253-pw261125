"""SQLite schema.  Append-only guarantees live in triggers, not in callers."""

SCHEMA_VERSION = 1

SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
  id TEXT PRIMARY KEY,
  device TEXT NOT NULL,
  seq INTEGER NOT NULL,
  raw INTEGER NOT NULL,
  device_ts TEXT NOT NULL,
  device_ts_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  received_ms INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  UNIQUE(device, seq)
);

CREATE TABLE IF NOT EXISTS calibrations (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  cal_id TEXT,
  cal_version INTEGER,
  device TEXT,
  decision TEXT NOT NULL,
  reason TEXT,
  detail TEXT,
  payload_json TEXT,
  digest TEXT,
  received_at TEXT NOT NULL,
  received_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS calibrations_accepted_unique
  ON calibrations(cal_id, cal_version, digest) WHERE decision = 'accepted';
CREATE INDEX IF NOT EXISTS calibrations_device ON calibrations(device, cal_version);

CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reading_id TEXT NOT NULL REFERENCES readings(id),
  revision INTEGER NOT NULL,
  status TEXT NOT NULL,
  device TEXT NOT NULL,
  seq INTEGER NOT NULL,
  raw INTEGER NOT NULL,
  device_ts TEXT NOT NULL,
  device_ts_ms INTEGER NOT NULL,
  value_kpa REAL,
  reason TEXT,
  chosen_by TEXT NOT NULL,
  cal_seq INTEGER,
  cal_id TEXT,
  cal_version INTEGER,
  cal_doc_digest TEXT,
  sem_identity TEXT,
  publish_mode TEXT NOT NULL,
  source TEXT NOT NULL,
  supersedes INTEGER,
  created_at TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  UNIQUE(reading_id, revision)
);

CREATE INDEX IF NOT EXISTS publications_reading ON publications(reading_id, revision);
CREATE INDEX IF NOT EXISTS publications_device_ts ON publications(device, device_ts_ms);
CREATE INDEX IF NOT EXISTS readings_device_ts ON readings(device, device_ts_ms);

CREATE TABLE IF NOT EXISTS jobs (
  reading_id TEXT PRIMARY KEY REFERENCES readings(id),
  enqueued_ms INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_token TEXT,
  claimed_ms INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
  device TEXT PRIMARY KEY,
  publish_mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  token TEXT PRIMARY KEY,
  reading_id TEXT NOT NULL REFERENCES readings(id),
  device TEXT NOT NULL,
  started_ms INTEGER NOT NULL,
  state TEXT NOT NULL,
  snapshot_cal_seq INTEGER,
  snapshot_sem TEXT,
  snapshot_chosen_by TEXT,
  snapshot_mode TEXT NOT NULL,
  finished_ms INTEGER,
  outcome TEXT,
  revision INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  device TEXT,
  reading_id TEXT,
  detail_json TEXT
);

CREATE TRIGGER IF NOT EXISTS publications_no_update
BEFORE UPDATE ON publications
BEGIN
  SELECT RAISE(ABORT, 'publications are append-only');
END;

CREATE TRIGGER IF NOT EXISTS publications_no_delete
BEFORE DELETE ON publications
BEGIN
  SELECT RAISE(ABORT, 'publications are append-only');
END;

CREATE TRIGGER IF NOT EXISTS calibrations_no_update
BEFORE UPDATE ON calibrations
BEGIN
  SELECT RAISE(ABORT, 'calibration deliveries are append-only');
END;

CREATE TRIGGER IF NOT EXISTS calibrations_no_delete
BEFORE DELETE ON calibrations
BEGIN
  SELECT RAISE(ABORT, 'calibration deliveries are append-only');
END;

CREATE TRIGGER IF NOT EXISTS readings_no_update
BEFORE UPDATE ON readings
BEGIN
  SELECT RAISE(ABORT, 'raw readings are immutable');
END;

CREATE TRIGGER IF NOT EXISTS readings_no_delete
BEFORE DELETE ON readings
BEGIN
  SELECT RAISE(ABORT, 'raw readings are immutable');
END;
"""
