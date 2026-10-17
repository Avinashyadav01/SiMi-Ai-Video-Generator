"""User-scoped OAuth flows and encrypted storage for supported social accounts."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import database


class OAuthError(RuntimeError):
    """A safe-to-display OAuth configuration or provider error."""


PLATFORMS = {"youtube", "instagram", "facebook"}


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def _fernet() -> Fernet:
    key = _setting("OAUTH_ENCRYPTION_KEY")
    if not key:
        raise OAuthError("Platform connections are not configured yet. Set OAUTH_ENCRYPTION_KEY first.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise OAuthError("OAUTH_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def configured(platform: str) -> bool:
    if not _setting("OAUTH_ENCRYPTION_KEY"):
        return False
    try:
        _fernet()
    except OAuthError:
        return False
    if platform == "youtube":
        return all(_setting(key) for key in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"))
    if platform in {"instagram", "facebook"}:
        redirect_key = "META_INSTAGRAM_REDIRECT_URI" if platform == "instagram" else "META_FACEBOOK_REDIRECT_URI"
        return all(_setting(key) for key in ("META_APP_ID", "META_APP_SECRET", redirect_key, "META_GRAPH_VERSION"))
    return False


def authorization_url(platform: str, user_id: str) -> str:
    if platform not in PLATFORMS:
        raise OAuthError("Unsupported platform.")
    if not configured(platform):
        raise OAuthError(f"{platform.title()} OAuth credentials and redirect URL are not configured.")
    _fernet()  # Fail before sending the user to consent if tokens cannot be secured.
    state = secrets.token_urlsafe(32)
    digest = hashlib.sha256(state.encode()).hexdigest()
    database.create_oauth_state(digest, user_id, platform, (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
    if platform == "youtube":
        params = {
            "client_id": _setting("GOOGLE_OAUTH_CLIENT_ID"),
            "redirect_uri": _setting("GOOGLE_OAUTH_REDIRECT_URI"),
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    version = _setting("META_GRAPH_VERSION")
    params = {
        "client_id": _setting("META_APP_ID"),
        "redirect_uri": _setting("META_INSTAGRAM_REDIRECT_URI" if platform == "instagram" else "META_FACEBOOK_REDIRECT_URI"),
        "response_type": "code",
        "scope": "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish",
        "state": state,
    }
    return f"https://www.facebook.com/{version}/dialog/oauth?" + urllib.parse.urlencode(params)


def complete(platform: str, code: str, state: str) -> str:
    if platform not in PLATFORMS or not code or len(code) > 4096 or not state or len(state) > 256:
        raise OAuthError("The platform authorization response is invalid.")
    user_id = database.consume_oauth_state(hashlib.sha256(state.encode()).hexdigest(), platform)
    if not user_id:
        raise OAuthError("This authorization request expired or was already used. Please connect again.")
    if not configured(platform):
        raise OAuthError("Platform OAuth credentials or callback URL are missing from server configuration.")
    token = _exchange_code(platform, code)
    expiry = _expiry(token)
    if platform == "youtube":
        _save_youtube(user_id, token, expiry)
    else:
        _save_meta(user_id, token, expiry, platform)
    return user_id


def _request_json(url: str, *, method: str = "GET", form: dict[str, str] | None = None, bearer: str | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"Accept": "application/json", "User-Agent": "SiMiContentStudio/1.0"}
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Provider payloads can echo request details; never copy them to logs/UI.
        raise OAuthError(f"The platform authorization request failed (HTTP {exc.code}). Check app settings and requested permissions.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError("Could not reach the platform authorization service. Check the internet connection and try again.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("The platform returned an unreadable authorization response.") from exc
    if not isinstance(result, dict):
        raise OAuthError("The platform returned an invalid authorization response.")
    if result.get("error"):
        raise OAuthError("The platform denied authorization or one of the required permissions is missing.")
    return result


def _exchange_code(platform: str, code: str) -> dict[str, Any]:
    if platform == "youtube":
        token = _request_json("https://oauth2.googleapis.com/token", method="POST", form={
            "code": code,
            "client_id": _setting("GOOGLE_OAUTH_CLIENT_ID"),
            "client_secret": _setting("GOOGLE_OAUTH_CLIENT_SECRET"),
            "redirect_uri": _setting("GOOGLE_OAUTH_REDIRECT_URI"),
            "grant_type": "authorization_code",
        })
    else:
        version = _setting("META_GRAPH_VERSION")
        token = _request_json(f"https://graph.facebook.com/{version}/oauth/access_token", form={
            "client_id": _setting("META_APP_ID"),
            "client_secret": _setting("META_APP_SECRET"),
            "redirect_uri": _setting("META_INSTAGRAM_REDIRECT_URI" if platform == "instagram" else "META_FACEBOOK_REDIRECT_URI"),
            "code": code,
        })
        short_token = token.get("access_token")
        if not isinstance(short_token, str):
            raise OAuthError("The platform did not issue an access token.")
        token = _request_json(f"https://graph.facebook.com/{version}/oauth/access_token?" + urllib.parse.urlencode({
            "grant_type": "fb_exchange_token",
            "client_id": _setting("META_APP_ID"),
            "client_secret": _setting("META_APP_SECRET"),
            "fb_exchange_token": short_token,
        }))
    if not isinstance(token.get("access_token"), str):
        raise OAuthError("The platform did not issue an access token.")
    return token


def _expiry(token: dict[str, Any]) -> str | None:
    try:
        seconds = max(0, int(token.get("expires_in", 0)))
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds") if seconds else None


def _save_youtube(user_id: str, token: dict[str, Any], expiry: str | None) -> None:
    access = token["access_token"]
    channels = _request_json("https://www.googleapis.com/youtube/v3/channels?part=id%2Csnippet&mine=true", bearer=access)
    items = channels.get("items") or []
    if not items or not isinstance(items[0].get("id"), str):
        raise OAuthError("No YouTube channel was found on the authorized Google account.")
    token_record = {"access_token": access, "refresh_token": token.get("refresh_token"), "token_type": token.get("token_type", "Bearer"), "scope": token.get("scope", "")}
    encrypted = _fernet().encrypt(json.dumps(token_record, separators=(",", ":")).encode())
    database.save_platform_connection(user_id, "youtube", items[0]["id"], encrypted, expiry)


def _save_meta(user_id: str, token: dict[str, Any], expiry: str | None, requested_platform: str) -> None:
    access = token["access_token"]
    version = _setting("META_GRAPH_VERSION")
    pages = _request_json(f"https://graph.facebook.com/{version}/me/accounts?fields=id%2Cname%2Caccess_token%2Cinstagram_business_account%7Bid%2Cusername%7D", bearer=access).get("data") or []
    if not pages:
        raise OAuthError("No Facebook Page was found. Add your account as a Page administrator and try again.")
    page = next((item for item in pages if isinstance(item.get("id"), str) and isinstance(item.get("access_token"), str) and (requested_platform != "instagram" or isinstance(item.get("instagram_business_account"), dict))), None)
    if not page:
        raise OAuthError("No Facebook Page with publishing access was returned.")
    ig = page.get("instagram_business_account")
    if requested_platform == "instagram" and not (isinstance(ig, dict) and isinstance(ig.get("id"), str)):
        raise OAuthError("No Instagram professional account linked to the selected Facebook Page was found. Link an Instagram Business or Creator account and try again.")
    fernet = _fernet()
    fb_encrypted = fernet.encrypt(json.dumps({"access_token": page["access_token"], "user_access_token": access, "page_id": page["id"]}, separators=(",", ":")).encode())
    database.save_platform_connection(user_id, "facebook", page["id"], fb_encrypted, expiry)
    if isinstance(ig, dict) and isinstance(ig.get("id"), str):
        ig_encrypted = fernet.encrypt(json.dumps({"access_token": page["access_token"], "user_access_token": access, "page_id": page["id"], "instagram_account_id": ig["id"]}, separators=(",", ":")).encode())
        database.save_platform_connection(user_id, "instagram", ig["id"], ig_encrypted, expiry)


def decrypt_connection(connection: dict[str, Any]) -> dict[str, Any]:
    ciphertext = connection.get("token_ciphertext")
    if not ciphertext:
        raise OAuthError("This platform account is disconnected. Connect it again.")
    try:
        result = json.loads(_fernet().decrypt(bytes(ciphertext)).decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("The saved platform credentials cannot be decrypted. Check OAUTH_ENCRYPTION_KEY.") from exc
    if not isinstance(result, dict):
        raise OAuthError("The saved platform credentials are invalid.")
    return result


def credentials_for(user_id: str, platform: str) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = database.get_platform_connection(user_id, platform)
    if not connection or connection.get("status") != "CONNECTED" or not connection.get("enabled"):
        raise OAuthError(f"Connect your {platform.title()} account before publishing.")
    payload = decrypt_connection(connection)
    if platform == "youtube" and connection.get("token_expires_at"):
        try:
            expires = datetime.fromisoformat(connection["token_expires_at"])
        except ValueError:
            expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expires <= datetime.now(timezone.utc) + timedelta(minutes=2):
            refresh_token = payload.get("refresh_token")
            if not refresh_token:
                raise OAuthError("YouTube authorization expired. Disconnect and connect the account again.")
            refreshed = _request_json("https://oauth2.googleapis.com/token", method="POST", form={
                "client_id": _setting("GOOGLE_OAUTH_CLIENT_ID"),
                "client_secret": _setting("GOOGLE_OAUTH_CLIENT_SECRET"),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            payload.update(refreshed)
            encrypted = _fernet().encrypt(json.dumps(payload, separators=(",", ":")).encode())
            new_expiry = _expiry(refreshed)
            database.save_platform_connection(user_id, platform, connection["external_account_id"], encrypted, new_expiry)
    return connection, payload
