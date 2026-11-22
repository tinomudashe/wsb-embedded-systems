# wsb-embedded-systems

IoT air-quality + GPS telemetry: an ESP32 CYD firmware (`firmware/`), a FastAPI +
SQLite backend with an MQTT bridge (`server/`), and a React dashboard (`web/`).

## Local development

Prerequisites: Python 3.12+ (3.14 works), Node 22+, and — for the container
stack — Docker. `pio` is only needed to compile firmware. `make test` refuses
older Python interpreters with a clear error.

```bash
cp .env.example .env      # set JWT_SECRET and MQTT_PASS (and MQTT_USER if desired)
make venv                 # creates server/.venv, installs runtime + dev deps
```

`.env` is **compose-only**: Docker Compose reads it automatically; `uvicorn` and
the other tools do not. For a manual API run, export it in the shell from the
repository root so the relative `DB_PATH`/`STATIC_DIR` values resolve correctly:

```bash
set -a; source .env; set +a
server/.venv/bin/uvicorn app.main:app --app-dir server --reload --port 8000
```

Build the dashboard into `server/static` (the API serves it at `/`):

```bash
make build-web            # cd web && npm ci && npm run build
cd web && npm run dev     # or use the Vite dev server during UI work
```

Firmware (PlatformIO; `include/secrets.h` is git-ignored). `make build-firmware`
copies `secrets.example.h` automatically if `secrets.h` is missing — edit the
copied credentials before flashing:

```bash
make build-firmware       # cd firmware && pio run -e cyd
```

### Container stack

The Docker build is multi-stage: a `node:22-slim` stage runs `npm ci` and
`npm run build`, then the built dashboard is copied into the Python runtime
image at `/app/static`. No stale committed `server/static` is used.

```bash
cp .env.example .env      # make dev-up fails with a clear message if this is missing
make dev-up               # docker compose up --build -d
curl localhost:8000/api/health
make dev-down
```

Host ports bind to **loopback only** by default (`127.0.0.1:8000` and
`127.0.0.1:1883`). The bundled local broker
(`server/deploy/mosquitto-local.conf`) allows anonymous access, which is safe
only because it is not exposed beyond the machine.

For real firmware testing on the LAN, use the explicit authenticated override —
never the anonymous broker:

```bash
# One-time password file outside the repo (use the same values as .env).
mkdir -p /tmp/wsb-mosquitto
mosquitto_passwd -c -b /tmp/wsb-mosquitto/passwd backend "$MQTT_PASS"
mosquitto_passwd -b /tmp/wsb-mosquitto/passwd cyd-01 "$DEVICE_KEY"
chmod 644 /tmp/wsb-mosquitto/passwd   # local-only file; broker runs as uid 1883

API_BIND=0.0.0.0 MQTT_BIND=0.0.0.0 \
  docker compose -f docker-compose.yml -f docker-compose.lan.yml up --build
```

`docker-compose.lan.yml` switches Mosquitto to
`server/deploy/mosquitto-local-auth.conf` plus `server/deploy/mosquitto.acl`
and the generated password file. Set `MQTT_USER`/`MQTT_PASS` in `.env` to match
the password file; the production password/ACL model remains in
`server/deploy/mosquitto-wsb.conf`.

## Ride recorder

The dashboard includes a **Rides & Recorded Tracks** panel (track preview,
distance/duration and PM/environment statistics) and these endpoints are
available:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/devices/{device_id}/rides` | Device upload of `text/csv` (or multipart `file`), authenticated by `X-Device-Id`/`X-Device-Key`; device id must match the URL; 5 MB / 20,000 rows (firmware uploads are capped at 2 MiB) |
| `GET` | `/api/devices/{device_id}/rides` | Ride summaries for a device |
| `GET` | `/api/rides/{ride_id}` | Ride detail, track and stats |
| `GET` | `/api/rides/{ride_id}/export.csv` | Full ride sample export |
| `DELETE` | `/api/rides/{ride_id}` | Delete a ride and its telemetry samples |

On the ESP32, insert a microSD card and use **Start REC / Stop REC** on the
System tab. Samples are buffered and appended to `/ride_YYYYMMDD_HHMMSS.csv`;
completed files upload at most once per minute while Wi-Fi is available, and
after the server confirms every row the copy is removed from the card (the
server is the durable copy; a copy that cannot be removed is renamed `.csv.done`
and pruned later). Files the server rejects permanently are parked as
`.csv.bad`; empty, oversized, header-only or over-20,000-row files are parked as
`.csv.big`. HTTP 401/403/408/429 are retried, so re-provisioning a device
recovers its queued rides.
Recording requires a valid UTC clock (NTP or GPS UTC); PM columns stay empty
until a particulate-matter sensor driver is added.

## Tests

Backend tests run offline against a throw-away SQLite database; the MQTT bridge
is stubbed so no broker is required. The suite includes legacy-schema migration
coverage and smoke tests for track/series/rejected/command/simulate/profile/QR
endpoints.

```bash
make test                 # pytest
make test-cov             # pytest --cov=app --cov-report=term-missing
```

`server/tests/conftest.py` sets `JWT_SECRET`, `MQTT_PASS`, `DB_PATH`,
`STATIC_DIR` and `MOSQUITTO_PASSWD_FILE` before importing the app, and rebuilds
a fresh database per test. Tests that document known, unfixed behavior are
marked `xfail` with a reason; the A-GPS expectation uses `strict=False` so a
future fix reports XPASS rather than failing the suite.

## CI

`.github/workflows/ci.yml` runs on pushes to any branch, pull requests targeting
`main`, and manual dispatch (`workflow_dispatch`). Jobs have timeouts
(server/web 20 min, firmware 30 min):

| Job        | What it does                                                        |
|------------|---------------------------------------------------------------------|
| `server`   | Python 3.12, installs runtime + dev requirements, runs `pytest`     |
| `web`      | Node 22, `npm ci`, `npm run build`                                  |
| `firmware` | PlatformIO compile of `env:cyd` using `secrets.example.h`           |

Pip and npm caches use `actions/cache`; no repository secrets are needed.

## Backup

`server/deploy/backup.sh` makes a consistent online copy with `sqlite3 .backup`
and prunes files older than `BACKUP_RETENTION_DAYS` (default 7; `0` disables
pruning). A legacy `RETENTION_DAYS` is accepted as a fallback, but it is the
server's telemetry retention setting, not a backup setting, so set
`BACKUP_RETENTION_DAYS` explicitly (for example from a wrapper that makes it
available to the cron job).

```bash
DB_PATH=/var/lib/wsb/telemetry.db BACKUP_DIR=/var/backups/wsb \
  server/deploy/backup.sh
```

It exits cleanly when the database does not exist yet. Copies are written to a
`.partial` file, checked with `PRAGMA quick_check`, and only then renamed to
`wsb-telemetry-<timestamp>.db`; an EXIT trap removes incomplete files. Backups
use `umask 077`, a `700` directory and `600` files, so they are private to the
account running the script. Schedule it with cron/systemd if you want automatic
backups.
