"""Official social-platform publishing adapters."""
from __future__ import annotations

from typing import Any

from .base import PublishResult, PublishingError
from .facebook_publisher import publish_reel as publish_facebook_reel
from .instagram_publisher import publish_reel as publish_instagram_reel
from .youtube_publisher import publish_short as publish_youtube_short


def publish(platform: str, *, user_id: str, video_path: str, title: str, caption: str, hashtags: list[str] | None = None, media_url: str | None = None) -> PublishResult:
    if platform == "youtube":
        return publish_youtube_short(user_id=user_id, video_path=video_path, title=title, caption=caption, hashtags=hashtags or [])
    if platform == "instagram":
        if not media_url:
            raise PublishingError("Instagram publishing needs a short-lived public HTTPS URL for this user's validated video.")
        return publish_instagram_reel(user_id=user_id, video_path=video_path, media_url=media_url, caption=caption, hashtags=hashtags or [])
    if platform == "facebook":
        return publish_facebook_reel(user_id=user_id, video_path=video_path, title=title, caption=caption, hashtags=hashtags or [])
    raise PublishingError("Unsupported publishing platform.")


__all__ = ["PublishResult", "PublishingError", "publish"]
