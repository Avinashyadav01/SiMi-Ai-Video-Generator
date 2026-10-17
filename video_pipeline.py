"""Orchestrates local short-video generation and persists each completed stage."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import asset_engine
import database
import gemini_engine
import subtitle_engine
import tts_engine
import video_engine
from quality_validator import QualityValidationError, validate_video

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", PROJECT_ROOT / "output"))
logger = logging.getLogger("video_pipeline")


class PipelineError(Exception):
    """Raised when a pipeline stage fails."""


def run_pipeline(topic: str, *, job_id: str, user_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate and validate one user-owned queued video job."""
    job = database.get_job(job_id, user_id)
    if not job or job["topic"] != topic:
        raise PipelineError("Queued job was not found for this user")
    jid = job["id"]
    work_dir = OUTPUT_DIR / "users" / user_id / jid
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        metadata = _metadata(job)
        content = {"title": job.get("title"), "voiceover_text": job.get("voiceover_text"), "caption": job.get("caption"), **metadata.get("content", {})}
        if not all(content.get(key) for key in ("title", "voiceover_text", "caption")) or not content.get("bg_video_keywords"):
            database.update_job(jid, user_id, status="CREATED", stage="Writing your script", progress=4, error=None)
            content = gemini_engine.generate_video_content(job["topic"], preferences=settings or {})
            _save(job, user_id, metadata, content=content, title=content["title"], voiceover_text=content["voiceover_text"], caption=content["caption"], status="SCRIPT_GENERATED", stage="Writing your script", progress=12, error=None)
            logger.info("job=%s stage=SCRIPT_GENERATED", jid)

        audio = Path(metadata.get("audio_path", work_dir / "voiceover.mp3"))
        if not audio.is_file() or audio.stat().st_size == 0:
            database.update_job(jid, user_id, stage="Creating your voiceover", progress=18)
            tts_engine.generate_voiceover_sync(content["voiceover_text"], str(audio), voice=(settings or {}).get("voice"))
        if not audio.is_file() or audio.stat().st_size == 0:
            raise PipelineError("TTS returned without producing a readable audio file")
        _save(job, user_id, metadata, audio_path=str(audio.resolve()), status="AUDIO_GENERATED", stage="Creating voiceover", progress=32, error=None)
        logger.info("job=%s stage=AUDIO_GENERATED", jid)

        assets = metadata.get("assets")
        if not assets or not all(Path(item).is_file() and Path(item).stat().st_size for item in assets):
            database.update_job(jid, user_id, stage="Finding suitable visuals", progress=36)
            assets = asset_engine.find_background_assets(content["bg_video_keywords"])
        if not assets:
            raise PipelineError(f"No valid local background video matches the topic. Add licensed videos under {asset_engine.DEFAULT_ASSETS_DIR} (category folders such as productivity/ or fallback/).")
        _save(job, user_id, metadata, assets=assets, status="ASSETS_READY", stage="Finding visuals", progress=48, error=None)
        logger.info("job=%s stage=ASSETS_READY asset_count=%d", jid, len(assets))

        subtitles = metadata.get("subtitles") or {}
        ass_path = Path(subtitles.get("ass_path", work_dir / "subtitles.ass"))
        srt_path = Path(subtitles.get("srt_path", work_dir / "subtitles.srt"))
        if not ass_path.is_file() or ass_path.stat().st_size == 0:
            database.update_job(jid, user_id, stage="Creating your subtitles", progress=54)
            subtitles = subtitle_engine.generate_subtitles(content["voiceover_text"], audio, ass_path, srt_path)
        _save(job, user_id, metadata, subtitles={"ass_path": subtitles["ass_path"], "srt_path": subtitles["srt_path"]}, status="SUBTITLES_GENERATED", stage="Adding subtitles", progress=62, error=None)
        logger.info("job=%s stage=SUBTITLES_GENERATED", jid)

        video = Path(job.get("video_path") or (work_dir / "short.mp4"))
        expected_duration = subtitle_engine.get_audio_duration(audio)
        quality = None
        if video.is_file() and video.stat().st_size > 0:
            try:
                quality = validate_video(video, expected_audio_duration=expected_duration)
            except QualityValidationError:
                # A crash during FFmpeg can leave a non-empty partial MP4. Do
                # not reuse it on a restart or retry.
                video.unlink(missing_ok=True)
        if quality is None:
            database.update_job(jid, user_id, stage="Rendering your vertical video", progress=70)
            video_engine.render_video(assets, audio, ass_path, video)
        _save(job, user_id, metadata, video_path=str(video.resolve()), status="VIDEO_RENDERED", stage="Rendering video", progress=84, error=None)
        logger.info("job=%s stage=VIDEO_RENDERED", jid)

        database.update_job(jid, user_id, stage="Checking video and audio quality", progress=92)
        if quality is None:
            quality = validate_video(video, expected_audio_duration=expected_duration)
        _save(job, user_id, metadata, quality=quality, status="VALIDATED", stage="Ready to preview", progress=100, completed_at=database._now(), error=None)
        database.complete_idea_for_job(user_id, jid, "READY")
        logger.info("job=%s stage=VALIDATED", jid)
        return database.get_job(jid, user_id) or {}
    except Exception as exc:
        try:
            current = database.get_job(jid, user_id) or job
            database.update_job(jid, user_id, status="FAILED", stage="Generation failed", error=f"{type(exc).__name__}: {exc}", retry_count=int(current.get("retry_count") or 0) + 1)
            database.complete_idea_for_job(user_id, jid, "FAILED")
        except Exception:
            logger.exception("Could not persist failure state for job=%s", jid)
        logger.exception("job=%s pipeline failed", jid)
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(f"Job {jid} failed: {exc}") from exc


def _metadata(job: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = job.get("metadata", job.get("metadata_json", "{}"))
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save(job: dict[str, Any], user_id: str, current: dict[str, Any], **changes: Any) -> None:
    data = dict(current)
    for key in ("content", "audio_path", "assets", "subtitles", "quality"):
        if key in changes:
            data[key] = changes.pop(key)
    current.clear()
    current.update(data)
    database.update_job(job["id"], user_id, metadata_json=data, **changes)
