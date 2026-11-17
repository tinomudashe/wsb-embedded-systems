"""Data-access helpers. Every write that the dashboard should see is pushed to the live hub."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone

import bcrypt

from . import db
from .live import hub
from .schemas import DeviceConfig, utcnow_iso

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://wsbmerito.telemetry.164-92-227-240.sslip.io"
)
MOSQUITTO_PASSWD_FILE = os.environ.get("MOSQUITTO_PASSWD_FILE", "/var/lib/wsb/mosquitto_passwd")

TELEMETRY_COLS = [
    "device_id", "seq", "ts", "received_at", "ts_source", "uptime_ms", "replay", "transport", "ride_id",
    "gps_fix", "lat", "lon", "alt", "speed_kmh", "course", "sats", "hdop",
    "temp_c", "humidity", "pressure_hpa", "gas_kohm", "aqi", "aqi_source", "eco2_ppm", "tvoc_ppb",
    "pm1", "pm2_5", "pm4", "pm10", "nc0_5", "nc1", "nc2_5", "nc4", "nc10", "ps",
    "rssi", "heap", "buffered", "fw", "cfg_version",
]
_INSERT_TELEMETRY = (
    f"INSERT OR IGNORE INTO telemetry ({','.join(TELEMETRY_COLS)}) "
    f"VALUES ({','.join(':' + c for c in TELEMETRY_COLS)})"
)


def iso_ago(seconds: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_in(seconds: float) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_secret(s: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(s.encode(), bcrypt.gensalt(rounds)).decode()


def check_secret(s: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(s.encode(), h.encode())
    except ValueError:
        return False


def _sync_mosquitto_passwd(device_id: str, key: str) -> None:
    """Best-effort update of the shared Mosquitto password file when a new device is registered."""
    exe = shutil.which("mosquitto_passwd")
    if not exe or not os.path.exists(MOSQUITTO_PASSWD_FILE):
        return
    try:
        subprocess.run([exe, "-b", MOSQUITTO_PASSWD_FILE, device_id, key], check=False, timeout=5)
    except Exception:
        pass


# ---------------- devices ----------------

def public_device(d: dict | None) -> dict | None:
    if not d:
        return None
    live_state = None
    if d.get("last_status"):
        try:
            live_state = json.loads(d["last_status"])
        except Exception:
            live_state = None
    out = {k: v for k, v in d.items() if k not in ("key_hash", "last_status")}
    out["online"] = bool(d["online"])
    out["live_state"] = live_state
    return out


def get_device(device_id: str) -> dict | None:
    return db.one("SELECT * FROM devices WHERE id = ?", (device_id,))


def list_devices() -> list[dict]:
    return [public_device(d) for d in db.query("SELECT * FROM devices ORDER BY id")]


def _generate_claim_code(device_id: str, mac: str = "") -> str:
    clean_mac = "".join(c for c in mac.upper() if c in "0123456789ABCDEF")
    if len(clean_mac) >= 4:
        return f"{clean_mac[-4:]}{secrets.token_hex(1).upper()}"
    return secrets.token_hex(3).upper()


def create_device(
    device_id: str,
    name: str | None = None,
    key: str | None = None,
    owner_username: str | None = None,
    location_label: str = "",
    mac_address: str = "",
) -> str:
    key = key or secrets.token_urlsafe(18)
    claim_code = _generate_claim_code(device_id, mac_address)
    db.execute(
        """INSERT INTO devices (id, name, key_hash, owner_username, mac_address, claim_code, location_label)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (device_id, name or device_id, hash_secret(key, 10), owner_username, mac_address or None, claim_code, location_label),
    )
    save_config(device_id, DeviceConfig().model_dump(), 1, owner_username or "system")
    _sync_mosquitto_passwd(device_id, key)
    add_log("server", "device_register", f"Registered device '{device_id}' ({name or device_id}) owner={owner_username or 'system'}", device_id=device_id)
    return key


def delete_device(device_id: str) -> bool:
    cur = db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    _key_cache.clear()
    return cur.rowcount > 0


