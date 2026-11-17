"""Backward-compatibility tests for databases created before later schema work.

`LEGACY_SCHEMA` is the `server/app/db.py` SCHEMA from base commit 29baa54.
`db.init()` must preserve existing rows and be idempotent. When the
ride-recorder schema is merged, the second test additionally verifies that the
rides table, telemetry columns and indexes are added to a legacy database.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config, db

LEGACY_SCHEMA = """
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
  gps_fix      INTEGER NOT NULL DEFAULT 0,
  lat REAL, lon REAL, alt REAL, speed_kmh REAL, course REAL, sats INTEGER, hdop REAL,
  temp_c REAL, humidity REAL, pressure_hpa REAL, gas_kohm REAL,
  aqi REAL, aqi_source TEXT, eco2_ppm REAL, tvoc_ppb REAL,
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

RIDES_COLUMNS = {"ride_id", "pm1", "pm2_5", "pm4", "pm10",
                 "nc0_5", "nc1", "nc2_5", "nc4", "nc10"}
RIDES_SCHEMA_MARKER = "CREATE TABLE IF NOT EXISTS rides"


def _remove_db_files() -> None:
    for suffix in ("", "-wal", "-shm"):
        config.DB_PATH.with_name(config.DB_PATH.name + suffix).unlink(missing_ok=True)


def _telemetry_columns() -> set[str]:
    return {row["name"] for row in db.query("PRAGMA table_info(telemetry)")}


def _telemetry_indexes() -> set[str]:
    return {row["name"] for row in db.query("PRAGMA index_list(telemetry)")}


def _reopen() -> None:
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    db.init()


@pytest.fixture()
def legacy_db(database):  # noqa: ARG001
    """Replace the fresh test DB with a 29baa54-era schema and one row."""
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    _remove_db_files()

    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO devices (id, name, key_hash) VALUES (?, ?, ?)",
        ("legacy-dev", "Legacy CYD", "not-a-real-hash"),
    )
    conn.execute(
        """INSERT INTO telemetry (device_id, seq, ts, received_at, transport, gps_fix,
                                  lat, lon, temp_c, humidity, pressure_hpa, aqi)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("legacy-dev", 7, "2026-01-01T10:00:00.000Z", "2026-01-01T10:00:01.000Z",
         "mqtt", 1, 54.37, 18.61, 21.5, 44.0, 1012.5, 30.0),
    )
    conn.commit()
    conn.close()
    yield


def test_legacy_schema_is_preserved_and_init_is_idempotent(legacy_db):
    db.init()

    row = db.one("SELECT * FROM telemetry WHERE device_id = ?", ("legacy-dev",))
    assert row is not None
    assert row["seq"] == 7
    assert row["temp_c"] == 21.5
    assert db.one("SELECT name FROM devices WHERE id = ?", ("legacy-dev",))["name"] == "Legacy CYD"

    columns = _telemetry_columns()
    indexes = _telemetry_indexes()
    _reopen()

    assert _telemetry_columns() == columns
    assert _telemetry_indexes() == indexes
    assert db.one("SELECT COUNT(*) AS n FROM telemetry WHERE device_id = ?",
                  ("legacy-dev",))["n"] == 1


def test_rides_schema_is_added_to_legacy_db(legacy_db):
    if RIDES_SCHEMA_MARKER not in db.SCHEMA:
        pytest.skip("ride-recorder schema is not on this branch; runs after feat/ride-recorder merges")

    # Query the raw file: db.one()/db.query() would call db.init() lazily and
    # create the rides table before we get to assert that it is absent.
    conn = sqlite3.connect(config.DB_PATH)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='rides'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == 0

    db.init()

    assert RIDES_COLUMNS <= _telemetry_columns()
    assert {"ix_telemetry_ride_id", "ix_telemetry_ride_ts",
            "ux_telemetry_dedupe", "ix_telemetry_device_ts"} <= _telemetry_indexes()
    assert db.one(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='rides'"
    )["n"] == 1
    assert db.one("SELECT ride_id, pm2_5 FROM telemetry WHERE device_id = ?",
                  ("legacy-dev",)) == {"ride_id": None, "pm2_5": None}

    columns = _telemetry_columns()
    indexes = _telemetry_indexes()
    _reopen()
    assert _telemetry_columns() == columns
    assert _telemetry_indexes() == indexes
    assert db.one("SELECT COUNT(*) AS n FROM telemetry WHERE device_id = ?",
                  ("legacy-dev",))["n"] == 1
