"""End-to-end ride recorder tests: CSV upload, idempotency, read API and export.

Self-contained by design: env values use ``setdefault`` so an existing
``conftest.py`` (CI) wins, and the app is imported only after those values are in
place. The module also works standalone without the CI conftest.
"""
from __future__ import annotations

import atexit
import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = Path(tempfile.mkdtemp(prefix="wsb-rides-tests-"))
atexit.register(shutil.rmtree, TMP_DIR, ignore_errors=True)

os.environ.setdefault("DB_PATH", str(TMP_DIR / "test.db"))
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("MQTT_PASS", "test-mqtt-pass")
os.environ.setdefault("STATIC_DIR", str(SERVER_DIR / "static"))
os.environ.setdefault("MOSQUITTO_PASSWD_FILE", str(TMP_DIR / "mosquitto_passwd"))

sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, mqtt_bridge, store  # noqa: E402
from app.main import (  # noqa: E402
    CSV_COLS,
    MAX_RIDE_BYTES,
    MAX_RIDE_ROWS,
    RIDE_CSV_COLUMNS,
    app,
)

# app.config may already be imported by an earlier test module, so also point
# the cached store constant at the same temp file.
store.MOSQUITTO_PASSWD_FILE = os.environ["MOSQUITTO_PASSWD_FILE"]

HEADER = ",".join(RIDE_CSV_COLUMNS)


def make_csv(*rows: dict) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RIDE_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


GOOD_CSV = make_csv(
    {
        "ts": "2026-01-01T10:00:00Z", "lat": "54.370000", "lon": "18.600000", "alt": "30.5",
        "speed_kmh": "12.3", "sats": "8", "hdop": "0.9", "temp_c": "20.0", "humidity": "45.0",
        "pressure_hpa": "1013.2", "pm1": "4.0", "pm2_5": "10.0", "pm4": "12.0", "pm10": "15.0",
        "nc0_5": "1.0", "nc1": "2.0", "nc2_5": "3.0", "nc4": "4.0", "nc10": "5.0", "aqi": "25",
        "gas_kohm": "30.0", "eco2_ppm": "410", "tvoc_ppb": "120",
    },
    {
        "ts": "2026-01-01T10:01:00Z", "lat": "54.371000", "lon": "18.601000", "alt": "31.0",
        "speed_kmh": "13.0", "sats": "9", "hdop": "0.8", "temp_c": "21.0", "humidity": "46.0",
        "pressure_hpa": "1012.8", "pm2_5": "20.0", "pm10": "25.0", "aqi": "28",
    },
    {
        # No GPS fix: coordinates and GPS-derived fields stay empty.
        "ts": "2026-01-01T10:02:00Z", "sats": "0", "temp_c": "22.0", "humidity": "47.0",
        "pressure_hpa": "1012.5", "aqi": "30",
    },
    {
        "ts": "2026-01-01T10:03:00Z", "lat": "54.373000", "lon": "18.603000", "alt": "32.0",
        "speed_kmh": "14.1", "sats": "7", "hdop": "1.1", "temp_c": "23.0", "humidity": "48.0",
        "pressure_hpa": "1012.0", "pm1": "5.0", "pm2_5": "30.0", "pm4": "32.0", "pm10": "35.0",
        "nc0_5": "2.0", "nc1": "3.0", "nc2_5": "4.0", "nc4": "5.0", "nc10": "6.0", "aqi": "32",
        "gas_kohm": "31.0", "eco2_ppm": "420", "tvoc_ppb": "130",
    },
)


@pytest.fixture(scope="session")
def client():
    with patch.object(mqtt_bridge, "start", lambda: None), \
            patch.object(mqtt_bridge, "stop", lambda: None), \
            patch.object(mqtt_bridge, "publish_config", lambda *a, **k: False), \
            patch.object(mqtt_bridge, "publish_command", lambda *a, **k: False):
        with TestClient(app) as c:
            store.upsert_user("tester", "password123", "Test User")
            resp = c.post("/api/auth/login", json={"username": "tester", "password": "password123"})
            assert resp.status_code == 200, resp.text
            yield c


def make_device(name: str = "Ride Test Device") -> tuple[str, str]:
    device_id = f"test-{uuid.uuid4().hex[:8]}"
    key = store.create_device(device_id, name)
    return device_id, key


def upload(client, device_id: str, key: str, body: str, filename: str = "ride_20260101_100000.csv"):
    return client.post(
        f"/api/devices/{device_id}/rides",
        content=body.encode(),
        headers={
            "Content-Type": "text/csv",
            "X-Device-Id": device_id,
            "X-Device-Key": key,
            "X-Filename": filename,
        },
    )


