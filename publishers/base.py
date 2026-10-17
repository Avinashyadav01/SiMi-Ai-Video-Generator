"""Shared publishing result types and validation helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quality_validator import QualityValidationError, validate_video


class PublishingError(RuntimeError):
    """A failed upload or missing platform configuration."""


@dataclass(frozen=True)
class PublishResult:
    platform: str
    external_post_id: str
    permalink: str | None = None
    status: str = "PUBLISHED"


def checked_video(video_path: str) -> Path:
    path = Path(video_path).expanduser().resolve()
    try:
        validate_video(path)
    except QualityValidationError as exc:
        raise PublishingError(f"Video failed quality validation; it was not uploaded: {exc}") from exc
    return path


def graph_version() -> str:
    import os

    version = os.getenv("META_GRAPH_VERSION", "").strip()
    if not version or not version.startswith("v") or not version[1:].replace(".", "").isdigit():
        raise PublishingError("Set META_GRAPH_VERSION to a supported Graph API version before publishing.")
    return version


def graph_request(url: str, *, token: str, method: str = "POST", data: dict | None = None) -> dict:
    import requests

    try:
        response = requests.request(method, url, data=data, headers={"Authorization": f"Bearer {token}"}, timeout=(15, 60))
    except requests.RequestException as exc:
        raise PublishingError("Could not reach the Meta publishing API. Check connectivity and retry.") from exc
    if not response.ok:
        raise PublishingError(f"Meta rejected the publishing request (HTTP {response.status_code}). Check account permissions and video requirements.")
    try:
        result = response.json()
    except requests.JSONDecodeError as exc:
        raise PublishingError("Meta returned an unreadable publishing response.") from exc
    if not isinstance(result, dict) or result.get("error"):
        raise PublishingError("Meta did not confirm the publishing request.")
    return result
