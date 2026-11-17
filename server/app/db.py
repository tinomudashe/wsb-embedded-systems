"""SQLite persistence (WAL). Schema is created and migrated idempotently on startup."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  full_name     TEXT,
  email         TEXT,
  role          TEXT DEFAULT 'Researcher',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_login    TEXT
);

CREATE TABLE IF NOT EXISTS devices (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  key_hash       TEXT NOT NULL,
  owner_username TEXT,
  mac_address    TEXT,
  claim_code     TEXT,
  location_label TEXT,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_seen      TEXT,
  online         INTEGER NOT NULL DEFAULT 0,
  fw             TEXT,
  ip             TEXT,
  cfg_version    INTEGER,
  last_status    TEXT
);

CREATE TABLE IF NOT EXISTS pairing_tokens (
  token       TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  claim_code  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_config (
  device_id   TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS rides (
  id           TEXT PRIMARY KEY,
  device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  filename     TEXT NOT NULL,
  sha256       TEXT NOT NULL,
  started_at   TEXT,
  ended_at     TEXT,
  duration_s   REAL,
  distance_m   REAL,
  sample_count INTEGER NOT NULL DEFAULT 0,
  stats_json   TEXT,
  uploaded_at  TEXT NOT NULL,
  UNIQUE(device_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_rides_device_started ON rides(device_id, started_at DESC);

CREATE TABLE IF NOT EXISTS telemetry (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  seq          INTEGER,
  ts           TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  ts_source    TEXT,
  uptime_ms    INTEGER,
  replay       INTEGER NOT NULL DEFAULT 0,
  transport    TEXT,
  ride_id      TEXT,
  gps_fix      INTEGER NOT NULL DEFAULT 0,
  lat REAL, lon REAL, alt REAL, speed_kmh REAL, course REAL, sats INTEGER, hdop REAL,
  temp_c REAL, humidity REAL, pressure_hpa REAL, gas_kohm REAL,
  aqi REAL, aqi_source TEXT, eco2_ppm REAL, tvoc_ppb REAL,
  pm1 REAL, pm2_5 REAL, pm4 REAL, pm10 REAL,
  nc0_5 REAL, nc1 REAL, nc2_5 REAL, nc4 REAL, nc10 REAL, ps REAL,
  rssi INTEGER, heap INTEGER, buffered INTEGER, fw TEXT, cfg_version INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_telemetry_dedupe ON telemetry(device_id, ts, seq);
CREATE INDEX IF NOT EXISTS ix_telemetry_device_ts ON telemetry(device_id, ts);

CREATE TABLE IF NOT EXISTS rejected_messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT,
  received_at TEXT NOT NULL,
  reason      TEXT NOT NULL,
  payload     TEXT
);

CREATE TABLE IF NOT EXISTS system_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  source      TEXT NOT NULL,
  device_id   TEXT,
  level       TEXT NOT NULL DEFAULT 'INFO',
  event       TEXT NOT NULL,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_system_logs_ts ON system_logs(ts DESC, id DESC);
"""

MIGRATIONS = [
    ("users", "full_name", "ALTER TABLE users ADD COLUMN full_name TEXT"),
    ("users", "email", "ALTER TABLE users ADD COLUMN email TEXT"),
    ("users", "role", "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'Researcher'"),
    ("devices", "owner_username", "ALTER TABLE devices ADD COLUMN owner_username TEXT"),
    ("devices", "mac_address", "ALTER TABLE devices ADD COLUMN mac_address TEXT"),
    ("devices", "claim_code", "ALTER TABLE devices ADD COLUMN claim_code TEXT"),
    ("devices", "location_label", "ALTER TABLE devices ADD COLUMN location_label TEXT"),
    ("telemetry", "ride_id", "ALTER TABLE telemetry ADD COLUMN ride_id TEXT"),
    ("telemetry", "pm1", "ALTER TABLE telemetry ADD COLUMN pm1 REAL"),
    ("telemetry", "pm2_5", "ALTER TABLE telemetry ADD COLUMN pm2_5 REAL"),
    ("telemetry", "pm4", "ALTER TABLE telemetry ADD COLUMN pm4 REAL"),
    ("telemetry", "pm10", "ALTER TABLE telemetry ADD COLUMN pm10 REAL"),
    ("telemetry", "nc0_5", "ALTER TABLE telemetry ADD COLUMN nc0_5 REAL"),
    ("telemetry", "nc1", "ALTER TABLE telemetry ADD COLUMN nc1 REAL"),
    ("telemetry", "nc2_5", "ALTER TABLE telemetry ADD COLUMN nc2_5 REAL"),
    ("telemetry", "nc4", "ALTER TABLE telemetry ADD COLUMN nc4 REAL"),
    ("telemetry", "nc10", "ALTER TABLE telemetry ADD COLUMN nc10 REAL"),
    ("telemetry", "ps", "ALTER TABLE telemetry ADD COLUMN ps REAL"),
]

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    for table, col, sql in MIGRATIONS:
        cols = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            _conn.execute(sql)
    # Created after migrations so it also works on databases that predate ride_id.
    _conn.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_ride_id ON telemetry(ride_id)")
    _conn.execute("CREATE INDEX IF NOT EXISTS ix_telemetry_ride_ts ON telemetry(ride_id, ts)")


def _c() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


def query(sql: str, params: Any = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _c().execute(sql, params).fetchall()]


def one(sql: str, params: Any = ()) -> dict | None:
    with _lock:
        r = _c().execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params: Any = ()) -> sqlite3.Cursor:
    with _lock:
        return _c().execute(sql, params)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _c()
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
