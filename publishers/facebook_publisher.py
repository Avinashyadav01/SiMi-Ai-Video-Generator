"""Facebook Page Reels upload adapter using Meta's local-file upload flow."""
from __future__ import annotations

import re
from urllib.parse import urlencode

import requests

import oauth_service
from .base import PublishResult, PublishingError, checked_video, graph_request, graph_version


def publish_reel(*, user_id: str, video_path: str, title: str, caption: str, hashtags: list[str]) -> PublishResult:
    video = checked_video(video_path)
    _, credentials = oauth_service.credentials_for(user_id, "facebook")
    token = credentials.get("access_token")
    if not isinstance(token, str):
        raise PublishingError("Facebook Page authorization is incomplete. Reconnect the account.")
    version = graph_version()
    base = f"https://graph.facebook.com/{version}"
    session = graph_request(f"{base}/me/video_reels", token=token, data={"upload_phase": "start"})
    video_id, upload_url = session.get("video_id"), session.get("upload_url")
    if not isinstance(video_id, str) or not isinstance(upload_url, str) or not upload_url.startswith("https://rupload.facebook.com/"):
        raise PublishingError("Meta did not return a valid Facebook Reel upload session.")
    try:
        with video.open("rb") as source:
            uploaded = requests.post(upload_url, data=source, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(video.stat().st_size), "Content-Type": "application/octet-stream"}, timeout=(30, 900))
    except requests.RequestException as exc:
        raise PublishingError("The Facebook video upload connection failed. Check publishing history before retrying.") from exc
    if not uploaded.ok:
        raise PublishingError(f"Facebook rejected the video upload (HTTP {uploaded.status_code}).")
    try:
        upload_result = uploaded.json()
    except requests.JSONDecodeError as exc:
        raise PublishingError("Facebook returned an unreadable video upload response.") from exc
    if not isinstance(upload_result, dict) or upload_result.get("success") is not True:
        raise PublishingError("Facebook did not confirm the Reel video upload.")
    existing={tag.casefold() for tag in re.findall(r"#\w+",caption)}
    additions=[f"#{tag.lstrip('#')}" for tag in hashtags if tag.strip() and f"#{tag.lstrip('#')}".casefold() not in existing]
    description = " ".join([caption.strip(), *additions]).strip()
    result = graph_request(f"{base}/me/video_reels", token=token, data={"video_id": video_id, "upload_phase": "finish", "video_state": "PUBLISHED", "title": title[:100], "description": description[:5000]})
    if result.get("success") is not True:
        raise PublishingError("Facebook did not confirm the Reel publication.")
    return PublishResult("facebook", video_id)
