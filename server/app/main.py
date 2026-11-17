"""FastAPI application: REST API, WebSocket live stream, device ingestion, profiles & QR pairing."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from . import auth, config, db, mqtt_bridge, store
from .live import hub
from .schemas import (
    DeviceBootstrapRequest,
    DeviceClaimRequest,
    DeviceCommandRequest,
    DeviceConfig,
    DeviceConfigUpdate,
    DeviceCreateRequest,
    DeviceSimulateRequest,
    LoginRequest,
    PasswordChange,
    ProfileUpdate,
    QrRedeemRequest,
    RegisterRequest,
    ValidationError,
    normalize_telemetry,
    utcnow_iso,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wsb.api")

MAX_INGEST_BYTES = 256 * 1024
MAX_BATCH = 200

MAX_RIDE_BYTES = 5 * 1024 * 1024
MAX_RIDE_ROWS = 50_000
RIDE_CSV_COLUMNS = [
    "ts", "lat", "lon", "alt", "speed_kmh", "sats", "hdop", "temp_c", "humidity", "pressure_hpa",
    "pm1", "pm2_5", "pm4", "pm10", "nc0_5", "nc1", "nc2_5", "nc4", "nc10", "aqi",
]


class _RideRowError(Exception):
    pass


def _ride_ts(raw: object) -> str:
    """Parse a CSV timestamp and return canonical UTC ISO-8601, or raise."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise _RideRowError("ts: missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _RideRowError(f"ts: invalid ISO-8601 {text[:32]!r}")
    if parsed.tzinfo is None:
        raise _RideRowError("ts: timezone required (use UTC ...Z)")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ride_num(row: dict, field: str) -> float | None:
    raw = row.get(field)
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise _RideRowError(f"{field}: not a number")


def _ride_row_message(row: dict, device_id: str) -> dict:
    ts = _ride_ts(row.get("ts"))
    lat = _ride_num(row, "lat")
    lon = _ride_num(row, "lon")
    fix = lat is not None and lon is not None and not (lat == 0 and lon == 0)

    env = {}
    for key in ("temp_c", "humidity", "pressure_hpa", "aqi",
                "pm1", "pm2_5", "pm4", "pm10",
                "nc0_5", "nc1", "nc2_5", "nc4", "nc10"):
        env[key] = _ride_num(row, key)
    sats = _ride_num(row, "sats")

    msg = {
        "device_id": device_id,
        "ts": ts,
        "ts_source": "sdcard",
        "replay": True,
        "gps": {
            "fix": fix,
            "lat": lat,
            "lon": lon,
            "alt": _ride_num(row, "alt"),
            "speed_kmh": _ride_num(row, "speed_kmh"),
            "sats": None if sats is None else int(round(sats)),
            "hdop": _ride_num(row, "hdop"),
        },
        "env": env,
        "sys": {},
    }
    rec, warnings = normalize_telemetry(
        msg, device_id, "upload", allow_agps_fallback=False, derive_aqi=False
    )
    ts_warnings = [w for w in warnings if w.startswith("ts:")]
    if ts_warnings:
        raise _RideRowError("; ".join(ts_warnings))
    return rec


