"""HTTP-level tests for the FastAPI app. Fully offline: no broker, temp DB."""
from __future__ import annotations

import pytest

from app import store
from app.schemas import normalize_telemetry

DEVICE = "cyd-api"
PASSWORD = "s3cret-pass"


def _iso_ago(seconds: int) -> str:
    return store.iso_ago(seconds)


def _telemetry_msg(seq: int = 1, ts: str | None = None) -> dict:
    return {
        "device_id": DEVICE,
        "seq": seq,
        "ts": ts or _iso_ago(30),
        "uptime_ms": 5_000,
        "gps": {"fix": True, "lat": 54.37, "lon": 18.61, "alt": 35.0,
                "speed_kmh": 0.0, "course": 0.0, "sats": 8, "hdop": 0.9},
        "env": {"temp_c": 22.0, "humidity": 45.0, "pressure_hpa": 1013.0,
                "aqi": 25.0, "aqi_source": "simulated"},
        "sys": {"rssi": -55, "heap": 100_000, "buffered": 0, "fw": "1.2.0", "cfg_version": 2},
    }


def _register(client, username: str = "alice"):
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": PASSWORD,
            "full_name": "Alice Student",
            "email": "alice@example.com",
            "role": "Student",
        },
    )
    assert response.status_code == 200, response.text
    return response


def _create_device(client, device_id: str = DEVICE) -> dict:
    response = client.post(
        "/api/devices",
        json={"device_id": device_id, "name": "Test CYD", "location_label": "Lab"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_health(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mqtt"] == {"connected": False, "received": 0, "rejected": 0}
    assert body["ws_clients"] == 0


def test_register_sets_cookie_and_me_works(client):
    response = _register(client)

    assert response.json()["user"] == "alice"
    assert response.json()["profile"]["full_name"] == "Alice Student"
    assert "wsb_session" in client.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"] == "alice"


def test_register_duplicate_username_conflicts(client):
    _register(client)

    response = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": PASSWORD, "full_name": "Other"},
    )

    assert response.status_code == 409


def test_login_logout_login_cycle(client):
    _register(client)
    assert client.post("/api/auth/logout").json() == {"ok": True}

    assert client.get("/api/auth/me").status_code == 401

    bad = client.post("/api/auth/login",
                      json={"username": "alice", "password": "wrong-password"})
    assert bad.status_code == 401

    good = client.post("/api/auth/login",
                       json={"username": "alice", "password": PASSWORD})
    assert good.status_code == 200
    assert good.json()["ok"] is True
    assert client.get("/api/auth/me").json()["user"] == "alice"


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/devices").status_code == 401
    assert client.post("/api/devices", json={"name": "x"}).status_code == 401
    assert client.get("/api/logs").status_code == 401
    assert client.post("/api/ingest", json={}).status_code == 401


def test_create_and_list_devices(client, database):  # noqa: ARG001
    _register(client)
    created = _create_device(client)

    assert created["device"]["id"] == DEVICE
    assert created["device"]["owner_username"] == "alice"
    assert created["device_key"]
    assert created["pair_token"]
    assert created["claim_code"]
    assert "key_hash" not in created["device"]

    assert store.verify_device_key(DEVICE, created["device_key"]) is True

    listed = client.get("/api/devices")
    assert listed.status_code == 200
    devices = listed.json()
    assert [d["id"] for d in devices] == [DEVICE]
    assert devices[0]["latest"] is None


def test_create_duplicate_device_conflicts(client):
    _register(client)
    _create_device(client)

    response = client.post("/api/devices", json={"device_id": DEVICE, "name": "Dup"})

    assert response.status_code == 409


def test_put_config_bumps_version(client):
    _register(client)
    _create_device(client)

    initial = client.get(f"/api/devices/{DEVICE}/config").json()
    assert initial["version"] == 1

    response = client.put(f"/api/devices/{DEVICE}/config", json={"telemetry_interval_s": 30})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == initial["version"] + 1
    assert body["telemetry_interval_s"] == 30
    assert body["pushed"] is False  # MQTT publish is stubbed out in tests

    updated = client.get(f"/api/devices/{DEVICE}/config").json()
    assert updated["version"] == 2
    assert updated["telemetry_interval_s"] == 30


def test_put_config_rejects_invalid_timeouts(client):
    _register(client)
    _create_device(client)

    response = client.put(
        f"/api/devices/{DEVICE}/config",
        json={"screen_dim_s": 60, "screen_off_s": 50},
    )

    # DeviceConfig requires screen_off_s > screen_dim_s.
    assert response.status_code == 422


def test_put_config_unknown_device(client):
    _register(client)

    response = client.put("/api/devices/missing-device/config", json={"telemetry_interval_s": 30})

    assert response.status_code == 404


def test_csv_export(client):
    _register(client)
    _create_device(client)
    rec, warnings = normalize_telemetry(_telemetry_msg(), DEVICE, "http")
    assert warnings == []
    assert store.store_telemetry([rec]) == 1

    response = client.get(f"/api/devices/{DEVICE}/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    lines = response.text.strip().splitlines()
    assert lines[0].split(",")[:3] == ["ts", "received_at", "seq"]
    assert len(lines) == 2
    assert lines[1].split(",")[2] == "1"


def test_ingest_requires_valid_device_key(client):
    _register(client)
    created = _create_device(client)

    response = client.post(
        "/api/ingest",
        headers={"X-Device-Id": DEVICE, "X-Device-Key": "wrong-key"},
        json=_telemetry_msg(),
    )
    assert response.status_code == 401

    headers = {"X-Device-Id": DEVICE, "X-Device-Key": created["device_key"]}
    payload = _telemetry_msg(seq=1)
    first = client.post("/api/ingest", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 1
    assert first.json()["inserted"] == 1

    duplicate = client.post("/api/ingest", headers=headers, json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["inserted"] == 0
    assert duplicate.json()["duplicates"] == 1


def test_ingest_rejects_device_id_mismatch(client):
    _register(client)
    created = _create_device(client)
    payload = _telemetry_msg()
    payload["device_id"] = "some-other-device"

    response = client.post(
        "/api/ingest",
        headers={"X-Device-Id": DEVICE, "X-Device-Key": created["device_key"]},
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert response.json()["rejected"] == 1


def test_telemetry_query_filters_by_device_and_window(client):
    _register(client)
    _create_device(client)
    rec, _ = normalize_telemetry(_telemetry_msg(seq=1), DEVICE, "http")
    store.store_telemetry([rec])

    response = client.get(f"/api/devices/{DEVICE}/telemetry", params={"hours": 1})

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["seq"] == 1

    missing = client.get("/api/devices/unknown/telemetry")
    assert missing.status_code == 404


def test_logs_endpoint_lists_and_clears(client):
    _register(client)
    _create_device(client)

    listed = client.get("/api/logs")
    assert listed.status_code == 200
    assert any(row["event"] == "auth_register" for row in listed.json())

    cleared = client.delete("/api/logs")
    assert cleared.status_code == 200
    assert cleared.json()["ok"] is True
    assert client.get("/api/logs").json() == []


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/auth/profile"])
def test_session_endpoints_require_cookie(client, path):
    assert client.get(path).status_code == 401