def set_device_key(device_id: str, key: str) -> None:
    db.execute("UPDATE devices SET key_hash = ? WHERE id = ?", (hash_secret(key, 10), device_id))
    _sync_mosquitto_passwd(device_id, key)
    _key_cache.clear()


_key_cache: dict[str, float] = {}


def verify_device_key(device_id: str | None, key: str | None) -> bool:
    if not device_id or not key:
        return False
    ck = device_id + "\0" + hashlib.sha256(key.encode()).hexdigest()
    if _key_cache.get(ck, 0) > time.time():
        return True
    d = get_device(device_id)
    if not d or not check_secret(key, d["key_hash"]):
        return False
    _key_cache[ck] = time.time() + 600
    return True


def issue_pairing_token(device_id: str) -> dict:
    """Issue a compact 10-character pairing token for ESP32 QR code rendering."""
    dev = get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    claim_code = dev.get("claim_code") or _generate_claim_code(device_id, dev.get("mac_address") or "")
    if not dev.get("claim_code"):
        db.execute("UPDATE devices SET claim_code = ? WHERE id = ?", (claim_code, device_id))
    token = secrets.token_urlsafe(8)[:10]
    now = utcnow_iso()
    exp = iso_in(24 * 3600)
    db.execute(
        "INSERT OR REPLACE INTO pairing_tokens (token, device_id, claim_code, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, device_id, claim_code, now, exp),
    )
    pair_url = f"{PUBLIC_BASE_URL}/?p={token}"
    return {
        "device_id": device_id,
        "claim_code": claim_code,
        "pair_token": token,
        "pair_url": pair_url,
    }


def resolve_pairing_code(code_or_token: str) -> dict | None:
    """Find a device by QR pair_token or 6-char claim_code."""
    c = code_or_token.strip()
    row = db.one(
        "SELECT * FROM pairing_tokens WHERE token = ? OR UPPER(claim_code) = UPPER(?) ORDER BY created_at DESC LIMIT 1",
        (c, c),
    )
    if row:
        dev = get_device(row["device_id"])
        if dev:
            return dev
    return db.one("SELECT * FROM devices WHERE UPPER(claim_code) = UPPER(?) OR id = ?", (c, c))


def claim_device(device_id: str, username: str, name: str | None = None, location_label: str | None = None) -> dict | None:
    dev = get_device(device_id)
    if not dev:
        return None
    new_name = name.strip() if name and name.strip() else dev["name"]
    new_loc = location_label.strip() if location_label is not None else (dev.get("location_label") or "")
    db.execute(
        "UPDATE devices SET owner_username = ?, name = ?, location_label = ? WHERE id = ?",
        (username, new_name, new_loc, device_id),
    )
    updated = public_device(get_device(device_id))
    add_log("server", "device_claim", f"Device '{device_id}' claimed by user '{username}'", device_id=device_id)
    hub.publish({"type": "status", "device": updated})
    return updated


def bootstrap_device(mac: str, requested_id: str, requested_key: str, fw: str) -> dict:
    """Called by the ESP32 at Wi-Fi connect to obtain its pairing QR URL and claim code."""
    clean_mac = mac.strip().upper()
    dev = None
    if requested_id and verify_device_key(requested_id, requested_key):
        dev = get_device(requested_id)
    elif clean_mac:
        dev = db.one("SELECT * FROM devices WHERE UPPER(mac_address) = ?", (clean_mac,))

    new_key = None
    if not dev:
        # If requested_id is cyd-01 and exists, use it; otherwise create a new device for this MAC
        existing = get_device(requested_id) if requested_id else None
        if existing:
            dev = existing
        else:
            suffix = "".join(c for c in clean_mac if c in "0123456789ABCDEF")[-4:].lower() or secrets.token_hex(2)
            dev_id = f"cyd-{suffix}"
            if get_device(dev_id):
                dev = get_device(dev_id)
            else:
                new_key = create_device(dev_id, f"ESP32 CYD ({suffix.upper()})", mac_address=clean_mac)
                dev = get_device(dev_id)

    assert dev is not None
    device_id = dev["id"]
    now = utcnow_iso()
    db.execute(
        """UPDATE devices SET mac_address = COALESCE(?, mac_address), fw = COALESCE(?, fw),
             last_seen = ?, online = 1 WHERE id = ?""",
        (clean_mac or None, fw or None, now, device_id),
    )
    pairing = issue_pairing_token(device_id)
    add_log("device", "bootstrap", f"ESP32 bootstrap OK (MAC={clean_mac or 'N/A'}, FW={fw}, claim_code={pairing['claim_code']})", device_id=device_id)
    out = {
        **pairing,
        "owner_username": dev.get("owner_username"),
        "agps": {
            "lat": 54.3757,
            "lon": 18.5987,
            "alt": 35.0,
            "sats": 7,
            "hdop": 1.1,
        },
    }
    if new_key:
        out["device_key"] = new_key
    return out


