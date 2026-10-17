"""Short-lived public video URLs for platform APIs that fetch media by URL."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode, urlparse

from publishers.base import PublishingError


def _key() -> bytes:
    value = os.getenv("MEDIA_SIGNING_KEY", "").strip().encode("utf-8")
    if len(value) < 32:
        raise PublishingError("Set a private MEDIA_SIGNING_KEY of at least 32 characters before Instagram publishing.")
    return value


def make_media_url(user_id: str, job_id: str, *, lifetime: int = 1800) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed=urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublishingError("PUBLIC_BASE_URL must be a public HTTPS address for Instagram video delivery.")
    expires = int(time.time()) + min(max(60, lifetime), 3600)
    payload = f"{user_id}:{job_id}:{expires}".encode()
    signature = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    return f"{base}/public/users/{user_id}/videos/{job_id}?" + urlencode({"expires": expires, "signature": signature})


def verify_signature(user_id: str, job_id: str, expires: int, signature: str) -> bool:
    now = int(time.time())
    if expires < now or expires > now + 3600 or len(signature) != 64:
        return False
    try:
        expected = hmac.new(_key(), f"{user_id}:{job_id}:{expires}".encode(), hashlib.sha256).hexdigest()
    except PublishingError:
        return False
    return hmac.compare_digest(expected, signature)