def _plain_record(device_id: str, ts: str, lat: float | None = 54.37, lon: float | None = 18.61) -> dict:
    row = {col: None for col in store.TELEMETRY_COLS}
    row.update(device_id=device_id, ts=ts, received_at=ts, transport="upload",
               gps_fix=1 if lat is not None and lon is not None else 0, lat=lat, lon=lon,
               replay=1)
    return row


def test_upload_and_ride_read_model(client):
    device_id, key = make_device()

    resp = upload(client, device_id, key, GOOD_CSV)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["duplicate"] is False
    assert body["samples"] == 4
    assert body["inserted"] == 4
    assert body["rejected"] == 0
    assert body["warnings"] == 0
    assert body["duration_s"] == 180.0
    assert body["distance_m"] > 0
    ride_id = body["ride_id"]
    assert len(ride_id) == 32

    listing = client.get(f"/api/devices/{device_id}/rides").json()
    assert isinstance(listing, list) and len(listing) == 1
    summary = listing[0]
    assert summary["id"] == ride_id
    assert summary["filename"] == "ride_20260101_100000.csv"
    assert summary["started_at"] == "2026-01-01T10:00:00.000Z"
    assert summary["ended_at"] == "2026-01-01T10:03:00.000Z"
    assert summary["sample_count"] == 4
    assert summary["stats"]["pm2_5"]["avg"] == 20.0
    assert summary["stats"]["pm2_5"]["max"] == 30.0
    assert summary["stats"]["pm10"]["avg"] == 25.0
    assert summary["stats"]["fixes"] == 3

    detail = client.get(f"/api/rides/{ride_id}")
    assert detail.status_code == 200, detail.text
    ride = detail.json()
    assert ride["device_id"] == device_id
    assert ride["sample_count"] == 4
    assert ride["stats"]["samples"] == 4
    assert ride["stats"]["fixes"] == 3
    assert ride["stats"]["temp_c"]["max"] == 23.0
    assert ride["track"] is not None and len(ride["track"]) == 3
    assert all(p["lat"] is not None and p["lon"] is not None for p in ride["track"])
    assert ride["device"]["id"] == device_id
    assert ride["config"] and ride["config"]["version"] >= 1

    telemetry = client.get(
        f"/api/devices/{device_id}/telemetry?hours=8760&limit=100&order=asc"
    ).json()
    assert len(telemetry) == 4
    assert telemetry[0]["ride_id"] == ride_id
    assert telemetry[0]["transport"] == "upload"
    assert telemetry[0]["replay"] == 1
    assert telemetry[0]["pm2_5"] == 10.0
    assert telemetry[0]["gas_kohm"] == 30.0
    assert telemetry[0]["eco2_ppm"] == 410.0
    assert telemetry[0]["tvoc_ppb"] == 120.0
    assert telemetry[2]["gps_fix"] == 0 and telemetry[2]["lat"] is None

    # Ride rows never feed the live series/stats streams.
    assert store.series(device_id, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", 3600) == []
    assert store.latest_telemetry(device_id) is None
    assert store.track(device_id, "2026-01-01T00:00:00Z", 100) == []

    export = client.get(f"/api/rides/{ride_id}/export.csv")
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    assert "\r\n" in export.text  # RFC-4180 line endings
    assert export.text.split("\r\n")[0].split(",") == CSV_COLS
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 4
    assert set(CSV_COLS) <= set(rows[0].keys())
    assert all(r["ride_id"] == ride_id for r in rows)
    assert [r["pm2_5"] for r in rows if r["pm2_5"]] == ["10.0", "20.0", "30.0"]
    assert [r["gas_kohm"] for r in rows if r["gas_kohm"]] == ["30.0", "31.0"]
    # The no-fix row is preserved but has no coordinates.
    assert sum(1 for r in rows if r["lat"] == "") == 1


def test_reupload_is_idempotent_and_keeps_rejected_count(client):
    device_id, key = make_device("Idempotent Device")
    mixed = make_csv(
        {"ts": "2026-01-01T12:00:00Z", "lat": "54.37", "lon": "18.60", "temp_c": "20"},
        {"ts": "bogus", "lat": "54.37", "lon": "18.60", "temp_c": "20"},
    )

    first = upload(client, device_id, key, mixed).json()
    assert first["samples"] == 1
    assert first["rejected"] == 1

    again = upload(client, device_id, key, mixed, filename="renamed.csv")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["duplicate"] is True
    assert body["ride_id"] == first["ride_id"]
    assert body["inserted"] == 0
    assert body["samples"] == 1
    assert body["rejected"] == 1  # persisted from the first upload
    assert len(client.get(f"/api/devices/{device_id}/rides").json()) == 1

    rows = client.get(f"/api/devices/{device_id}/telemetry?hours=8760&limit=100").json()
    assert len(rows) == 1


def test_multipart_upload(client):
    device_id, key = make_device("Multipart Device")
    resp = client.post(
        f"/api/devices/{device_id}/rides",
        files={"file": ("multipart_ride.csv", GOOD_CSV.encode(), "text/csv")},
        headers={"X-Device-Id": device_id, "X-Device-Key": key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["samples"] == 4
    assert body["duplicate"] is False


def test_invalid_csv_is_rejected(client):
    device_id, key = make_device("Bad CSV Device")

    missing_header = "ts,lat,lon\n2026-01-01T10:00:00Z,54.37,18.60\n"
    resp = upload(client, device_id, key, missing_header)
    assert resp.status_code == 400
    assert "missing columns" in str(resp.json()["detail"])

    bad_rows = make_csv({"ts": "not-a-timestamp", "lat": "54.37", "lon": "18.60"})
    resp = upload(client, device_id, key, bad_rows)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["message"] == "no valid rows in CSV"
    assert detail["errors"][0]["row"] == 1
    assert "ts" in detail["errors"][0]["error"]

    partial = make_csv(
        {"ts": "2026-01-01T12:00:00Z", "lat": "54.37", "lon": "18.60", "temp_c": "20",
         "humidity": "40", "pressure_hpa": "1000", "aqi": "20"},
        {"ts": "bogus", "lat": "54.37", "lon": "18.60", "temp_c": "20",
         "humidity": "40", "pressure_hpa": "1000", "aqi": "20"},
    )
    resp = upload(client, device_id, key, partial)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["samples"] == 1
    assert body["rejected"] == 1

    many_bad = make_csv(
        *[{"ts": f"bad-{i}", "lat": "54.37", "lon": "18.60"} for i in range(12)]
    )
    resp = upload(client, device_id, key, many_bad)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["rejected"] == 12
    assert len(detail["errors"]) == 10

    assert upload(client, device_id, key, "not csv at all").status_code == 400


def test_out_of_range_values_are_counted_as_warnings(client):
    device_id, key = make_device("Warning Device")
    body = make_csv({
        "ts": "2026-01-01T10:00:00Z", "lat": "54.37", "lon": "18.60",
        "temp_c": "250", "humidity": "45", "pressure_hpa": "1000",
    })
    resp = upload(client, device_id, key, body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["samples"] == 1
    assert data["warnings"] >= 1
    rows = client.get(f"/api/devices/{device_id}/telemetry?hours=8760&limit=10").json()
    assert rows[0]["temp_c"] is None
    assert rows[0]["humidity"] == 45.0


def test_non_finite_sats_is_a_400(client):
    device_id, key = make_device("NaN Device")
    for value in ("nan", "inf", "-inf"):
        body = make_csv({"ts": "2026-01-01T10:00:00Z", "lat": "54.37", "lon": "18.60",
                         "sats": value})
        resp = upload(client, device_id, key, body)
        assert resp.status_code == 400, resp.text
        assert "not a finite number" in str(resp.json()["detail"])


def test_distance_skips_gps_jumps(client):
    device_id, key = make_device("Jump Device")
    body = make_csv(
        {"ts": "2026-01-01T10:00:00Z", "lat": "54.3700", "lon": "18.6000", "sats": "9", "hdop": "0.8"},
        {"ts": "2026-01-01T10:01:00Z", "lat": "54.3701", "lon": "18.6001", "sats": "9", "hdop": "0.8"},
        # ~111 km in one minute: a GPS jump that must not inflate distance.
        {"ts": "2026-01-01T10:02:00Z", "lat": "55.3700", "lon": "18.6000", "sats": "9", "hdop": "0.8"},
    )
    resp = upload(client, device_id, key, body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["distance_m"] < 100


def test_upload_limits(client):
    device_id, key = make_device("Limit Device")

    big_body = b"x" * (MAX_RIDE_BYTES + 1)
    resp = client.post(
        f"/api/devices/{device_id}/rides",
        content=big_body,
        headers={"Content-Type": "text/csv", "X-Device-Id": device_id, "X-Device-Key": key},
    )
    assert resp.status_code == 413

    row = "2026-01-01T00:00:00Z" + "," * (len(RIDE_CSV_COLUMNS) - 1) + "\n"
    many_rows = HEADER + "\n" + row * (MAX_RIDE_ROWS + 1)
    resp = upload(client, device_id, key, many_rows)
    assert resp.status_code == 413
    assert "rows" in str(resp.json()["detail"])


def test_live_endpoints_ignore_ride_rows(client):
    device_id, key = make_device("Live Isolation Device")
    ts = store.iso_ago(60)
    duplicate, ride = store.save_ride(
        device_id, "live.csv", f"sha-{uuid.uuid4().hex}",
        [_plain_record(device_id, ts, 54.37, 18.61)],
    )
    assert duplicate is False

    detail = client.get(f"/api/devices/{device_id}").json()
    assert detail["latest"] is None
    assert detail["last_fix"] is None
    assert client.get(f"/api/devices/{device_id}/track?hours=24").json() == []
    assert client.get(f"/api/devices/{device_id}/series?hours=1&points=10").json()["points"] == []
    assert client.get(f"/api/rides/{ride['id']}").json()["track"][0]["lat"] == 54.37


def test_retention_removes_ride_metadata_with_telemetry(client):
    device_id, key = make_device("Retention Device")
    old_ts = "2020-01-01T00:00:00Z"
    duplicate, ride = store.save_ride(
        device_id, "old.csv", f"sha-{uuid.uuid4().hex}",
        [_plain_record(device_id, old_ts, 54.37, 18.61)],
    )
    assert duplicate is False

    purged = store.purge_old(1)
    assert purged >= 1
    assert store.get_ride(ride["id"]) is None
    assert store.list_rides(device_id) == []
    assert db.one("SELECT COUNT(*) AS n FROM telemetry WHERE device_id = ?", (device_id,))["n"] == 0


def test_ride_404s(client):
    assert client.get("/api/rides/unknown-ride").status_code == 404
    assert client.get("/api/rides/unknown-ride/export.csv").status_code == 404
    assert client.delete("/api/rides/unknown-ride").status_code == 404
    assert client.get("/api/devices/unknown-device/rides").status_code == 404


def test_mosquitto_passwd_file_is_not_production():
    assert store.MOSQUITTO_PASSWD_FILE == os.environ["MOSQUITTO_PASSWD_FILE"]
    assert store.MOSQUITTO_PASSWD_FILE != "/var/lib/wsb/mosquitto_passwd"


def test_legacy_schema_migrates_rides_columns():
    legacy_sql = """
    CREATE TABLE telemetry (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL,
      seq INTEGER,
      ts TEXT NOT NULL,
      received_at TEXT NOT NULL,
      ts_source TEXT,
      uptime_ms INTEGER,
      replay INTEGER NOT NULL DEFAULT 0,
      transport TEXT,
      gps_fix INTEGER NOT NULL DEFAULT 0,
      lat REAL, lon REAL, alt REAL, speed_kmh REAL, course REAL, sats INTEGER, hdop REAL,
      temp_c REAL, humidity REAL, pressure_hpa REAL, gas_kohm REAL,
      aqi REAL, aqi_source TEXT, eco2_ppm REAL, tvoc_ppb REAL,
      rssi INTEGER, heap INTEGER, buffered INTEGER, fw TEXT, cfg_version INTEGER
    );
    CREATE TABLE rides (
      id TEXT PRIMARY KEY,
      device_id TEXT NOT NULL,
      filename TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      started_at TEXT,
      ended_at TEXT,
      duration_s REAL,
      distance_m REAL,
      sample_count INTEGER NOT NULL DEFAULT 0,
      stats_json TEXT,
      uploaded_at TEXT NOT NULL,
      UNIQUE(device_id, sha256)
    );
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(legacy_sql)
    db.apply_schema(conn)

    telemetry_cols = {r["name"] for r in conn.execute("PRAGMA table_info(telemetry)")}
    assert {"ride_id", "pm1", "pm2_5", "pm10", "nc0_5", "nc10"} <= telemetry_cols
    ride_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rides)")}
    assert "rejected_count" in ride_cols
    indexes = {r["name"] for r in conn.execute("PRAGMA index_list(telemetry)")}
    assert "ix_telemetry_ride_id" in indexes
    conn.close()


def test_device_auth_and_mismatch(client):
    device_a, key_a = make_device("Device A")
    device_b, _ = make_device("Device B")

    resp = client.post(
        f"/api/devices/{device_b}/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": key_a},
    )
    assert resp.status_code == 403

    resp = client.post(
        f"/api/devices/{device_a}/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": "wrong"},
    )
    assert resp.status_code == 401

    resp = client.post(f"/api/devices/{device_a}/rides",
                       content=GOOD_CSV.encode(), headers={"Content-Type": "text/csv"})
    assert resp.status_code == 401

    resp = client.post(
        "/api/devices/does-not-exist/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": key_a},
    )
    assert resp.status_code == 403

    anonymous = TestClient(app)
    assert anonymous.get(f"/api/devices/{device_a}/rides").status_code == 401


def test_delete_ride(client):
    device_id, key = make_device("Delete Device")
    ride_id = upload(client, device_id, key, GOOD_CSV).json()["ride_id"]

    deleted = client.delete(f"/api/rides/{ride_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_samples"] == 4
    assert client.get(f"/api/rides/{ride_id}").status_code == 404
    assert client.get(f"/api/devices/{device_id}/rides").json() == []
    assert client.delete("/api/rides/unknown-ride").status_code == 404
