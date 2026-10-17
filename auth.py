"""Password hashing and opaque cookie-session helpers, isolated from media logic."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from database import create_session, delete_session, session_user

SESSION_LIFETIME_DAYS = 30
COOKIE_NAME = "simi_session"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Use a password with at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = encoded.split("$", 2)
        if algorithm != "scrypt": return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def create_login_session(user_id: str) -> str:
    token = secrets.token_urlsafe(40)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expiry = (datetime.now(timezone.utc) + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat(timespec="seconds")
    create_session(user_id, digest, expiry)
    return token


def resolve_session(token: str | None) -> dict | None:
    if not token: return None
    return session_user(hashlib.sha256(token.encode()).hexdigest())


def revoke_session(token: str | None) -> None:
    if token: delete_session(hashlib.sha256(token.encode()).hexdigest())
