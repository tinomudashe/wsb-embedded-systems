"""Unit tests for telemetry normalization (app.schemas.normalize_telemetry)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import ValidationError, normalize_telemetry

DEVICE = "cyd-schema"
FIXED_TS = "2026-05-01T10:00:00.000Z"


def _payload(**overrides):
    msg = {
        "device_id": DEVICE,
        "seq": 42,
        "ts": FIXED_TS,
        "ts_source": "gps",
        "uptime_ms": 123_456,
        "replay": False,
        "gps": {
            "fix": True,
            "lat": 54.37,
            "lon": 18.61,
            "alt": 35.5,
            "speed_kmh": 1.5,
            "course": 90.0,
            "sats": 9,
            "hdop": 0.9,
        },
        "env": {
            "temp_c": 22.5,
            "humidity": 45.0,
            "pressure_hpa": 1013.2,
            "gas_kohm": 30.0,
            "aqi": 28.0,
            "aqi_source": "ens160",
            "eco2_ppm": 410.0,
            "tvoc_ppb": 120.0,
        },
        "sys": {
            "rssi": -55,
            "heap": 198_400,
            "buffered": 0,
            "fw": "1.2.0",
            "cfg_version": 3,
        },
    }
    msg.update(overrides)
    return msg


def test_valid_payload_is_normalized_without_warnings():
    rec, warnings = normalize_telemetry(_payload(), DEVICE, "http")

    assert warnings == []
    assert rec["device_id"] == DEVICE
    assert rec["seq"] == 42
    assert rec["ts"] == FIXED_TS
    assert rec["ts_source"] == "gps"
    assert rec["transport"] == "http"
    assert rec["gps_fix"] == 1
    assert rec["lat"] == pytest.approx(54.37)
    assert rec["lon"] == pytest.approx(18.61)
    assert rec["alt"] == pytest.approx(35.5)
    assert rec["sats"] == 9
    assert rec["temp_c"] == pytest.approx(22.5)
    assert rec["humidity"] == pytest.approx(45.0)
    assert rec["pressure_hpa"] == pytest.approx(1013.2)
    assert rec["aqi"] == pytest.approx(28.0)
    assert rec["aqi_source"] == "ens160"
    assert rec["rssi"] == -55
    assert rec["fw"] == "1.2.0"
    assert rec["cfg_version"] == 3
    assert rec["replay"] == 0


def test_out_of_range_values_warn_and_are_dropped():
    msg = _payload()
    msg["env"] = {
        "temp_c": 250.0,  # > 100
        "humidity": 150.0,  # > 100
        "pressure_hpa": 10.0,  # < 300
        "gas_kohm": 999_999.0,  # > 100000
        "aqi": 999.0,  # > 500
    }
    msg["gps"] = {**msg["gps"], "lat": 123.0, "lon": 200.0, "hdop": 999.0}

    rec, warnings = normalize_telemetry(msg, DEVICE, "http")

    for field in ("env.temp_c", "env.humidity", "env.pressure_hpa", "env.gas_kohm",
                  "env.aqi", "gps.lat", "gps.lon", "gps.hdop"):
        assert any(w.startswith(f"{field}:") for w in warnings), f"missing warning for {field}: {warnings}"

    assert rec["temp_c"] is None
    assert rec["humidity"] is None
    assert rec["pressure_hpa"] is None
    assert rec["gas_kohm"] is None
    assert rec["aqi"] is None


def test_non_numeric_environment_value_warns():
    msg = _payload()
    msg["env"] = {**msg["env"], "temp_c": "warm"}

    rec, warnings = normalize_telemetry(msg, DEVICE, "http")

    assert any(w.startswith("env.temp_c: not a number") for w in warnings)
    assert rec["temp_c"] is None


def test_missing_timestamp_falls_back_to_server_receive_time():
    msg = _payload()
    msg.pop("ts")

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["ts"] == rec["received_at"]
    assert rec["ts_source"] == "server"


def test_implausible_device_clock_is_replaced_and_warned():
    msg = _payload(ts="2019-06-01T00:00:00.000Z")  # before _MIN_TS

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert any("implausible device clock" in w for w in warnings)
    assert rec["ts"] == rec["received_at"]
    assert rec["ts_source"] == "server"


def test_future_timestamp_is_replaced_and_warned():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    msg = _payload(ts=future)

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert any("implausible device clock" in w for w in warnings)
    assert rec["ts"] == rec["received_at"]


def test_malformed_timestamp_silently_falls_back():
    # Current behavior: strings that fail the strict ISO regex get no warning;
    # the parser uses the receive time.
    msg = _payload(ts="01/05/2026 10:00")

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["ts"] == rec["received_at"]
    assert rec["ts_source"] == "server"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "known data-integrity issue: a missing fix is fabricated as a valid "
        "A-GPS position; this test states the expected behavior and will XPASS "
        "when it is fixed"
    ),
)
def test_missing_fix_is_not_synthesized():
    """Desired behavior: no fix must remain no fix (currently xfails)."""
    msg = _payload()
    msg["gps"] = {"fix": False, "lat": None, "lon": None}

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["gps_fix"] == 0
    assert rec["lat"] is None
    assert rec["lon"] is None


def test_missing_fix_current_default_is_agps():
    """Document the current A-GPS default once; this is a known bug.

    A record without a GPS fix is rewritten to gps_fix=1 at 54.3757/18.5987, so
    consumers cannot tell a real fix from a synthetic one. The rides branch owns
    any change to this behavior; the xfail test above describes the target.
    """
    msg = _payload()
    msg["gps"] = {"fix": False, "lat": None, "lon": None}

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["gps_fix"] == 1
    assert rec["lat"] == pytest.approx(54.3757)
    assert rec["lon"] == pytest.approx(18.5987)


def test_device_id_mismatch_raises():
    msg = _payload(device_id="someone-else")

    with pytest.raises(ValidationError, match="device_id mismatch"):
        normalize_telemetry(msg, DEVICE, "http")


def test_non_object_payload_raises():
    with pytest.raises(ValidationError, match="JSON object"):
        normalize_telemetry([{"seq": 1}], DEVICE, "http")
