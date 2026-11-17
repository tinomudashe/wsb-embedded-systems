"""End-to-end ride recorder tests: CSV upload, idempotency, read API and export.

Self-contained by design (the CI agent owns conftest.py): environment variables are
set before importing the app, the database lives in a temporary directory and MQTT
is stubbed out so the suite never touches the network.
"""
from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
TMP_DIR = Path(tempfile.mkdtemp(prefix="wsb-rides-tests-"))
os.environ["DB_PATH"] = str(TMP_DIR / "test.db")
os.environ["JWT_SECRET"] = "test-jwt-secret-not-for-production"
os.environ["MQTT_PASS"] = "test-mqtt-pass"
os.environ["STATIC_DIR"] = str(SERVER_DIR / "static")

sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app import mqtt_bridge, store  # noqa: E402
from app.main import CSV_COLS, app  # noqa: E402

# Never start a real broker/thread during tests.
mqtt_bridge.start = lambda: None  # type: ignore[assignment]
mqtt_bridge.stop = lambda: None  # type: ignore[assignment]
mqtt_bridge.publish_config = lambda *a, **k: False  # type: ignore[assignment]
mqtt_bridge.publish_command = lambda *a, **k: False  # type: ignore[assignment]

HEADER = (
    "ts,lat,lon,alt,speed_kmh,sats,hdop,temp_c,humidity,pressure_hpa,"
    "pm1,pm2_5,pm4,pm10,nc0_5,nc1,nc2_5,nc4,nc10,aqi"
)


def make_csv(*rows: dict) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADER.split(","), lineterminator="\n")
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
    },
)


@pytest.fixture(scope="session")
def client():
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
    assert telemetry[2]["gps_fix"] == 0 and telemetry[2]["lat"] is None

    series = store.series(device_id, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", 3600)
    assert any(p["pm2_5"] is not None for p in series)

    export = client.get(f"/api/rides/{ride_id}/export.csv")
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    assert "\r\n" in export.text  # RFC-4180 line endings
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 4
    assert set(CSV_COLS) <= set(rows[0].keys())
    assert all(r["ride_id"] == ride_id for r in rows)
    assert [r["pm2_5"] for r in rows if r["pm2_5"]] == ["10.0", "20.0", "30.0"]
    # The no-fix row is preserved but has no coordinates.
    assert sum(1 for r in rows if r["lat"] == "") == 1


def test_reupload_is_idempotent(client):
    device_id, key = make_device("Idempotent Device")

    first = upload(client, device_id, key, GOOD_CSV).json()
    again = upload(client, device_id, key, GOOD_CSV, filename="renamed.csv")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["duplicate"] is True
    assert body["ride_id"] == first["ride_id"]
    assert body["inserted"] == 0
    assert body["samples"] == 4
    assert len(client.get(f"/api/devices/{device_id}/rides").json()) == 1

    # Telemetry rows were not duplicated either.
    rows = client.get(f"/api/devices/{device_id}/telemetry?hours=8760&limit=100").json()
    assert len(rows) == 4


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

    # A row with a bad timestamp is skipped, valid rows still land.
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

    assert upload(client, device_id, key, "not csv at all").status_code == 400


def test_device_auth_and_mismatch(client):
    device_a, key_a = make_device("Device A")
    device_b, _ = make_device("Device B")

    # URL device must match the authenticated X-Device-Id (auth succeeds for A, URL says B).
    resp = client.post(
        f"/api/devices/{device_b}/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": key_a},
    )
    assert resp.status_code == 403

    # Wrong key.
    resp = client.post(
        f"/api/devices/{device_a}/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": "wrong"},
    )
    assert resp.status_code == 401

    # No credentials at all.
    resp = client.post(f"/api/devices/{device_a}/rides",
                       content=GOOD_CSV.encode(), headers={"Content-Type": "text/csv"})
    assert resp.status_code == 401

    # Unknown device in the URL, valid credentials for another device -> mismatch first.
    resp = client.post(
        "/api/devices/does-not-exist/rides",
        content=GOOD_CSV.encode(),
        headers={"Content-Type": "text/csv", "X-Device-Id": device_a, "X-Device-Key": key_a},
    )
    assert resp.status_code == 403

    # Session auth is required for the read API.
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
