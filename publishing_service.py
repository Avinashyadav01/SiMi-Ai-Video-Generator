"""At-most-once orchestration around validated jobs and platform adapters."""
from __future__ import annotations

import os
import re

import database
import media_service
import oauth_service
from publishers import PublishingError, publish
from publishers.base import checked_video


def publish_job_platform(user_id: str, job_id: str, platform: str) -> dict:
    if os.getenv("PUBLISHING_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        raise PublishingError("Publishing is disabled. Set PUBLISHING_ENABLED=true only after OAuth and platform setup are complete.")
    existing=database.get_job(job_id,user_id)
    if not existing or not existing.get("video_path"):
        raise PublishingError("The validated video was not found for this account.")
    # Perform all local checks before writing the at-most-once upload claim.
    oauth_service.credentials_for(user_id,platform)
    checked_video(existing["video_path"])
    media_url = media_service.make_media_url(user_id, job_id) if platform == "instagram" else None
    job = database.claim_publish(user_id, job_id, platform)
    try:
        caption=job.get("caption") or ""
        hashtags=(job.get("metadata") or {}).get("hashtags") or re.findall(r"#([\w\u0900-\u097F]+)",caption)
        result = publish(platform, user_id=user_id, video_path=job["video_path"], title=job.get("title") or job["topic"], caption=caption, hashtags=hashtags, media_url=media_url)
        return database.finish_publish(user_id, job_id, platform, result.external_post_id, result.permalink)
    except Exception as exc:
        database.fail_publish(user_id, job_id, platform, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, PublishingError):
            raise
        raise PublishingError("Publishing failed. Check the platform account before retrying to avoid a duplicate post.") from exc
