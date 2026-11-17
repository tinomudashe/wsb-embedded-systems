# wsb-embedded-systems

IoT air-quality + GPS telemetry: an ESP32 CYD firmware (`firmware/`), a FastAPI +
SQLite backend with an MQTT bridge (`server/`), and a React dashboard (`web/`).

## Local development

Prerequisites: Python 3.12+ (3.14 works), Node 22+, and — for the container
stack — Docker. `pio` is only needed to compile firmware.

```bash
cp .env.example .env      # then set JWT_SECRET and MQTT_PASS
make venv                 # creates server/.venv, installs runtime + dev deps
```

Run the API (defaults read `.env`-style variables from the process environment):

```bash
cd server
DB_PATH=../server/data/telemetry.db STATIC_DIR=../server/static \
JWT_SECRET=dev-secret MQTT_PASS=dev-pass \
  .venv/bin/uvicorn app.main:app --reload --port 8000
```

Build the dashboard into `server/static` (the API serves it at `/`):

```bash
make build-web            # cd web && npm ci && npm run build
cd web && npm run dev     # or use the Vite dev server during UI work
```

Firmware (PlatformIO; `include/secrets.h` is git-ignored):

```bash
cp firmware/include/secrets.example.h firmware/include/secrets.h
make build-firmware       # cd firmware && pio run -e cyd
```

Container stack (API + Mosquitto, named volume for `/var/lib/wsb`):

```bash
cp .env.example .env
make dev-up               # docker compose up --build -d
curl localhost:8000/api/health
make dev-down
```

The local broker (`server/deploy/mosquitto-local.conf`) allows anonymous access
and is for development only; production keeps the password/ACL config in
`server/deploy/mosquitto-wsb.conf`.

## Tests

Backend tests run offline against a throw-away SQLite database; the MQTT bridge
is stubbed so no broker is required.

```bash
make test                 # pytest
make test-cov             # pytest --cov=app --cov-report=term-missing
```

`server/tests/conftest.py` sets `JWT_SECRET`, `MQTT_PASS` and a temp `DB_PATH`
before importing the app, and rebuilds a fresh database per test. Tests that
document known, unfixed behavior are marked `xfail` with a reason.

## CI

`.github/workflows/ci.yml` runs on pushes and pull requests to `main`:

| Job        | What it does                                                        |
|------------|---------------------------------------------------------------------|
| `server`   | Python 3.12, installs runtime + dev requirements, runs `pytest`     |
| `web`      | Node 22, `npm ci`, `npm run build`                                  |
| `firmware` | PlatformIO compile of `env:cyd` using `secrets.example.h`           |

Pip and npm caches use `actions/cache`; no repository secrets are needed.

## Backup

`server/deploy/backup.sh` makes a consistent online copy with `sqlite3 .backup`
and prunes files older than `RETENTION_DAYS` (default 7).

```bash
DB_PATH=/var/lib/wsb/telemetry.db BACKUP_DIR=/var/backups/wsb \
  server/deploy/backup.sh
```

It exits cleanly when the database does not exist yet and verifies each copy
with `PRAGMA quick_check`. Schedule it with cron/systemd if you want automatic
backups.
