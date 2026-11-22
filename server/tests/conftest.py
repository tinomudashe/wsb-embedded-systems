"""Shared pytest fixtures for the FastAPI backend.

`app.config` calls ``sys.exit(1)`` when JWT_SECRET or MQTT_PASS is missing, so
this module sets the environment before importing any `app.*` module. pytest
imports conftest.py before test modules.

Tests use no broker. The `client` fixture stubs `app.mqtt_bridge.start/stop` and
the publish helpers. Data goes to a temporary SQLite file.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="wsb-server-tests-"))
TEST_DB = _TMP_ROOT / "telemetry-test.db"

# --- must happen before `from app import ...` anywhere -----------------------
os.environ.update(
    {
        "JWT_SECRET": "test-only-jwt-secret-not-valid-outside-tests",
        "MQTT_PASS": "test-only-mqtt-password",
        "DB_PATH": str(TEST_DB),
        "STATIC_DIR": str(SERVER_DIR / "static"),
        "MQTT_HOST": "127.0.0.1",
        "MQTT_PORT": "1883",
        "JWT_TTL_HOURS": "12",
        "RETENTION_DAYS": "90",
        "OFFLINE_AFTER_SEC": "90",
        "PUBLIC_BASE_URL": "http://testserver",
        # Never touch a developer's real /var/lib/wsb/mosquitto_passwd.
        "MOSQUITTO_PASSWD_FILE": str(_TMP_ROOT / "mosquitto_passwd"),
    }
)

import pytest  # noqa: E402  (import after env setup by design)

_DB_FILES = (TEST_DB, Path(f"{TEST_DB}-wal"), Path(f"{TEST_DB}-shm"))


def _close_db() -> None:
    """Drop the module-level sqlite connection so the next init() starts clean."""
    from app import db

    conn = db._conn
    if conn is not None:
        with suppress(Exception):
            conn.close()
    db._conn = None


def _remove_db_files() -> None:
    for path in _DB_FILES:
        with suppress(FileNotFoundError):
            path.unlink()


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Reset global caches that would otherwise leak between tests."""
    from app import auth, store

    auth._failures.clear()
    store._key_cache.clear()
    yield
    auth._failures.clear()
    store._key_cache.clear()


@pytest.fixture()
def database():
    """A fresh, empty SQLite database for a single test.

    The schema is created by ``app.db.init()`` exactly like production startup.
    """
    from app import db, store

    _close_db()
    _remove_db_files()
    store._key_cache.clear()
    db.init()
    try:
        yield db
    finally:
        _close_db()
        _remove_db_files()


@pytest.fixture()
def client(database, monkeypatch):
    """FastAPI TestClient with broker integration stubbed out."""
    from fastapi.testclient import TestClient

    from app import main, mqtt_bridge
    from app.live import hub

    # Stub the broker: no TCP connection, no publish.
    monkeypatch.setattr(mqtt_bridge, "start", lambda: None)
    monkeypatch.setattr(mqtt_bridge, "stop", lambda: None)
    monkeypatch.setattr(mqtt_bridge, "publish_config", lambda device_id: False)
    monkeypatch.setattr(mqtt_bridge, "publish_command", lambda device_id, command: False)

    # The app lifespan calls db.init(); close the fixture's connection first so
    # only one connection exists at a time.
    _close_db()
    with TestClient(main.app) as test_client:
        yield test_client
    hub._loop = None


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Remove the temp database directory at the end of the test session."""
    from app import db

    _close_db()
    _remove_db_files()
    with suppress(OSError):
        _TMP_ROOT.rmdir()