def update_device_status(device_id: str, status: dict) -> None:
    online = 1 if status.get("online") else 0
    ip = status.get("ip") if isinstance(status.get("ip"), str) else None
    fw = status.get("fw") if isinstance(status.get("fw"), str) else None
    cv = status.get("cfg_version") if isinstance(status.get("cfg_version"), int) else None
    db.execute(
        """UPDATE devices SET online = :online, last_status = :status,
             ip = COALESCE(:ip, ip), fw = COALESCE(:fw, fw), cfg_version = COALESCE(:cv, cfg_version),
             last_seen = CASE WHEN :online = 1 THEN :now ELSE last_seen END
           WHERE id = :id""",
        {"online": online, "status": json.dumps(status)[:2000], "ip": ip and ip[:45], "fw": fw and fw[:20],
         "cv": cv, "now": utcnow_iso(), "id": device_id},
    )
    add_log(
        "device",
        "status",
        f"Device {'ONLINE' if online else 'OFFLINE'}{f' (IP {ip}, FW {fw})' if ip or fw else ''}",
        level="INFO" if online else "WARN",
        device_id=device_id,
    )
    hub.publish({"type": "status", "device": public_device(get_device(device_id))})


def mark_stale_offline(offline_after_sec: int) -> None:
    cutoff = iso_ago(offline_after_sec)
    for row in db.query("SELECT id FROM devices WHERE online = 1 AND (last_seen IS NULL OR last_seen < ?)", (cutoff,)):
        db.execute("UPDATE devices SET online = 0 WHERE id = ?", (row["id"],))
        hub.publish({"type": "status", "device": public_device(get_device(row["id"]))})


# ---------------- telemetry ----------------

def store_telemetry(records: list[dict]) -> int:
    inserted = 0
    now = utcnow_iso()
    with db.transaction() as conn:
        for r in records:
            inserted += conn.execute(_INSERT_TELEMETRY, r).rowcount
            conn.execute(
                """UPDATE devices SET last_seen = ?, online = 1, fw = COALESCE(?, fw),
                     cfg_version = COALESCE(?, cfg_version) WHERE id = ?""",
                (now, r["fw"], r["cfg_version"], r["device_id"]),
            )
    for r in records:
        hub.publish({"type": "telemetry", "data": r})
    if records:
        hub.publish({"type": "status", "device": public_device(get_device(records[-1]["device_id"]))})
    return inserted


# ---------------- rides ----------------

