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
    # Invalid coordinates make the record fall back to A-GPS (see the known
    # data-integrity issue documented in test_missing_fix_uses_hardcoded_agps).
    assert rec["gps_fix"] == 1
    assert rec["lat"] == pytest.approx(54.3757)
    assert rec["lon"] == pytest.approx(18.5987)


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
    # Current behavior: strings that do not match the strict ISO regex are not
    # warned about, they are simply ignored in favor of the receive time.
    msg = _payload(ts="01/05/2026 10:00")

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["ts"] == rec["received_at"]
    assert rec["ts_source"] == "server"


def test_missing_fix_uses_hardcoded_agps():
    """KNOWN DATA-INTEGRITY ISSUE (asserted as-is, do not "fix" here).

    normalize_telemetry() replaces a missing GPS fix with a hard-coded
    A-GPS fallback at the Merito campus (54.3757, 18.5987) and reports
    gps_fix=1. Consuming code therefore cannot distinguish a real fix from
    a fabricated one. The rides branch owns any change to this behavior.
    """
    msg = _payload()
    msg["gps"] = {"fix": False, "lat": None, "lon": None}
    msg["env"] = {**msg["env"], "pressure_hpa": 1000.0}

    rec, warnings = normalize_telemetry(msg, DEVICE, "mqtt")

    assert warnings == []
    assert rec["gps_fix"] == 1
    assert rec["lat"] == pytest.approx(54.3757)
    assert rec["lon"] == pytest.approx(18.5987)
    assert rec["speed_kmh"] == 0.0
    assert rec["course"] == 0.0
    assert rec["sats"] == 7
    assert rec["hdop"] == pytest.approx(1.1)
    # Altitude is barometric when pressure is present.
    expected_alt = round(44330.0 * (1.0 - pow(1000.0 / 1013.25, 0.190295)), 1)
    assert rec["alt"] == pytest.approx(expected_alt)


def test_missing_fix_without_pressure_defaults_altitude():
    msg = _payload()
    msg.pop("gps")
    msg["env"] = {"temp_c": 20.0, "humidity": 40.0}

    rec, _ = normalize_telemetry(msg, DEVICE, "mqtt")

    assert rec["gps_fix"] == 1
    assert rec["lat"] == pytest.approx(54.3757)
    assert rec["alt"] == pytest.approx(35.0)


def test_zero_zero_coordinates_are_treated_as_no_fix():
    msg = _payload()
    msg["gps"] = {**msg["gps"], "lat": 0.0, "lon": 0.0}

    rec, _ = normalize_telemetry(msg, DEVICE, "mqtt")

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


def test_dedupe_key_is_device_ts_seq():
    first, _ = normalize_telemetry(_payload(), DEVICE, "http")
    second, _ = normalize_telemetry(_payload(), DEVICE, "mqtt")

    key = lambda rec: (rec["device_id"], rec["ts"], rec["seq"])  # noqa: E731

    assert key(first) == key(second) == (DEVICE, FIXED_TS, 42)

    other_seq, _ = normalize_telemetry(_payload(seq=43), DEVICE, "http")
    assert key(other_seq) != key(first)
