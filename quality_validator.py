"""Strict media validation for videos before they can be published."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class QualityValidationError(Exception):
    """Raised when a rendered file does not meet delivery requirements."""


def validate_video(path: str | Path, *, expected_audio_duration: float | None = None) -> dict[str, object]:
    video = Path(path).expanduser().resolve()
    if not video.is_file():
        raise QualityValidationError(f"Video file does not exist: {video}")
    size = video.stat().st_size
    if size <= 0:
        raise QualityValidationError(f"Video file is empty: {video}")
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise QualityValidationError("FFmpeg and ffprobe must both be installed and available on PATH")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualityValidationError(f"Could not inspect video: {exc}") from exc
    if result.returncode:
        raise QualityValidationError(f"Invalid or corrupted media: {(result.stderr or '').strip()[-1200:]}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise QualityValidationError("ffprobe returned invalid media metadata") from exc
    fmt = info.get("format") or {}
    if "mp4" not in str(fmt.get("format_name", "")).lower():
        raise QualityValidationError("Output container is not MP4")
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v or not a:
        raise QualityValidationError("Output must contain both video and audio streams")
    if v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise QualityValidationError("Output codecs must be H.264 video and AAC audio")
    if (v.get("width"), v.get("height")) != (1080, 1920):
        raise QualityValidationError(f"Output resolution must be 1080x1920; got {v.get('width')}x{v.get('height')}")
    fps = _rate(v.get("avg_frame_rate") or v.get("r_frame_rate"))
    if abs(fps - 30) > 1:
        raise QualityValidationError(f"Frame rate must be approximately 30 FPS; got {fps:.3f}")
    duration = _num(fmt.get("duration"))
    audio_duration = _num(a.get("duration")) or duration
    video_duration = _num(v.get("duration")) or duration
    if min(duration, audio_duration, video_duration) <= 0:
        raise QualityValidationError("Video and audio durations must be greater than zero")
    if abs(audio_duration - video_duration) > 0.5:
        raise QualityValidationError(f"Audio and video durations differ by more than 0.5s ({audio_duration:.2f}s vs {video_duration:.2f}s)")
    if expected_audio_duration is not None and abs(duration - expected_audio_duration) > 0.75:
        raise QualityValidationError(f"Video duration {duration:.2f}s does not match voiceover {expected_audio_duration:.2f}s")
    # Decode every frame and audio packet to catch corruption that metadata probing misses.
    try:
        decoded = subprocess.run([ffmpeg, "-v", "error", "-i", str(video), "-f", "null", "NUL"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualityValidationError(f"Could not decode output for corruption check: {exc}") from exc
    if decoded.returncode:
        raise QualityValidationError(f"Video decode failed: {(decoded.stderr or '').strip()[-1200:]}")
    return {"path": str(video), "size": size, "duration": duration, "audio_duration": audio_duration, "width": 1080, "height": 1920, "fps": fps, "video_codec": "h264", "audio_codec": "aac", "valid": True}


def _num(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate(value: object) -> float:
    try:
        text = str(value)
        return float(text.split("/")[0]) / float(text.split("/")[1]) if "/" in text else float(text)
    except (ValueError, ZeroDivisionError, IndexError):
        return 0.0