_RIDE_STAT_FIELDS = ("temp_c", "humidity", "pressure_hpa", "aqi", "pm2_5", "pm10")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _ride_metrics(records: list[dict]) -> dict:
    """Compute duration, haversine distance and per-metric min/max/avg for a ride."""
    recs = sorted(records, key=lambda r: r["ts"])
    fixes = [r for r in recs if r.get("gps_fix") and r.get("lat") is not None and r.get("lon") is not None]
    distance = 0.0
    for prev, cur in zip(fixes, fixes[1:]):
        distance += _haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])

    def agg(field: str) -> dict:
        vals = [float(r[field]) for r in recs if isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)]
        if not vals:
            return {"min": None, "max": None, "avg": None}
        return {
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "avg": round(sum(vals) / len(vals), 2),
        }

    stats = {field: agg(field) for field in _RIDE_STAT_FIELDS}
    stats["fixes"] = len(fixes)
    stats["samples"] = len(recs)

    started = recs[0]["ts"] if recs else None
    ended = recs[-1]["ts"] if recs else None
    duration = 0.0
    if started and ended:
        try:
            duration = (datetime.fromisoformat(ended.replace("Z", "+00:00"))
                        - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            duration = 0.0
    return {
        "started_at": started,
        "ended_at": ended,
        "duration_s": round(max(0.0, duration), 3),
        "distance_m": round(distance, 1),
        "stats": stats,
    }


def _hydrate_ride(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    try:
        out["stats"] = json.loads(out.pop("stats_json") or "{}")
    except (TypeError, ValueError):
        out["stats"] = {}
        out.pop("stats_json", None)
    return out


def get_ride(ride_id: str) -> dict | None:
    return _hydrate_ride(db.one("SELECT * FROM rides WHERE id = ?", (ride_id,)))


def find_ride_by_hash(device_id: str, sha256: str) -> dict | None:
    return _hydrate_ride(db.one("SELECT * FROM rides WHERE device_id = ? AND sha256 = ?",
                                (device_id, sha256)))


def list_rides(device_id: str, limit: int = 50) -> list[dict]:
    rows = db.query("SELECT * FROM rides WHERE device_id = ? ORDER BY started_at DESC, uploaded_at DESC LIMIT ?",
                    (device_id, limit))
    return [_hydrate_ride(r) for r in rows]


def ride_track(ride_id: str, limit: int = 50000) -> list[dict]:
    return db.query(
        """SELECT ts, lat, lon, alt, speed_kmh FROM telemetry
           WHERE ride_id = ? AND gps_fix = 1 AND lat IS NOT NULL AND lon IS NOT NULL
           ORDER BY ts LIMIT ?""",
        (ride_id, limit),
    )


def save_ride(device_id: str, filename: str, sha256: str, records: list[dict]) -> tuple[bool, dict]:
    """Persist an uploaded ride idempotently. Returns (duplicate, ride)."""
    existing = find_ride_by_hash(device_id, sha256)
    if existing:
        return True, existing

    ride_id = secrets.token_hex(16)
    metrics = _ride_metrics(records)
    uploaded_at = utcnow_iso()
    try:
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO rides (id, device_id, filename, sha256, started_at, ended_at, duration_s,
                                      distance_m, sample_count, stats_json, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ride_id, device_id, filename[:200], sha256, metrics["started_at"], metrics["ended_at"],
                 metrics["duration_s"], metrics["distance_m"], len(records),
                 json.dumps(metrics["stats"]), uploaded_at),
            )
            for rec in records:
                row = dict(rec)
                row["ride_id"] = ride_id
                row["transport"] = "upload"
                row["replay"] = 1
                conn.execute(_INSERT_TELEMETRY, row)
    except sqlite3.IntegrityError:
        # A concurrent upload with the same content won the unique(device_id, sha256) race.
        existing = find_ride_by_hash(device_id, sha256)
        if existing:
            return True, existing
        raise

    ride = get_ride(ride_id)
    assert ride is not None
    add_log("device", "ride_upload",
            f"Ride {filename} stored: {len(records)} samples, {metrics['distance_m']:.0f} m", device_id=device_id)
    return False, ride


def delete_ride(ride_id: str) -> int:
    ride = get_ride(ride_id)
    if not ride:
        return 0
    with db.transaction() as conn:
        removed = conn.execute("DELETE FROM telemetry WHERE ride_id = ?", (ride_id,)).rowcount
        conn.execute("DELETE FROM rides WHERE id = ?", (ride_id,))
    add_log("server", "ride_delete", f"Deleted ride {ride_id} ({removed} samples)", device_id=ride["device_id"])
    return removed


def log_rejected(device_id: str | None, reason: str, payload) -> None:
    p = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    db.execute("INSERT INTO rejected_messages (device_id, received_at, reason, payload) VALUES (?, ?, ?, ?)",
               (device_id, utcnow_iso(), reason[:500], (p or "")[:4000]))
    add_log("server", "telemetry_warn", reason[:300], level="WARN", device_id=device_id)


# ---------------- system & device logs ----------------

def add_log(source: str, event: str, detail: str = "", level: str = "INFO", device_id: str | None = None) -> dict:
    ts = utcnow_iso()
    lvl = level.upper() if level.upper() in ("INFO", "WARN", "ERROR") else "INFO"
    src = "device" if source == "device" else "server"
    cur = db.execute(
        "INSERT INTO system_logs (ts, source, device_id, level, event, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, src, device_id, lvl, event[:48], detail[:600]),
    )
    row = {
        "id": cur.lastrowid,
        "ts": ts,
        "source": src,
        "device_id": device_id,
        "level": lvl,
        "event": event[:48],
        "detail": detail[:600],
    }
    hub.publish({"type": "log", "data": row})
    return row


def query_logs(
    source: str | None = None,
    device_id: str | None = None,
    level: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = []
    params: list = []
    if source in ("server", "device"):
        clauses.append("source = ?")
        params.append(source)
    if device_id:
        clauses.append("(device_id = ? OR device_id IS NULL)")
        params.append(device_id)
    if level and level.upper() in ("INFO", "WARN", "ERROR"):
        clauses.append("level = ?")
        params.append(level.upper())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.query(f"SELECT * FROM system_logs {where} ORDER BY id DESC LIMIT ?", tuple(params))


def clear_logs(device_id: str | None = None) -> int:
    if device_id:
        return db.execute("DELETE FROM system_logs WHERE device_id = ?", (device_id,)).rowcount
    return db.execute("DELETE FROM system_logs").rowcount


def latest_telemetry(device_id: str) -> dict | None:
    return db.one("SELECT * FROM telemetry WHERE device_id = ? ORDER BY ts DESC, id DESC LIMIT 1", (device_id,))


def last_fix(device_id: str) -> dict | None:
    return db.one("SELECT ts, lat, lon, alt, sats, hdop FROM telemetry "
                  "WHERE device_id = ? AND gps_fix = 1 ORDER BY ts DESC LIMIT 1", (device_id,))


def query_telemetry(device_id: str, frm: str, to: str, limit: int, newest_first: bool = False) -> list[dict]:
    order = "DESC" if newest_first else "ASC"
    return db.query(f"SELECT * FROM telemetry WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts {order} LIMIT ?",
                    (device_id, frm, to, limit))


def series(device_id: str, frm: str, to: str, bucket_sec: int) -> list[dict]:
    return db.query(
        """SELECT CAST(CAST(strftime('%s', ts) AS INTEGER) / :b AS INTEGER) * :b AS t,
                  AVG(temp_c) AS temp_c, AVG(humidity) AS humidity, AVG(pressure_hpa) AS pressure_hpa,
                  AVG(aqi) AS aqi, AVG(gas_kohm) AS gas_kohm, AVG(rssi) AS rssi, AVG(alt) AS alt,
                  AVG(eco2_ppm) AS eco2_ppm, AVG(pm2_5) AS pm2_5, AVG(pm10) AS pm10, COUNT(*) AS n
             FROM telemetry WHERE device_id = :id AND ts >= :frm AND ts <= :to
            GROUP BY t ORDER BY t""",
        {"b": bucket_sec, "id": device_id, "frm": frm, "to": to},
    )


def track(device_id: str, frm: str, limit: int) -> list[dict]:
    return db.query("SELECT ts, lat, lon, alt, speed_kmh FROM telemetry "
                    "WHERE device_id = ? AND gps_fix = 1 AND ts >= ? ORDER BY ts LIMIT ?", (device_id, frm, limit))


def stats(device_id: str, frm: str) -> dict:
    return db.one(
        """SELECT COUNT(*) AS n, MIN(temp_c) AS temp_min, MAX(temp_c) AS temp_max, AVG(temp_c) AS temp_avg,
                  MIN(humidity) AS hum_min, MAX(humidity) AS hum_max, AVG(humidity) AS hum_avg,
                  MIN(pressure_hpa) AS pres_min, MAX(pressure_hpa) AS pres_max,
                  AVG(aqi) AS aqi_avg, MAX(aqi) AS aqi_max, SUM(replay) AS replayed, SUM(gps_fix) AS fixes,
                  MIN(pm2_5) AS pm2_5_min, MAX(pm2_5) AS pm2_5_max, AVG(pm2_5) AS pm2_5_avg,
                  MIN(pm10) AS pm10_min, MAX(pm10) AS pm10_max, AVG(pm10) AS pm10_avg
             FROM telemetry WHERE device_id = ? AND ts >= ?""",
        (device_id, frm),
    ) or {}


def rejected(device_id: str, limit: int) -> list[dict]:
    return db.query("SELECT * FROM rejected_messages WHERE device_id = ? ORDER BY id DESC LIMIT ?", (device_id, limit))


def purge_old(days: int) -> int:
    n = db.execute("DELETE FROM telemetry WHERE ts < ?", (iso_ago(days * 86400),)).rowcount
    db.execute("DELETE FROM rejected_messages WHERE received_at < ?", (iso_ago(days * 86400),))
    db.execute("DELETE FROM pairing_tokens WHERE expires_at < ?", (utcnow_iso(),))
    return n


# ---------------- calibration config ----------------

def get_config(device_id: str) -> dict | None:
    row = db.one("SELECT * FROM device_config WHERE device_id = ?", (device_id,))
    if not row:
        return None
    return {"version": row["version"], **json.loads(row["config_json"]),
            "_updated_at": row["updated_at"], "_updated_by": row["updated_by"]}


def save_config(device_id: str, cfg: dict, version: int, user: str | None) -> dict:
    clean = {k: v for k, v in cfg.items() if k != "version" and not k.startswith("_")}
    db.execute(
        """INSERT INTO device_config (device_id, version, config_json, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(device_id) DO UPDATE SET version = excluded.version, config_json = excluded.config_json,
             updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
        (device_id, version, json.dumps(clean), utcnow_iso(), user),
    )
    return {"version": version, **clean}


def device_config_payload(device_id: str) -> dict | None:
    c = get_config(device_id)
    return {k: v for k, v in c.items() if not k.startswith("_")} if c else None


# ---------------- users & profiles ----------------

def get_user(username: str) -> dict | None:
    return db.one("SELECT * FROM users WHERE username = ?", (username,))


def public_profile(u: dict | None) -> dict | None:
    if not u:
        return None
    devices = [
        public_device(d)
        for d in db.query("SELECT * FROM devices WHERE owner_username = ? ORDER BY id", (u["username"],))
    ]
    return {
        "username": u["username"],
        "full_name": u.get("full_name") or u["username"],
        "email": u.get("email") or "",
        "role": u.get("role") or "Researcher",
        "created_at": u.get("created_at"),
        "last_login": u.get("last_login"),
        "devices": devices,
    }


def upsert_user(username: str, password: str, full_name: str | None = None, email: str = "", role: str = "Researcher") -> None:
    db.execute(
        """INSERT INTO users (username, password_hash, full_name, email, role)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(username) DO UPDATE SET
             password_hash = excluded.password_hash,
             full_name = COALESCE(excluded.full_name, users.full_name),
             email = CASE WHEN excluded.email != '' THEN excluded.email ELSE users.email END,
             role = COALESCE(excluded.role, users.role)""",
        (username, hash_secret(password), full_name or username, email, role),
    )


def update_profile(username: str, full_name: str, email: str, role: str) -> dict | None:
    db.execute(
        "UPDATE users SET full_name = ?, email = ?, role = ? WHERE username = ?",
        (full_name.strip(), email.strip(), role.strip(), username),
    )
    return public_profile(get_user(username))


def touch_login(username: str) -> None:
    db.execute("UPDATE users SET last_login = ? WHERE username = ?", (utcnow_iso(), username))
