"""Unit tests for JWT sessions, bcrypt secrets and device-key verification."""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from app import auth, config, store

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def user(database):  # noqa: ARG001 - database fixture guarantees a fresh schema
    store.upsert_user("alice", PASSWORD, full_name="Alice")
    return "alice"


def test_issue_and_verify_token(user):
    token = auth.issue_token(user)

    assert isinstance(token, str)
    payload = pyjwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    assert payload["sub"] == user
    assert payload["exp"] > payload["iat"]
    assert auth.verify_token(token) == user


def test_verify_token_rejects_missing_or_garbage():
    assert auth.verify_token(None) is None
    assert auth.verify_token("") is None
    assert auth.verify_token("not-a-jwt") is None


def test_tampered_token_is_rejected(user):
    token = auth.issue_token(user)
    payload = pyjwt.decode(token, options={"verify_signature": False})
    payload["sub"] = "mallory"
    forged = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")

    assert auth.verify_token(forged) is None
    assert auth.verify_token(token + "x") is None


def test_expired_token_is_rejected(user):
    now = int(time.time())
    expired = pyjwt.encode(
        {"sub": user, "iat": now - 7200, "exp": now - 3600},
        config.JWT_SECRET,
        algorithm="HS256",
    )

    assert auth.verify_token(expired) is None


def test_token_for_unknown_user_is_rejected(user):  # noqa: ARG001
    token = auth.issue_token("nobody")

    assert auth.verify_token(token) is None


def test_password_hash_and_check(user):  # noqa: ARG001
    hashed = store.hash_secret("hunter2", rounds=4)

    assert hashed != "hunter2"
    assert store.check_secret("hunter2", hashed) is True
    assert store.check_secret("hunter3", hashed) is False
    assert store.check_secret("hunter2", "not-a-bcrypt-hash") is False


def test_check_credentials(user):
    assert auth.check_credentials(user, PASSWORD) is True
    assert auth.check_credentials(user, "wrong") is False
    assert auth.check_credentials("unknown-user", PASSWORD) is False


def test_device_key_verification(database):  # noqa: ARG001
    key = store.create_device("cyd-auth", name="Auth CYD", owner_username="alice")

    assert key
    assert store.verify_device_key("cyd-auth", key) is True
    assert store.verify_device_key("cyd-auth", key + "-wrong") is False
    assert store.verify_device_key("unknown-device", key) is False
    assert store.verify_device_key("cyd-auth", None) is False
    assert store.verify_device_key(None, key) is False


def test_device_key_rotation_invalidates_old_key(database):  # noqa: ARG001
    old_key = store.create_device("cyd-rotate", name="Rotating CYD")
    store.set_device_key("cyd-rotate", "brand-new-key")

    assert store.verify_device_key("cyd-rotate", "brand-new-key") is True
    assert store.verify_device_key("cyd-rotate", old_key) is False
