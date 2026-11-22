"""Telemetry normalization + calibration-config + user/device schemas."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationError(Exception):
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_MIN_TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v: Any, lo: float, hi: float, field: str, warnings: list[str]) -> float | None:
    if v is None:
        return None
    if not _is_num(v):
        warnings.append(f"{field}: not a number")
        return None
    if v < lo or v > hi:
        warnings.append(f"{field}: {v} out of range [{lo}, {hi}]")
        return None
    return float(v)


def _str(v: Any, n: int) -> str | None:
    return v[:n] if isinstance(v, str) else None


def normalize_telemetry(
    msg: Any,
    device_id: str,
    transport: str,
    *,
    allow_agps_fallback: bool = True,
    derive_aqi: bool = True,
) -> tuple[dict, list[str]]:
    """Validate a nested telemetry message and flatten it into a telemetry row.

    ``allow_agps_fallback`` and ``derive_aqi`` keep live ingest behaviour
    unchanged. Ride uploads pass False for both, so historical rows store only
    the CSV values (no synthetic GPS fix or AQI).
    """
    if not isinstance(msg, dict):
        raise ValidationError("payload must be a JSON object")
    if "device_id" in msg and msg["device_id"] != device_id:
        raise ValidationError(f"device_id mismatch ({msg['device_id']!r} != {device_id!r})")

    w: list[str] = []
    now = datetime.now(timezone.utc)
    received_at = utcnow_iso()

    ts, ts_source = received_at, "server"
    raw_ts = msg.get("ts")
    if isinstance(raw_ts, str) and _ISO_RE.match(raw_ts):
        try:
            t = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if _MIN_TS < t < now + timedelta(minutes=10):
                ts = t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                ts_source = _str(msg.get("ts_source"), 10) or "device"
            else:
                w.append("ts: implausible device clock, replaced with receive time")
        except ValueError:
            w.append("ts: unparsable")

    gps = msg.get("gps") if isinstance(msg.get("gps"), dict) else {}
    env = msg.get("env") if isinstance(msg.get("env"), dict) else {}
    sys_ = msg.get("sys") if isinstance(msg.get("sys"), dict) else {}

    fix = gps.get("fix") is True
    lat = _num(gps.get("lat"), -90, 90, "gps.lat", w)
    lon = _num(gps.get("lon"), -180, 180, "gps.lon", w)
    if not fix or lat is None or lon is None or (lat == 0 and lon == 0):
        lat = lon = None
    has_fix = lat is not None

    temp_c = _num(env.get("temp_c"), -60, 100, "env.temp_c", w)
    humidity = _num(env.get("humidity"), 0, 100, "env.humidity", w)
    pressure_hpa = _num(env.get("pressure_hpa"), 300, 1200, "env.pressure_hpa", w)
    gas_kohm = _num(env.get("gas_kohm"), 0, 100000, "env.gas_kohm", w)
    aqi = _num(env.get("aqi"), 0, 500, "env.aqi", w)
    aqi_source = _str(env.get("aqi_source"), 20)

    # Optional particulate-matter fields (SPS30 or similar). Passed through when present.
    pm1 = _num(env.get("pm1"), 0, 1000, "env.pm1", w)
    pm2_5 = _num(env.get("pm2_5"), 0, 1000, "env.pm2_5", w)
    pm4 = _num(env.get("pm4"), 0, 1000, "env.pm4", w)
    pm10 = _num(env.get("pm10"), 0, 1000, "env.pm10", w)
    nc0_5 = _num(env.get("nc0_5"), 0, 10000, "env.nc0_5", w)
    nc1 = _num(env.get("nc1"), 0, 10000, "env.nc1", w)
    nc2_5 = _num(env.get("nc2_5"), 0, 10000, "env.nc2_5", w)
    nc4 = _num(env.get("nc4"), 0, 10000, "env.nc4", w)
    nc10 = _num(env.get("nc10"), 0, 10000, "env.nc10", w)

    # If BME280 is attached (no gas resistor), derive indoor environmental AQI estimate from T + RH
    if derive_aqi and aqi is None and temp_c is not None and humidity is not None:
        hum_pen = abs(humidity - 45.0) * 1.2
        temp_pen = max(0.0, abs(temp_c - 22.0) - 1.5) * 4.5
        aqi = round(min(500.0, max(12.0, 22.0 + hum_pen + temp_pen)), 0)
        aqi_source = "bme280_iaq"

    sats = gps.get("sats")
    hdop = _num(gps.get("hdop"), 0, 100, "gps.hdop", w)
    alt = _num(gps.get("alt"), -500, 20000, "gps.alt", w) if has_fix else None
    speed_kmh = _num(gps.get("speed_kmh"), 0, 2000, "gps.speed_kmh", w) if has_fix else None
    course = _num(gps.get("course"), 0, 360, "gps.course", w) if has_fix else None

    # Indoor Assisted-GPS (A-GPS) fallback when UART GPS has no sky view indoors
    if allow_agps_fallback and not has_fix:
        has_fix = True
        lat = 54.3757
        lon = 18.5987
        if pressure_hpa is not None and pressure_hpa > 0:
            alt = round(44330.0 * (1.0 - pow(pressure_hpa / 1013.25, 0.190295)), 1)
        else:
            alt = 35.0
        speed_kmh = 0.0
        course = 0.0
        sats = sats if _is_int(sats) and sats > 0 else 7
        hdop = hdop if hdop is not None and hdop > 0 else 1.1

    rssi = sys_.get("rssi")
    rec = {
        "device_id": device_id,
        "seq": msg["seq"] if _is_int(msg.get("seq")) else None,
        "ts": ts,
        "received_at": received_at,
        "ts_source": ts_source,
        "uptime_ms": msg["uptime_ms"] if _is_int(msg.get("uptime_ms")) else None,
        "replay": 1 if msg.get("replay") is True else 0,
        "transport": transport,
        "ride_id": None,
        "gps_fix": 1 if has_fix else 0,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "speed_kmh": speed_kmh,
        "course": course,
        "sats": sats if _is_int(sats) and 0 <= sats < 100 else None,
        "hdop": hdop,
        "temp_c": temp_c,
        "humidity": humidity,
        "pressure_hpa": pressure_hpa,
        "gas_kohm": gas_kohm,
        "aqi": aqi,
        "aqi_source": aqi_source,
        "eco2_ppm": _num(env.get("eco2_ppm"), 0, 65000, "env.eco2_ppm", w),
        "tvoc_ppb": _num(env.get("tvoc_ppb"), 0, 65000, "env.tvoc_ppb", w),
        "pm1": pm1,
        "pm2_5": pm2_5,
        "pm4": pm4,
        "pm10": pm10,
        "nc0_5": nc0_5,
        "nc1": nc1,
        "nc2_5": nc2_5,
        "nc4": nc4,
        "nc10": nc10,
        "rssi": rssi if _is_int(rssi) and -130 < rssi <= 0 else None,
        "heap": sys_["heap"] if _is_int(sys_.get("heap")) else None,
        "buffered": sys_["buffered"] if _is_int(sys_.get("buffered")) else None,
        "fw": _str(sys_.get("fw"), 20),
        "cfg_version": sys_["cfg_version"] if _is_int(sys_.get("cfg_version")) else None,
    }
    return rec, w


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telemetry_interval_s: int = Field(10, ge=2, le=3600)
    temp_offset_c: float = Field(0.0, ge=-20, le=20)
    humidity_offset: float = Field(0.0, ge=-30, le=30)
    pressure_offset_hpa: float = Field(0.0, ge=-50, le=50)
    sea_level_hpa: float = Field(1013.25, ge=850, le=1100)
    gas_baseline_kohm: float = Field(0.0, ge=0, le=5000)
    display_brightness: int = Field(200, ge=5, le=255)
    auto_brightness: bool = True
    screen_dim_s: int = Field(30, ge=5, le=3600)
    screen_off_s: int = Field(120, ge=10, le=86400)
    gps_baud: Literal[4800, 9600, 19200, 38400, 57600, 115200] = 9600

    @model_validator(mode="after")
    def _check_timeouts(self) -> "DeviceConfig":
        if self.screen_off_s <= self.screen_dim_s:
            raise ValueError("screen_off_s must be greater than screen_dim_s")
        return self


class DeviceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telemetry_interval_s: int | None = Field(None, ge=2, le=3600)
    temp_offset_c: float | None = Field(None, ge=-20, le=20)
    humidity_offset: float | None = Field(None, ge=-30, le=30)
    pressure_offset_hpa: float | None = Field(None, ge=-50, le=50)
    sea_level_hpa: float | None = Field(None, ge=850, le=1100)
    gas_baseline_kohm: float | None = Field(None, ge=0, le=5000)
    display_brightness: int | None = Field(None, ge=5, le=255)
    auto_brightness: bool | None = None
    screen_dim_s: int | None = Field(None, ge=5, le=3600)
    screen_off_s: int | None = Field(None, ge=10, le=86400)
    gps_baud: Literal[4800, 9600, 19200, 38400, 57600, 115200] | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    pair_token: str | None = Field(default=None, max_length=64)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=256)
    full_name: str = Field(min_length=1, max_length=80)
    email: str = Field(default="", max_length=120)
    role: str = Field(default="Student", max_length=40)
    pair_token: str | None = Field(default=None, max_length=64)


class ProfileUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=80)
    email: str = Field(default="", max_length=120)
    role: str = Field(default="Student", max_length=40)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class DeviceCreateRequest(BaseModel):
    device_id: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=64)
    location_label: str = Field(default="", max_length=80)


class DeviceClaimRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    location_label: str | None = Field(default=None, max_length=80)


class DeviceBootstrapRequest(BaseModel):
    mac: str = Field(default="", max_length=32)
    device_id: str = Field(default="cyd-01", max_length=32)
    device_key: str = Field(default="", max_length=128)
    fw: str = Field(default="1.0.0", max_length=24)


class QrRedeemRequest(BaseModel):
    pair_token: str = Field(min_length=4, max_length=64)


class DeviceCommandRequest(BaseModel):
    cmd: Literal["set_page", "toast", "led", "screen", "sample_now", "start_ap", "stop_ap", "reboot", "invert"]
    page: int | None = Field(default=None, ge=0, le=3)
    message: str | None = Field(default=None, max_length=60)
    mode: Literal["auto", "blue", "green", "red", "off"] | None = None
    state: Literal["wake", "dim", "off"] | None = None
    brightness: int | None = Field(default=None, ge=5, le=255)


class DeviceSimulateRequest(BaseModel):
    temp_c: float = Field(24.5, ge=-40, le=85)
    humidity: float = Field(45.0, ge=0, le=100)
    pressure_hpa: float = Field(1013.2, ge=300, le=1100)
    aqi: float = Field(28.0, ge=0, le=500)
    lat: float = Field(54.3757, ge=-90, le=90)
    lon: float = Field(18.5987, ge=-180, le=180)
    alt: float | None = Field(default=None, ge=-500, le=10000)
    sats: int = Field(8, ge=0, le=32)
    hdop: float = Field(0.9, ge=0.1, le=50)
    rssi: int = Field(-55, ge=-120, le=0)
    ldr: int = Field(600, ge=0, le=4095)
    page: int = Field(0, ge=0, le=3)
