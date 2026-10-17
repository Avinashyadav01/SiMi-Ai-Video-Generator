"""YouTube Data API resumable upload adapter."""
from __future__ import annotations

from urllib.parse import urlencode

import requests

import oauth_service
from .base import PublishResult, PublishingError, checked_video


def publish_short(*, user_id: str, video_path: str, title: str, caption: str, hashtags: list[str]) -> PublishResult:
    video = checked_video(video_path)
    connection, credentials = oauth_service.credentials_for(user_id, "youtube")
    access = credentials.get("access_token")
    if not isinstance(access, str):
        raise PublishingError("YouTube account authorization is incomplete. Reconnect the account.")
    tags = [str(tag).lstrip("#")[:100] for tag in hashtags if str(tag).strip()][:15]
    snippet = {"title": title[:100], "description": caption[:5000], "categoryId": "22"}
    if tags:
        snippet["tags"] = tags
    body = {"snippet": snippet, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    start_url = "https://www.googleapis.com/upload/youtube/v3/videos?" + urlencode({"uploadType": "resumable", "part": "snippet,status"})
    try:
        start = requests.post(start_url, json=body, headers={"Authorization": f"Bearer {access}", "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video.stat().st_size)}, timeout=(20, 60))
        if start.status_code not in (200, 201) or not start.headers.get("Location"):
            raise PublishingError(f"YouTube did not start a resumable upload (HTTP {start.status_code}). Check channel access and API quota.")
        with video.open("rb") as source:
            uploaded = requests.put(start.headers["Location"], data=source, headers={"Content-Type": "video/mp4", "Content-Length": str(video.stat().st_size)}, timeout=(30, 900))
    except requests.RequestException as exc:
        raise PublishingError("The YouTube upload connection failed. Check publishing history before retrying to avoid duplicates.") from exc
    if uploaded.status_code not in (200, 201):
        raise PublishingError(f"YouTube upload failed (HTTP {uploaded.status_code}). Check publishing history before retrying.")
    try:
        result = uploaded.json()
    except requests.JSONDecodeError as exc:
        raise PublishingError("YouTube did not return a valid upload result.") from exc
    video_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(video_id, str) or not video_id:
        raise PublishingError("YouTube did not confirm the uploaded video ID.")
    return PublishResult("youtube", video_id, f"https://www.youtube.com/shorts/{video_id}")
