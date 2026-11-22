# wsb-embedded-systems

ESP32 CYD firmware (`firmware/`), FastAPI + SQLite backend with an MQTT bridge
(`server/`), and a React dashboard (`web/`).

## Local development

Requires Python 3.12+ (3.14 works), Node 22+, Docker for the container stack,
and `pio` for firmware builds. `make test` rejects older Python versions.

```bash
cp .env.example .env      # set JWT_SECRET and MQTT_PASS
make venv                 # creates server/.venv and installs runtime + dev deps
```

Docker Compose reads `.env`. The other tools do not. For a manual API run,
export it from the repository root:

```bash
set -a; source .env; set +a
server/.venv/bin/uvicorn app.main:app --app-dir server --reload --port 8000
```

Build the dashboard into `server/static`; the API serves it at `/`:

```bash
make build-web            # cd web && npm ci && npm run build
cd web && npm run dev     # Vite dev server
```

Firmware builds need `include/secrets.h`, which git ignores. `make
build-firmware` copies `secrets.example.h` when `secrets.h` is missing. Edit the
copy before flashing:

```bash
make build-firmware       # cd firmware && pio run -e cyd
```

### Container stack

The image builds in two stages. A `node:22-slim` stage runs `npm ci` and `npm
run build`; the Python runtime copies the result to `/app/static`.

```bash
cp .env.example .env      # make dev-up fails with a message when this is missing
make dev-up               # docker compose up --build -d
curl localhost:8000/api/health
make dev-down
```

Host ports bind to `127.0.0.1` by default. The local broker
(`server/deploy/mosquitto-local.conf`) allows anonymous access; the loopback
binding keeps it off the network.

For firmware testing on the LAN, use the authenticated override:

```bash
# One-time password file outside the repo (same values as .env).
mkdir -p /tmp/wsb-mosquitto
mosquitto_passwd -c -b /tmp/wsb-mosquitto/passwd backend "$MQTT_PASS"
mosquitto_passwd -b /tmp/wsb-mosquitto/passwd cyd-01 "$DEVICE_KEY"
chmod 644 /tmp/wsb-mosquitto/passwd   # broker runs as uid 1883

API_BIND=0.0.0.0 MQTT_BIND=0.0.0.0 \
  docker compose -f docker-compose.yml -f docker-compose.lan.yml up --build
```

`docker-compose.lan.yml` switches Mosquitto to
`server/deploy/mosquitto-local-auth.conf`, `server/deploy/mosquitto.acl` and the
generated password file. Set `MQTT_USER` and `MQTT_PASS` in `.env` to match.
Production keeps its config in `server/deploy/mosquitto-wsb.conf`.

## Ride recorder

The dashboard includes a Rides & Recorded Tracks panel with track preview,
distance and duration, and PM/environment statistics. Endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/devices/{device_id}/rides` | Device upload of `text/csv` (or multipart `file`), authenticated by `X-Device-Id`/`X-Device-Key`; device id must match the URL; 5 MB / 20,000 rows (firmware uploads are capped at 2 MiB) |
| `GET` | `/api/devices/{device_id}/rides` | Ride summaries for a device |
| `GET` | `/api/rides/{ride_id}` | Ride detail, track and stats |
| `GET` | `/api/rides/{ride_id}/export.csv` | Full ride sample export |
| `DELETE` | `/api/rides/{ride_id}` | Delete a ride and its telemetry samples |

On the ESP32, insert a microSD card and use Start REC / Stop REC on the System
tab. The firmware writes samples to `/ride_YYYYMMDD_HHMMSS.csv` and uploads
completed files at most once per minute while Wi-Fi is available. After the
server confirms every row, the firmware removes the card copy. If removal fails,
it renames the file `.csv.done` and prunes it later. The firmware parks
permanent rejections as `.csv.bad`, and empty, oversized, header-only or
over-20,000-row files as `.csv.big`. It retries HTTP 401/403/408/429, so
re-provisioning a device recovers queued rides. The recorder needs a valid UTC
clock (NTP or GPS UTC). The firmware leaves PM columns empty until a
particulate-matter driver exists.

## Tests

Backend tests run offline against a temporary SQLite database. `conftest.py`
stubs the MQTT bridge. Tests cover legacy-schema migration and smoke tests for
track, series, rejected, command, simulate, profile and QR endpoints.

```bash
make test                 # pytest
make test-cov             # pytest --cov=app --cov-report=term-missing
```

`server/tests/conftest.py` sets `JWT_SECRET`, `MQTT_PASS`, `DB_PATH`,
`STATIC_DIR` and `MOSQUITTO_PASSWD_FILE` before importing the app, and creates a
fresh database for each test. Tests for known bugs use `xfail` with a reason.
The A-GPS test uses `strict=False`, so a fix reports XPASS.

## CI

`.github/workflows/ci.yml` runs on pushes to any branch, pull requests to `main`,
and `workflow_dispatch`. Timeouts: 20 minutes for server and web, 30 for
firmware.

| Job        | What it does                                                        |
|------------|---------------------------------------------------------------------|
| `server`   | Python 3.12, installs runtime + dev requirements, runs `pytest`     |
| `web`      | Node 22, `npm ci`, `npm run build`                                  |
| `firmware` | PlatformIO compile of `env:cyd` using `secrets.example.h`           |

Pip and npm caches use `actions/cache`. The workflow needs no repository
secrets.

## Backup

`server/deploy/backup.sh` copies the database with `sqlite3 .backup` and prunes
files older than `BACKUP_RETENTION_DAYS` (default 7; `0` disables pruning). The
script also accepts a legacy `RETENTION_DAYS`, but the app reads that variable as
telemetry retention, so set `BACKUP_RETENTION_DAYS` explicitly.

```bash
DB_PATH=/var/lib/wsb/telemetry.db BACKUP_DIR=/var/backups/wsb \
  server/deploy/backup.sh
```

The script exits when the database is missing. It writes a `.partial` file,
checks it with `PRAGMA quick_check`, then renames it to
`wsb-telemetry-<timestamp>.db`; an EXIT trap removes incomplete files. Backups
use `umask 077`, a `700` directory and `600` files. Schedule it with cron or
systemd.
