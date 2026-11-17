"""Tests for the SQLite data-access layer (app.store / app.db) on a temp DB."""
from __future__ import annotations

import sqlite3

import pytest

from app import db, store
from app.schemas import DeviceConfig, normalize_telemetry

DEVICE = "cyd-store"
FIXED_TS = "2026-05-01T10:00:00.000Z"


def _record(device_id: str = DEVICE, seq: int = 1, ts: str = FIXED_TS) -> dict:
    msg = {
        "device_id": device_id,
        "seq": seq,
        "ts": ts,
        "uptime_ms": 1_000,
        "gps": {"fix": True, "lat": 54.37, "lon": 18.61, "alt": 35.0,
                "speed_kmh": 0.0, "course": 0.0, "sats": 8, "hdop": 0.9},
        "env": {"temp_c": 21.5, "humidity": 44.0, "pressure_hpa": 1012.0,
                "gas_kohm": 25.0, "aqi": 30.0, "aqi_source": "ens160"},
        "sys": {"rssi": -60, "heap": 100_000, "buffered": 0, "fw": "1.2.0", "cfg_version": 1},
    }
    rec, warnings = normalize_telemetry(msg, device_id, "http")
    assert warnings == []
    return rec


def _blank_record(device_id: str, ts: str) -> dict:
    row = {col: None for col in store.TELEMETRY_COLS}
    row.update(device_id=device_id, ts=ts, received_at=ts, transport="http", gps_fix=0, replay=0)
    return row


def test_create_device_returns_working_key(database):  # noqa: ARG001
    key = store.create_device(DEVICE, name="Store CYD", owner_username="alice",
                              location_label="Lab 101", mac_address="AA:BB:CC:DD:EE:FF")

    raw = store.get_device(DEVICE)
    assert raw is not None
    assert raw["key_hash"] != key
    assert raw["claim_code"]

    public = store.public_device(raw)
    assert "key_hash" not in public
    assert "last_status" not in public
    assert public["name"] == "Store CYD"
    assert public["online"] is False

    assert store.verify_device_key(DEVICE, key) is True
    assert store.verify_device_key(DEVICE, "nope") is False


def test_create_device_rejects_duplicate_id(database):  # noqa: ARG001
    store.create_device("cyd-dup", name="One")

    with pytest.raises(sqlite3.IntegrityError):
        store.create_device("cyd-dup", name="Two")


def test_telemetry_insert_and_dedupe(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    rec = _record()

    assert store.store_telemetry([rec]) == 1
    assert store.store_telemetry([rec]) == 0  # same (device_id, ts, seq)

    assert db.one("SELECT COUNT(*) AS n FROM telemetry")["n"] == 1
    latest = store.latest_telemetry(DEVICE)
    assert latest["seq"] == 1
    assert latest["temp_c"] == pytest.approx(21.5)

    device = store.get_device(DEVICE)
    assert device["online"] == 1
    assert device["last_seen"] is not None
    assert device["fw"] == "1.2.0"


@pytest.mark.xfail(
    reason="SQLite UNIQUE(device_id, ts, seq) treats NULL seq values as distinct, "
           "so telemetry without a seq is never deduplicated (known limitation)",
    strict=True,
)
def test_telemetry_without_seq_is_deduped(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    rec = _record()
    rec["seq"] = None
    store.store_telemetry([rec])
    store.store_telemetry([rec])

    assert db.one("SELECT COUNT(*) AS n FROM telemetry")["n"] == 1


def test_store_telemetry_updates_device_metadata(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    rec = _record()
    rec["cfg_version"] = 7

    store.store_telemetry([rec])

    assert store.get_device(DEVICE)["cfg_version"] == 7


def test_config_save_and_version_bump(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    initial = store.get_config(DEVICE)
    assert initial["version"] == 1
    assert initial["telemetry_interval_s"] == 10

    cfg = DeviceConfig(telemetry_interval_s=30, temp_offset_c=1.5).model_dump()
    saved = store.save_config(DEVICE, cfg, initial["version"] + 1, "alice")
    assert saved["version"] == 2
    assert saved["telemetry_interval_s"] == 30

    loaded = store.get_config(DEVICE)
    assert loaded["version"] == 2
    assert loaded["temp_offset_c"] == pytest.approx(1.5)
    assert loaded["_updated_by"] == "alice"

    payload = store.device_config_payload(DEVICE)
    assert payload["version"] == 2
    assert not any(k.startswith("_") for k in payload)


def test_retention_purge_removes_old_telemetry(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    old_ts = "2020-01-01T00:00:00.000Z"
    new_ts = store.iso_ago(60)
    store.store_telemetry([_blank_record(DEVICE, old_ts), _blank_record(DEVICE, new_ts)])
    assert db.one("SELECT COUNT(*) AS n FROM telemetry")["n"] == 2

    purged = store.purge_old(1)

    assert purged == 1
    remaining = db.query("SELECT ts FROM telemetry")
    assert [r["ts"] for r in remaining] == [new_ts]


def test_logs_add_query_and_clear(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")
    store.add_log("server", "boot", "service started", level="INFO")
    store.add_log("device", "sensor_error", "BME280 missing", level="WARN", device_id=DEVICE)
    store.add_log("bogus-source", "weird", "source is normalized", level="LOUD")

    device_logs = store.query_logs(source="device")
    assert [r["event"] for r in device_logs] == ["sensor_error"]
    assert device_logs[0]["source"] == "device"
    assert device_logs[0]["level"] == "WARN"

    warn_logs = store.query_logs(level="warn")
    assert [r["event"] for r in warn_logs] == ["sensor_error"]

    # device_id filter includes global (NULL device) rows.
    scoped = store.query_logs(device_id=DEVICE)
    assert {r["event"] for r in scoped} == {"sensor_error", "boot", "weird", "device_register"}

    normalized = store.query_logs(source="server", limit=1)
    assert normalized[0]["source"] == "server"

    assert store.clear_logs(DEVICE) == 2
    assert store.query_logs(source="device") == []

    remaining = db.one("SELECT COUNT(*) AS n FROM system_logs")["n"]
    assert store.clear_logs() == remaining
    assert store.query_logs() == []


def test_rejected_messages_are_recorded(database):  # noqa: ARG001
    store.create_device(DEVICE, name="Store CYD")

    store.log_rejected(DEVICE, "invalid JSON", '{"broken"')

    rows = store.rejected(DEVICE, 10)
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalid JSON"
    # A WARN entry is also written to the system log stream.
    assert any(r["event"] == "telemetry_warn" for r in store.query_logs(source="server"))
