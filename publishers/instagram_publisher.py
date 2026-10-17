"""Instagram professional-account Reel adapter using Meta's media publishing API."""
from __future__ import annotations

import time
import re
from urllib.parse import urlparse

import requests

import oauth_service
from .base import PublishResult, PublishingError, checked_video, graph_request, graph_version


def publish_reel(*, user_id: str, video_path: str, media_url: str, caption: str, hashtags: list[str]) -> PublishResult:
    video = checked_video(video_path)
    _, credentials = oauth_service.credentials_for(user_id, "instagram")
    token = credentials.get("access_token")
    account_id = credentials.get("instagram_account_id") or credentials.get("account_id")
    if not isinstance(token, str) or not isinstance(account_id, str):
        raise PublishingError("Instagram professional account authorization is incomplete. Disconnect and connect it again.")
    parsed = urlparse(media_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublishingError("Instagram requires a public HTTPS URL for this user's validated video.")
    version = graph_version()
    base = f"https://graph.facebook.com/{version}"
    existing={tag.casefold() for tag in re.findall(r"#\w+",caption)}
    additions=[f"#{tag.lstrip('#')}" for tag in hashtags if tag.strip() and f"#{tag.lstrip('#')}".casefold() not in existing]
    description = " ".join([caption.strip(), *additions]).strip()
    created = graph_request(f"{base}/{account_id}/media", token=token, data={"media_type": "REELS", "video_url": media_url, "caption": description[:2200]})
    container_id = created.get("id")
    if not isinstance(container_id, str):
        raise PublishingError("Instagram did not create a Reel media container.")
    ready = False
    for _ in range(60):
        try:
            response = requests.get(f"{base}/{container_id}", params={"fields": "status_code"}, headers={"Authorization": f"Bearer {token}"}, timeout=(15, 30))
            response.raise_for_status()
            status = response.json().get("status_code")
        except (requests.RequestException, requests.JSONDecodeError, AttributeError) as exc:
            raise PublishingError("Could not check Instagram Reel processing status.") from exc
        if status == "FINISHED":
            ready = True
            break
        if status == "ERROR":
            raise PublishingError("Instagram could not process this video. Check Reel format requirements.")
        time.sleep(5)
    if not ready:
        raise PublishingError("Instagram Reel processing timed out. Check the account before retrying.")
    result = graph_request(f"{base}/{account_id}/media_publish", token=token, data={"creation_id": container_id})
    media_id = result.get("id")
    if not isinstance(media_id, str):
        raise PublishingError("Instagram did not confirm the Reel publication.")
    return PublishResult("instagram", media_id)
