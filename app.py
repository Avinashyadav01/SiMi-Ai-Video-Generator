"""FastAPI backend for the multi-user SiMi local-first application."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import auth
import asset_engine
import database
import oauth_service
import media_service
import publishing_service
from scheduler import run_scheduler
from video_pipeline import run_pipeline

WEB = ROOT / "web"
OUTPUT = Path(os.getenv("OUTPUT_DIR", ROOT / "output")).resolve()
APP_ENV = os.getenv("APP_ENV", "development").lower()
SECURE_COOKIE = os.getenv("COOKIE_SECURE", "true" if APP_ENV == "production" else "false").lower() in {"1", "true", "yes"}
APP_ORIGIN = os.getenv("APP_ORIGIN", "").strip().rstrip("/")
if APP_ENV not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be either development or production")
if APP_ENV == "production" and not SECURE_COOKIE:
    raise RuntimeError("COOKIE_SECURE must be enabled in production")
if APP_ENV == "production":
    parsed_origin = urllib.parse.urlsplit(APP_ORIGIN)
    try:
        parsed_origin.port
    except ValueError as exc:
        raise RuntimeError("APP_ORIGIN contains an invalid port") from exc
    if parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.username or parsed_origin.password or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
        raise RuntimeError("APP_ORIGIN must be the public HTTPS origin, for example https://studio.example.com")
    if not parsed_origin.hostname:
        raise RuntimeError("APP_ORIGIN must include a valid hostname")
logger = logging.getLogger("simi")
_AUTH_RATE_WINDOW = 15 * 60
_AUTH_RATE_LIMITS = {"login": 10, "signup": 5}
_auth_attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_auth_attempts_lock = threading.Lock()


def _enforce_auth_rate_limit(request: Request, action: str) -> None:
    address = request.client.host if request.client else "unknown"
    key = (address, action)
    now = time.monotonic()
    with _auth_attempts_lock:
        attempts = _auth_attempts[key]
        while attempts and now - attempts[0] >= _AUTH_RATE_WINDOW:
            attempts.popleft()
        if len(attempts) >= _AUTH_RATE_LIMITS[action]:
            retry_after = max(1, int(_AUTH_RATE_WINDOW - (now - attempts[0])))
            raise HTTPException(
                status_code=429,
                detail="Too many account attempts. Please wait before trying again.",
                headers={"Retry-After": str(retry_after)},
            )
        attempts.append(now)
        if len(_auth_attempts) > 10000:
            expired_keys = [k for k, values in _auth_attempts.items() if not values or now - values[-1] >= _AUTH_RATE_WINDOW]
            for expired_key in expired_keys:
                _auth_attempts.pop(expired_key, None)


def _clear_auth_rate_limit(request: Request, action: str) -> None:
    address = request.client.host if request.client else "unknown"
    with _auth_attempts_lock:
        _auth_attempts.pop((address, action), None)


class SignupBody(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=12, max_length=256)


class TopicBody(BaseModel):
    topic: str = Field(min_length=2, max_length=500, strip_whitespace=True)
    platforms: list[str] = Field(default_factory=list, max_length=3)


class TopicsBody(BaseModel):
    topics: list[str] = Field(min_length=1, max_length=50)

    @field_validator("topics")
    @classmethod
    def validate_topics(cls, topics: list[str]) -> list[str]:
        cleaned = [topic.strip() for topic in topics]
        if any(not 2 <= len(topic) <= 500 for topic in cleaned):
            raise ValueError("Each topic must contain between 2 and 500 characters")
        return cleaned


class ScheduleBody(BaseModel):
    enabled: bool
    posts_per_day: int = Field(ge=1, le=12)
    times: list[str] = Field(min_length=1, max_length=12)
    timezone: str = Field(min_length=1, max_length=80)
    platforms: list[str] = Field(default_factory=list, max_length=3)


class SettingsBody(BaseModel):
    language: Literal["English", "Hindi", "Hinglish"] | None = None
    voice: Literal["en-IN-NeerjaNeural", "en-IN-PrabhatNeural", "en-US-JennyNeural", "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"] | None = None
    tone: Literal["Friendly", "Confident", "Educational", "Energetic", "Storytelling"] | None = None
    audience: Literal["General", "Beginners", "Students", "Creators", "Professionals"] | None = None
    style: Literal["Auto Style", "Cinematic", "Minimal", "Bold captions"] | None = None
    duration: Literal["30-60 sec", "15-30 sec", "60-90 sec"] | None = None


class PublishBody(BaseModel):
    platform: Literal["youtube", "instagram", "facebook"]


async def worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            job = await asyncio.to_thread(database.claim_next_job)
            if job:
                user = await asyncio.to_thread(database.get_user, job["user_id"])
                try:
                    if not user:
                        raise RuntimeError("Job owner is no longer available")
                    preferences=(job.get("metadata") or {}).get("preferences",user["settings"])
                    await asyncio.to_thread(run_pipeline, job["topic"], job_id=job["id"], user_id=job["user_id"], settings=preferences)
                except Exception:
                    logger.exception("generation failed job_id=%s", job["id"])
                continue
        except Exception:
            logger.exception("generation worker iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.initialize_database()
    recovered = await asyncio.to_thread(database.recover_interrupted_jobs)
    if recovered: logger.info("re-queued %d interrupted job(s)", recovered)
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_loop(stop), name="generation-worker")
    scheduler = asyncio.create_task(run_scheduler(stop), name="user-scheduler")
    app.state.stop_event = stop
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(worker, scheduler, return_exceptions=True)


app = FastAPI(title="SiMi AI Content Automation", version="0.2.0", lifespan=lifespan, docs_url=None if APP_ENV == "production" else "/docs", redoc_url=None if APP_ENV == "production" else "/redoc", openapi_url=None if APP_ENV == "production" else "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response=await call_next(request)
        response.headers.setdefault("X-Content-Type-Options","nosniff")
        response.headers.setdefault("X-Frame-Options","DENY")
        response.headers.setdefault("Referrer-Policy","strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy","camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return response


app.add_middleware(SecurityHeadersMiddleware)


class SameOriginMutationMiddleware(BaseHTTPMiddleware):
    """Require browser mutations to originate from the configured public site."""

    async def dispatch(self, request, call_next):
        if APP_ENV == "production" and request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "").rstrip("/")
            if origin != APP_ORIGIN:
                return Response("Request origin is not allowed", status_code=403)
        return await call_next(request)


app.add_middleware(SameOriginMutationMiddleware)

if APP_ENV == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urllib.parse.urlsplit(APP_ORIGIN).hostname])


def current_user(request: Request) -> dict[str, Any]:
    user = auth.resolve_session(request.cookies.get(auth.COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in to continue")
    return user


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    allowed = ("id", "topic", "title", "caption", "status", "stage", "progress", "created_at", "updated_at", "scheduled_at", "youtube_status", "instagram_status", "facebook_status", "error", "retry_count", "platforms")
    result={key: job.get(key) for key in allowed}
    if job.get("status")=="VALIDATED" and job.get("scheduled_at"):
        try:
            scheduled=datetime.fromisoformat(job["scheduled_at"])
            if scheduled.tzinfo is None:scheduled=scheduled.replace(tzinfo=timezone.utc)
            if scheduled>datetime.now(timezone.utc):result["status"]="SCHEDULED"
        except (TypeError,ValueError):
            pass
    result["video_ready"]=bool(job.get("video_path") and job.get("status") in {"VALIDATED","SCHEDULED","PUBLISHING","PUBLISHED"})
    if result["video_ready"]:result["video_url"]=f"/api/jobs/{job['id']}/video"
    return result


def _readiness() -> dict[str, Any]:
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    model = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
    ollama_ready = False
    if provider in {"ollama", "auto"}:
        base = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        try:
            request = urllib.request.Request(f"{base}/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=1.5) as response:
                models = json.loads(response.read().decode("utf-8")).get("models", [])
            ollama_ready = any(item.get("name") == model for item in models)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, AttributeError):
            ollama_ready = False
    gemini_ready = bool(os.getenv("GEMINI_API_KEY", "").strip())
    ai_ready = ollama_ready if provider == "ollama" else gemini_ready if provider == "gemini" else (ollama_ready or gemini_ready)
    ffmpeg_ready = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    assets_count = asset_engine.count_local_assets()
    return {
        "database_ready": database.health_check(),
        "ai_provider": provider,
        "ai_ready": ai_ready,
        "model": model if provider in {"ollama", "auto"} else os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "ffmpeg_ready": ffmpeg_ready,
        "local_assets_count": assets_count,
        "generation_ready": ai_ready and ffmpeg_ready and assets_count > 0,
        "publishing_available": os.getenv("PUBLISHING_ENABLED", "false").strip().lower() in {"1", "true", "yes"},
        "oauth_configured": {name: oauth_service.configured(name) for name in ("youtube", "instagram", "facebook")},
    }


@app.get("/api/health")
def health():
    database_ready = database.health_check()
    return {"status": "ok" if database_ready else "not_ready", "database_ready": database_ready}


@app.post("/api/auth/signup")
def signup(body: SignupBody, request: Request, response: Response):
    _enforce_auth_rate_limit(request, "signup")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body.email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    try:
        user = database.create_user(body.email, auth.hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except database.DatabaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = auth.create_login_session(user["id"])
    _clear_auth_rate_limit(request, "signup")
    _set_cookie(response, token)
    return {"user": user}


@app.post("/api/auth/login")
def login(body: SignupBody, request: Request, response: Response):
    _enforce_auth_rate_limit(request, "login")
    record = database.get_user_auth(body.email)
    if not record or not auth.verify_password(body.password, record["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    token = auth.create_login_session(record["id"])
    _clear_auth_rate_limit(request, "login")
    _set_cookie(response, token)
    return {"user": database.get_user(record["id"])}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.revoke_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"user": user}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    jobs = database.list_jobs(user["id"], 100)
    return {"user": user, "jobs": [_public_job(j) for j in jobs], "schedule": database.get_schedule(user["id"]), "platforms": database.list_platforms(user["id"]), "ideas": database.list_topic_ideas(user["id"]), "readiness": _readiness()}


@app.get("/api/jobs")
def jobs(user=Depends(current_user)):
    return {"jobs": [_public_job(j) for j in database.list_jobs(user["id"])]}


@app.post("/api/jobs")
def create_job(body: TopicBody, user=Depends(current_user)):
    allowed = {"youtube", "instagram", "facebook"}
    if not set(body.platforms) <= allowed:
        raise HTTPException(status_code=422, detail="Unsupported platform")
    try:
        job = database.enqueue_job(user["id"], body.topic, body.platforms)
    except database.DatabaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _public_job(job)


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str, user=Depends(current_user)):
    try:
        return _public_job(database.retry_job(user["id"], job_id))
    except database.DatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/publish")
def publish_job(job_id: str, body: PublishBody, user=Depends(current_user)):
    try:
        result=publishing_service.publish_job_platform(user["id"],job_id,body.platform)
        return _public_job(result)
    except publishing_service.PublishingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except database.DatabaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, user=Depends(current_user)):
    job = database.get_job(job_id, user["id"])
    if not job: raise HTTPException(status_code=404, detail="Video not found")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/video")
def video_file(job_id: str, user=Depends(current_user)):
    job = database.get_job(job_id, user["id"])
    if not job or job["status"] not in {"VALIDATED", "SCHEDULED", "PUBLISHED", "PUBLISHING"} or not job.get("video_path"):
        raise HTTPException(status_code=404, detail="Validated video is not ready")
    path = Path(job["video_path"]).resolve()
    root = (OUTPUT / "users" / user["id"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Video file was not found")
    return FileResponse(path, media_type="video/mp4", filename=f"{job_id}.mp4", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@app.get("/public/users/{user_id}/videos/{job_id}")
def public_signed_video(user_id: str, job_id: str, expires: int, signature: str):
    if not media_service.verify_signature(user_id, job_id, expires, signature):
        raise HTTPException(status_code=404, detail="Video link is invalid or expired")
    job=database.get_job(job_id,user_id)
    if not job or job["status"] not in {"VALIDATED","SCHEDULED","PUBLISHED","PUBLISHING"} or not job.get("video_path"):
        raise HTTPException(status_code=404, detail="Validated video is not ready")
    path=Path(job["video_path"]).resolve()
    root=(OUTPUT/"users"/user_id).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Video file was not found")
    return FileResponse(path,media_type="video/mp4",headers={"X-Content-Type-Options":"nosniff","Cache-Control":"private, no-store"})


@app.post("/api/ideas")
def add_ideas(body: TopicsBody, user=Depends(current_user)):
    try: return {"ideas": database.add_topic_ideas(user["id"], body.topics)}
    except database.DatabaseError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/ideas")
def ideas(user=Depends(current_user)):
    return {"ideas": database.list_topic_ideas(user["id"])}


@app.get("/api/schedule")
def schedule(user=Depends(current_user)):
    return database.get_schedule(user["id"])


@app.put("/api/schedule")
def update_schedule(body: ScheduleBody, user=Depends(current_user)):
    try: ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc: raise HTTPException(status_code=422, detail="Choose a valid timezone") from exc
    try: return database.save_schedule(user["id"], enabled=body.enabled, posts_per_day=body.posts_per_day, times=body.times, timezone=body.timezone, platforms=body.platforms)
    except database.DatabaseError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/schedule")
def remove_schedule(user=Depends(current_user)):
    database.delete_schedule(user["id"])
    return {"ok": True, "schedule": database.get_schedule(user["id"])}


@app.get("/api/platforms")
def platforms(user=Depends(current_user)):
    configured = {name: oauth_service.configured(name) for name in ("youtube", "instagram", "facebook")}
    enabled=os.getenv("PUBLISHING_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    return {"platforms": database.list_platforms(user["id"]), "oauth_configured": configured, "publishing_available": enabled}


@app.get("/api/oauth/{platform}/start")
def oauth_start(platform: str, user=Depends(current_user)):
    try:
        return RedirectResponse(oauth_service.authorization_url(platform, user["id"]), status_code=303)
    except oauth_service.OAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _oauth_callback(platform: str, code: str | None, state: str | None, error: str | None) -> RedirectResponse:
    if error:
        return RedirectResponse("/?oauth=denied", status_code=303)
    try:
        oauth_service.complete(platform, code or "", state or "")
        return RedirectResponse("/?oauth=connected", status_code=303)
    except oauth_service.OAuthError:
        logger.warning("OAuth connection failed platform=%s", platform)
        return RedirectResponse("/?oauth=failed", status_code=303)


@app.get("/api/oauth/youtube/callback")
def youtube_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    return _oauth_callback("youtube", code, state, error)


@app.get("/api/oauth/instagram/callback")
def instagram_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    return _oauth_callback("instagram", code, state, error)


@app.get("/api/oauth/facebook/callback")
def facebook_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    return _oauth_callback("facebook", code, state, error)


@app.delete("/api/platforms/{platform}")
def disconnect_platform(platform: str, user=Depends(current_user)):
    if platform not in oauth_service.PLATFORMS:
        raise HTTPException(status_code=404, detail="Platform not found")
    database.disconnect_platform(user["id"], platform)
    return {"ok": True, "platform": platform, "status": "DISCONNECTED"}


@app.put("/api/settings")
def settings(body: SettingsBody, user=Depends(current_user)):
    values = body.model_dump(exclude_none=True)
    try: return {"settings": database.update_settings(user["id"], values)}
    except database.DatabaseError as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_LIFETIME_DAYS*86400, httponly=True, secure=SECURE_COOKIE, samesite="strict", path="/")


if WEB.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB), name="assets")


@app.get("/")
def home():
    return FileResponse(WEB / "index.html")
