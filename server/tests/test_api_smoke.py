"""Smoke tests for secondary API surface: tracks, series, rejected data,
commands, simulation, profiles/passwords and QR pairing."""
from __future__ import annotations

import pytest

from app import store
from app.schemas import normalize_telemetry

DEVICE = "cyd-smoke"
PASSWORD = "smoke-pass-123"


def _register(client, username: str = "smoke") -> None:
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "full_name": "Smoke Tester",
            "email": "smoke@example.com",
            "role": "Student",
        },
    )
    assert response.status_code == 200, response.text


def _create_device(client, device_id: str = DEVICE) -> dict:
    response = client.post(
        "/api/devices",
        json={"device_id": device_id, "name": "Smoke CYD", "location_label": "Lab"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _message(seq: int = 1) -> dict:
    return {
        "device_id": DEVICE,
        "seq": seq,
        "ts": store.iso_ago(60),
        "uptime_ms": 1_000,
        "gps": {"fix": True, "lat": 54.37, "lon": 18.61, "alt": 35.0,
                "speed_kmh": 1.0, "course": 90.0, "sats": 8, "hdop": 0.9},
        "env": {"temp_c": 21.5, "humidity": 44.0, "pressure_hpa": 1012.0,
                "aqi": 30.0, "aqi_source": "ens160"},
        "sys": {"rssi": -55, "heap": 100_000, "buffered": 0, "fw": "1.2.0", "cfg_version": 2},
    }


def test_track_and_series_smoke(client):
    _register(client)
    _create_device(client)
    rec, warnings = normalize_telemetry(_message(), DEVICE, "http")
    assert warnings == []
    assert store.store_telemetry([rec]) == 1

    track = client.get(f"/api/devices/{DEVICE}/track", params={"hours": 1})
    assert track.status_code == 200, track.text
    points = track.json()
    assert len(points) == 1
    assert points[0]["lat"] == pytest.approx(54.37)

    series = client.get(f"/api/devices/{DEVICE}/series",
                        params={"hours": 1, "points": 120})
    assert series.status_code == 200, series.text
    body = series.json()
    assert body["bucket_sec"] >= 1
    assert any(point["n"] >= 1 for point in body["points"])


def test_rejected_command_and_simulate_smoke(client):
    _register(client)
    _create_device(client)

    store.log_rejected(DEVICE, "invalid JSON", '{"broken"')
    rejected = client.get(f"/api/devices/{DEVICE}/rejected")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()[0]["reason"] == "invalid JSON"

    command = client.post(f"/api/devices/{DEVICE}/command",
                          json={"cmd": "set_page", "page": 2})
    assert command.status_code == 200, command.text
    assert command.json()["ok"] is True
    assert command.json()["sent"] is False
    assert command.json()["command"] == {"cmd": "set_page", "page": 2}

    simulated = client.post(f"/api/devices/{DEVICE}/simulate",
                            json={"temp_c": 21.5, "humidity": 44.0})
    assert simulated.status_code == 200, simulated.text
    body = simulated.json()
    assert body["ok"] is True
    assert body["telemetry"]["device_id"] == DEVICE
    assert body["telemetry"]["temp_c"] == pytest.approx(21.5)
    assert body["telemetry"]["gps_fix"] == 1


def test_profile_and_password_smoke(client):
    _register(client, "smoke-profile")

    profile = client.put(
        "/api/auth/profile",
        json={"full_name": "Renamed User", "email": "new@example.com", "role": "Researcher"},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["full_name"] == "Renamed User"
    assert profile.json()["email"] == "new@example.com"

    changed = client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": "new-smoke-pass-456"},
    )
    assert changed.status_code == 200, changed.text

    assert client.post("/api/auth/logout").status_code == 200
    old_login = client.post("/api/auth/login",
                            json={"username": "smoke-profile", "password": PASSWORD})
    assert old_login.status_code == 401
    new_login = client.post("/api/auth/login",
                            json={"username": "smoke-profile", "password": "new-smoke-pass-456"})
    assert new_login.status_code == 200, new_login.text
    assert client.get("/api/auth/me").json()["user"] == "smoke-profile"


def test_qr_and_claim_smoke(client):
    _register(client)
    created = _create_device(client)
    token = created["pair_token"]
    assert token

    info = client.get("/api/auth/qr-info", params={"p": token})
    assert info.status_code == 200, info.text
    assert info.json()["device_id"] == DEVICE
    assert info.json()["owner_username"] == "smoke"

    claimed = client.post("/api/devices/claim", json={"code": token, "name": "Renamed CYD"})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["device"]["name"] == "Renamed CYD"

    redeemed = client.post("/api/auth/qr-redeem", json={"pair_token": token})
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["device_id"] == DEVICE

    missing = client.get("/api/auth/qr-info", params={"p": "does-not-exist"})
    assert missing.status_code == 404