def _parse_ride_csv(raw: bytes, device_id: str) -> tuple[list[dict], list[dict], int]:
    """Return (accepted records, first row errors, total rejected). Raises for bad header/size."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must be UTF-8")
    if not text.strip():
        raise HTTPException(400, "empty CSV body")

    reader = csv.DictReader(io.StringIO(text))
    fields = [(f or "").strip() for f in (reader.fieldnames or [])]
    missing = [c for c in RIDE_CSV_COLUMNS if c not in fields]
    if missing:
        raise HTTPException(400, f"CSV header missing columns: {', '.join(missing)}")
    reader.fieldnames = fields

    accepted: list[dict] = []
    errors: list[dict] = []
    rejected = 0
    for index, row in enumerate(reader, start=1):
        if index > MAX_RIDE_ROWS:
            raise HTTPException(413, f"ride larger than {MAX_RIDE_ROWS} rows")
        try:
            accepted.append(_ride_row_message(row, device_id))
        except _RideRowError as e:
            rejected += 1
            if len(errors) < 10:
                errors.append({"row": index, "error": str(e)})
    return accepted, errors, rejected


def _process_ride_upload(device_id: str, filename: str, raw: bytes) -> dict:
    sha256 = hashlib.sha256(raw).hexdigest()
    existing = store.find_ride_by_hash(device_id, sha256)
    if existing:
        return {
            "ok": True,
            "ride_id": existing["id"],
            "inserted": 0,
            "rejected": 0,
            "samples": existing["sample_count"],
            "duration_s": existing["duration_s"],
            "distance_m": existing["distance_m"],
            "duplicate": True,
        }

    accepted, errors, rejected = _parse_ride_csv(raw, device_id)
    if not accepted:
        store.add_log("device", "ride_rejected",
                      f"Ride upload {filename} had no valid rows ({rejected} errors)", level="WARN",
                      device_id=device_id)
        raise HTTPException(400, {"message": "no valid rows in CSV", "rejected": rejected, "errors": errors})

    duplicate, ride = store.save_ride(device_id, filename, sha256, accepted)
    if duplicate:
        return {
            "ok": True,
            "ride_id": ride["id"],
            "inserted": 0,
            "rejected": rejected,
            "samples": ride["sample_count"],
            "duration_s": ride["duration_s"],
            "distance_m": ride["distance_m"],
            "duplicate": True,
        }
    return {
        "ok": True,
        "ride_id": ride["id"],
        "inserted": len(accepted),
        "rejected": rejected,
        "samples": ride["sample_count"],
        "duration_s": ride["duration_s"],
        "distance_m": ride["distance_m"],
        "duplicate": False,
    }


async def _housekeeping() -> None:
    tick = 0
    while True:
        await asyncio.sleep(15)
        tick += 1
        try:
            await asyncio.to_thread(store.mark_stale_offline, config.OFFLINE_AFTER_SEC)
            if tick % 240 == 0:
                n = await asyncio.to_thread(store.purge_old, config.RETENTION_DAYS)
                if n:
                    log.info("retention: purged %d rows", n)
        except Exception:
            log.exception("housekeeping failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    # Ensure existing devices have claim codes & default admin has profile metadata
    for d in store.list_devices():
        if not d.get("claim_code"):
            store.issue_pairing_token(d["id"])
    hub.bind(asyncio.get_running_loop())
    mqtt_bridge.start()
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    mqtt_bridge.stop()


app = FastAPI(title="WSB Telemetry API", version="1.1.0", lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

CSP = ("default-src 'self'; script-src 'self'; worker-src 'self' blob:; child-src 'self' blob:; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "img-src 'self' data: blob: https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org https://server.arcgisonline.com; "
       "connect-src 'self' ws: wss: https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org https://server.arcgisonline.com; "
       "font-src 'self' data: https://fonts.gstatic.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/api/docs"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _device_or_404(device_id: str) -> dict:
    d = store.get_device(device_id)
    if not d:
        raise HTTPException(404, "device not found")
    return d


def _bounds(hours: float | None, frm: str | None, to: str | None, default_hours: float) -> tuple[str, str]:
    if frm or to:
        try:
            f = datetime.fromisoformat((frm or "2000-01-01T00:00:00Z").replace("Z", "+00:00"))
            t = datetime.fromisoformat(to.replace("Z", "+00:00")) if to else datetime.now(timezone.utc)
        except ValueError:
            raise HTTPException(400, "from/to must be ISO-8601")
        iso = lambda d: d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return iso(f), iso(t)
    return store.iso_ago((hours or default_hours) * 3600), utcnow_iso()


# =============================== health ===============================

@app.get("/api/health")
def health():
    return {"ok": True, "time": utcnow_iso(), "mqtt": mqtt_bridge.state, "ws_clients": hub.client_count}


# ============================ auth & profiles =========================

@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    ip = auth.client_ip(request)
    auth.throttle_check(ip)
    if not auth.check_credentials(body.username, body.password):
        auth.throttle_fail(ip)
        log.warning("failed login for %r from %s", body.username, ip)
        raise HTTPException(401, "Invalid username or password")
    auth.throttle_reset(ip)
    store.touch_login(body.username)

    claimed_device_id = None
    if body.pair_token:
        dev = store.resolve_pairing_code(body.pair_token)
        if dev:
            store.claim_device(dev["id"], body.username)
            claimed_device_id = dev["id"]

    auth.set_session_cookie(request, response, auth.issue_token(body.username))
    store.add_log("server", "auth_login", f"User '{body.username}' signed in from {ip}", device_id=claimed_device_id)
    return {
        "ok": True,
        "user": body.username,
        "profile": store.public_profile(store.get_user(body.username)),
        "claimed_device_id": claimed_device_id,
    }


@app.post("/api/auth/register")
def register(body: RegisterRequest, request: Request, response: Response):
    ip = auth.client_ip(request)
    auth.throttle_check(ip)
    if store.get_user(body.username):
        raise HTTPException(409, "Username is already taken")
    store.upsert_user(body.username, body.password, body.full_name, body.email, body.role)
    store.touch_login(body.username)

    claimed_device_id = None
    if body.pair_token:
        dev = store.resolve_pairing_code(body.pair_token)
        if dev:
            store.claim_device(dev["id"], body.username)
            claimed_device_id = dev["id"]

    auth.set_session_cookie(request, response, auth.issue_token(body.username))
    log.info("registered new user profile %r (claimed=%s)", body.username, claimed_device_id)
    store.add_log("server", "auth_register", f"Created profile '{body.username}' ({body.full_name}, {body.role})", device_id=claimed_device_id)
    return {
        "ok": True,
        "user": body.username,
        "profile": store.public_profile(store.get_user(body.username)),
        "claimed_device_id": claimed_device_id,
    }


@app.post("/api/auth/logout")
def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: str = Depends(auth.require_user)):
    u = store.get_user(user)
    return {
        "user": user,
        "last_login": u.get("last_login") if u else None,
        "profile": store.public_profile(u),
    }


@app.get("/api/auth/profile")
def get_profile(user: str = Depends(auth.require_user)):
    return store.public_profile(store.get_user(user))


@app.put("/api/auth/profile")
def put_profile(body: ProfileUpdate, user: str = Depends(auth.require_user)):
    return store.update_profile(user, body.full_name, body.email, body.role)


@app.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request, user: str = Depends(auth.require_user)):
    if not auth.check_credentials(user, body.current_password):
        auth.throttle_fail(auth.client_ip(request))
        raise HTTPException(400, "Current password is incorrect")
    store.upsert_user(user, body.new_password)
    return {"ok": True}


# ============================ QR pairing / phone sign-in ==============

@app.get("/api/auth/qr-info")
def qr_info(p: str = Query(..., min_length=3, max_length=64)):
    """Public metadata for a scanned ESP32 QR code so the phone can display the unit details."""
    dev = store.resolve_pairing_code(p)
    if not dev:
        raise HTTPException(404, "Invalid or expired QR pairing code")
    return {
        "device_id": dev["id"],
        "name": dev["name"],
        "online": bool(dev["online"]),
        "owner_username": dev.get("owner_username"),
        "claim_code": dev.get("claim_code"),
        "can_quick_login": True,
    }


@app.post("/api/auth/qr-redeem")
def qr_redeem(body: QrRedeemRequest, request: Request, response: Response):
    """Allows a user scanning the physical ESP32 QR code on their phone to sign in directly."""
    dev = store.resolve_pairing_code(body.pair_token)
    if not dev:
        raise HTTPException(404, "Invalid or expired QR pairing code")
    target_user = dev.get("owner_username") or "admin"
    if not store.get_user(target_user):
        target_user = "admin"
    if not dev.get("owner_username"):
        store.claim_device(dev["id"], target_user)
    store.touch_login(target_user)
    auth.set_session_cookie(request, response, auth.issue_token(target_user))
    store.add_log("server", "qr_login", f"Mobile QR sign-in for user '{target_user}' on device '{dev['id']}'", device_id=dev["id"])
    return {
        "ok": True,
        "user": target_user,
        "profile": store.public_profile(store.get_user(target_user)),
        "device_id": dev["id"],
    }


# =========================== device endpoints ==========================

@app.post("/api/device/bootstrap")
def device_bootstrap(body: DeviceBootstrapRequest):
    """Called by the ESP32 on Wi-Fi connect to obtain its pairing QR URL and claim code."""
    return store.bootstrap_device(body.mac, body.device_id, body.device_key, body.fw)


@app.post("/api/ingest")
async def ingest(request: Request, device_id: str = Depends(auth.require_device)):
    raw = await request.body()
    if len(raw) > MAX_INGEST_BYTES:
        raise HTTPException(413, "payload too large")
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        await asyncio.to_thread(store.log_rejected, device_id, "invalid JSON (http)", raw.decode(errors="replace"))
        raise HTTPException(400, "invalid JSON")
    items = payload if isinstance(payload, list) else [payload]
    if len(items) > MAX_BATCH:
        raise HTTPException(413, f"batch larger than {MAX_BATCH}")

    def work():
        recs, rejected, warnings_total = [], [], 0
        for i, item in enumerate(items):
            try:
                rec, warnings = normalize_telemetry(item, device_id, "http")
                if warnings:
                    warnings_total += 1
                    store.log_rejected(device_id, "partial: " + "; ".join(warnings), item)
                recs.append(rec)
            except ValidationError as e:
                rejected.append({"index": i, "error": str(e)})
                store.log_rejected(device_id, str(e), item)
        inserted = store.store_telemetry(recs) if recs else 0
        return {"accepted": len(recs), "inserted": inserted, "duplicates": len(recs) - inserted,
                "rejected": len(rejected), "errors": rejected[:10], "with_warnings": warnings_total}

    return await asyncio.to_thread(work)


@app.get("/api/device/config")
def device_config(device_id: str = Depends(auth.require_device)):
    return store.device_config_payload(device_id) or {"version": 0, **DeviceConfig().model_dump()}


@app.post("/api/devices/{device_id}/rides")
async def upload_ride(device_id: str, request: Request, auth_device: str = Depends(auth.require_device)):
    """Upload a CSV ride recorded on the device SD card (raw text/csv or multipart `file`)."""
    if device_id != auth_device:
        raise HTTPException(403, "device_id does not match authenticated device")
    _device_or_404(device_id)

    content_type = (request.headers.get("content-type") or "").lower()
    filename = request.headers.get("x-filename") or ""
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "multipart body must include a 'file' field")
        raw = await upload.read()
        filename = filename or getattr(upload, "filename", "") or ""
    elif "text/csv" in content_type or "application/csv" in content_type:
        raw = await request.body()
    else:
        raise HTTPException(415, "expected Content-Type text/csv or multipart/form-data")

    if len(raw) > MAX_RIDE_BYTES:
        raise HTTPException(413, f"ride larger than {MAX_RIDE_BYTES} bytes")
    safe_name = os.path.basename((filename or "ride.csv").replace("\\", "/"))[:200] or "ride.csv"
    return await asyncio.to_thread(_process_ride_upload, device_id, safe_name, raw)


# ============================ user endpoints ===========================

@app.get("/api/devices")
def devices(user: str = Depends(auth.require_user)):
    out = []
    for d in store.list_devices():
        out.append({**d, "latest": store.latest_telemetry(d["id"])})
    return out


@app.post("/api/devices")
def register_device(body: DeviceCreateRequest, user: str = Depends(auth.require_user)):
    dev_id = (body.device_id or "").strip().lower()
    if not dev_id:
        dev_id = f"cyd-{secrets.token_hex(2)}"
    if store.get_device(dev_id):
        raise HTTPException(409, f"Device ID '{dev_id}' already exists")
    key = store.create_device(
        dev_id,
        name=body.name.strip(),
        owner_username=user,
        location_label=body.location_label.strip(),
    )
    mqtt_bridge.publish_config(dev_id)
    pairing = store.issue_pairing_token(dev_id)
    dev = store.public_device(store.get_device(dev_id))
    return {
        "device": dev,
        "device_key": key,
        **pairing,
    }


@app.post("/api/devices/claim")
def claim_device_endpoint(body: DeviceClaimRequest, user: str = Depends(auth.require_user)):
    dev = store.resolve_pairing_code(body.code)
    if not dev:
        raise HTTPException(404, "No ESP32 found matching that claim code or QR token")
    updated = store.claim_device(dev["id"], user, body.name, body.location_label)
    return {"ok": True, "device": updated}


@app.delete("/api/devices/{device_id}")
def remove_device(device_id: str, user: str = Depends(auth.require_user)):
    _device_or_404(device_id)
    store.delete_device(device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}")
def device_detail(device_id: str, user: str = Depends(auth.require_user)):
    d = _device_or_404(device_id)
    status = json.loads(d["last_status"]) if d.get("last_status") else None
    pairing = store.issue_pairing_token(device_id)
    return {
        "device": store.public_device(d),
        "pairing": pairing,
        "status": status,
        "latest": store.latest_telemetry(device_id),
        "last_fix": store.last_fix(device_id),
        "config": store.get_config(device_id),
        "stats_24h": store.stats(device_id, store.iso_ago(86400)),
        "mqtt": mqtt_bridge.state,
    }


@app.get("/api/devices/{device_id}/telemetry")
def telemetry(device_id: str, user: str = Depends(auth.require_user),
              hours: float | None = Query(None, gt=0, le=24 * 365),
              frm: str | None = Query(None, alias="from"), to: str | None = None,
              limit: int = Query(200, ge=1, le=10000), order: str = Query("desc", pattern="^(asc|desc)$")):
    _device_or_404(device_id)
    f, t = _bounds(hours, frm, to, 24)
    return store.query_telemetry(device_id, f, t, limit, newest_first=(order == "desc"))


@app.get("/api/devices/{device_id}/series")
def series(device_id: str, user: str = Depends(auth.require_user),
           hours: float = Query(1, gt=0, le=24 * 90), points: int = Query(240, ge=10, le=2000)):
    _device_or_404(device_id)
    bucket = max(1, int(hours * 3600 / points))
    return {"bucket_sec": bucket,
            "points": store.series(device_id, store.iso_ago(hours * 3600), utcnow_iso(), bucket)}


@app.get("/api/devices/{device_id}/track")
def track(device_id: str, user: str = Depends(auth.require_user),
          hours: float = Query(24, gt=0, le=24 * 90), limit: int = Query(5000, ge=1, le=50000)):
    _device_or_404(device_id)
    return store.track(device_id, store.iso_ago(hours * 3600), limit)


@app.get("/api/devices/{device_id}/rejected")
def rejected(device_id: str, user: str = Depends(auth.require_user), limit: int = Query(50, ge=1, le=500)):
    _device_or_404(device_id)
    return store.rejected(device_id, limit)


@app.get("/api/devices/{device_id}/rides")
def device_rides(device_id: str, user: str = Depends(auth.require_user),
                 limit: int = Query(50, ge=1, le=200)):
    _device_or_404(device_id)
    return store.list_rides(device_id, limit)


@app.get("/api/rides/{ride_id}")
def ride_detail(ride_id: str, user: str = Depends(auth.require_user)):
    ride = store.get_ride(ride_id)
    if not ride:
        raise HTTPException(404, "ride not found")
    device = store.public_device(store.get_device(ride["device_id"]))
    return {
        **ride,
        "track": store.ride_track(ride_id),
        "device": device,
        "config": store.device_config_payload(ride["device_id"]),
    }


@app.get("/api/rides/{ride_id}/export.csv")
def ride_export_csv(ride_id: str, user: str = Depends(auth.require_user)):
    ride = store.get_ride(ride_id)
    if not ride:
        raise HTTPException(404, "ride not found")
    rows = db.query("SELECT * FROM telemetry WHERE ride_id = ? ORDER BY ts, id", (ride_id,))
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    name = f"ride_{ride_id}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/rides/{ride_id}")
def remove_ride(ride_id: str, user: str = Depends(auth.require_user)):
    if not store.get_ride(ride_id):
        raise HTTPException(404, "ride not found")
    removed = store.delete_ride(ride_id)
    return {"ok": True, "deleted_samples": removed}


@app.get("/api/devices/{device_id}/config")
def get_config(device_id: str, user: str = Depends(auth.require_user)):
    _device_or_404(device_id)
    return store.get_config(device_id) or {"version": 0, **DeviceConfig().model_dump()}


@app.put("/api/devices/{device_id}/config")
def put_config(device_id: str, update: DeviceConfigUpdate, user: str = Depends(auth.require_user)):
    _device_or_404(device_id)
    current = store.get_config(device_id) or {"version": 0, **DeviceConfig().model_dump()}
    merged = {k: v for k, v in current.items() if k != "version" and not k.startswith("_")}
    merged.update(update.model_dump(exclude_unset=True, exclude_none=True))
    try:
        cfg = DeviceConfig(**merged)
    except PydanticValidationError as e:
        raise HTTPException(422, "; ".join(err["msg"] for err in e.errors()))
    saved = store.save_config(device_id, cfg.model_dump(), current["version"] + 1, user)
    pushed = mqtt_bridge.publish_config(device_id)
    store.add_log("server", "config_update", f"Config v{saved['version']} saved by '{user}' (MQTT push={pushed})", device_id=device_id)
    hub.publish({"type": "config", "device_id": device_id, "config": saved})
    log.info("config v%d for %s by %s (mqtt push=%s)", saved["version"], device_id, user, pushed)
    return {**saved, "pushed": pushed}


@app.post("/api/devices/{device_id}/command")
def send_device_command(device_id: str, body: DeviceCommandRequest, user: str = Depends(auth.require_user)):
    d = _device_or_404(device_id)
    payload = body.model_dump(exclude_none=True)
    sent = mqtt_bridge.publish_command(device_id, payload)
    # Also update cached live_state immediately so all connected browsers reflect page/LED/screen changes
    st = {}
    if d.get("last_status"):
        try:
            st = json.loads(d["last_status"])
        except Exception:
            st = {}
    st["online"] = bool(d.get("online", 1))
    if body.cmd == "set_page" and body.page is not None:
        st["page"] = body.page
    elif body.cmd == "led" and body.mode is not None:
        st["led_mode"] = body.mode
    elif body.cmd == "screen" and body.state is not None:
        st["screen_state"] = "active" if body.state == "wake" else body.state
    store.update_device_status(device_id, st)
    store.add_log("server", f"cmd_{body.cmd}", f"Remote command '{body.cmd}' sent by '{user}': {json.dumps(payload)}", device_id=device_id)
    return {"ok": True, "sent": sent, "command": payload}


@app.post("/api/devices/{device_id}/simulate")
def simulate_device_telemetry(device_id: str, body: DeviceSimulateRequest, user: str = Depends(auth.require_user)):
    d = _device_or_404(device_id)
    latest = store.latest_telemetry(device_id)
    next_seq = ((latest.get("seq") or 0) + 1) if latest else 1
    alt = body.alt if body.alt is not None else round(44330.0 * (1.0 - pow(body.pressure_hpa / 1013.25, 0.190295)), 1)
    msg = {
        "device_id": device_id,
        "seq": next_seq,
        "ts": utcnow_iso(),
        "ts_source": "sim",
        "uptime_ms": ((latest.get("uptime_ms") or 60000) + 10000) if latest else 60000,
        "replay": False,
        "gps": {
            "fix": True,
            "lat": body.lat,
            "lon": body.lon,
            "alt": alt,
            "speed_kmh": 0.0,
            "course": 0.0,
            "sats": body.sats,
            "hdop": body.hdop,
        },
        "env": {
            "temp_c": body.temp_c,
            "humidity": body.humidity,
            "pressure_hpa": body.pressure_hpa,
            "aqi": body.aqi,
            "aqi_source": "simulated",
        },
        "sys": {
            "rssi": body.rssi,
            "heap": 198400,
            "buffered": 0,
            "fw": d.get("fw") or "1.0.0",
            "cfg_version": d.get("cfg_version") or 1,
        },
    }
    rec, _ = normalize_telemetry(msg, device_id, "sim")
    store.store_telemetry([rec])
    st = {}
    if d.get("last_status"):
        try:
            st = json.loads(d["last_status"])
        except Exception:
            st = {}
    st.update({"online": True, "page": body.page, "ldr": body.ldr, "fw": d.get("fw") or "1.0.0"})
    store.update_device_status(device_id, st)
    store.add_log("device", "sim_packet", f"Impersonated packet seq={next_seq} (T={body.temp_c}°C, RH={body.humidity}%, P={body.pressure_hpa}hPa, AQI={body.aqi})", device_id=device_id)
    return {"ok": True, "telemetry": rec}


@app.get("/api/logs")
def get_logs(
    user: str = Depends(auth.require_user),
    source: str | None = Query(None, pattern="^(all|server|device)$"),
    device_id: str | None = None,
    level: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    src = None if source in (None, "all") else source
    return store.query_logs(source=src, device_id=device_id, level=level, limit=limit)


@app.delete("/api/logs")
def delete_logs(user: str = Depends(auth.require_user), device_id: str | None = None):
    cleared = store.clear_logs(device_id=device_id)
    return {"ok": True, "cleared": cleared}


CSV_COLS = ["ts", "received_at", "seq", "ts_source", "replay", "transport", "ride_id", "gps_fix",
            "lat", "lon", "alt", "speed_kmh", "course", "sats", "hdop", "temp_c", "humidity",
            "pressure_hpa", "gas_kohm", "aqi", "aqi_source", "eco2_ppm", "tvoc_ppb",
            "pm1", "pm2_5", "pm4", "pm10", "nc0_5", "nc1", "nc2_5", "nc4", "nc10", "ps",
            "rssi", "heap", "fw"]


@app.get("/api/devices/{device_id}/export.csv")
def export_csv(device_id: str, user: str = Depends(auth.require_user),
               hours: float = Query(24, gt=0, le=24 * 365)):
    _device_or_404(device_id)
    rows = store.query_telemetry(device_id, store.iso_ago(hours * 3600), utcnow_iso(), 500000)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    buf.seek(0)
    name = f"{device_id}_{datetime.now(timezone.utc):%Y%m%dT%H%M}Z.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ============================== WebSocket ==============================

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=4403)
        return
    user = auth.verify_token(websocket.cookies.get(auth.COOKIE))
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    q = hub.subscribe()
    await websocket.send_text(json.dumps({"type": "hello", "user": user, "time": utcnow_iso()}))

    async def sender():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                msg = json.dumps({"type": "ping", "time": utcnow_iso()})
            await websocket.send_text(msg)

    send_task = asyncio.create_task(sender())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        hub.unsubscribe(q)


# ============================== Web UI ================================

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
