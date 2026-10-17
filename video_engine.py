"""FFmpeg-based vertical short-video renderer."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ASSETS_DIR = PROJECT_ROOT / "data" / "assets"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

WIDTH = 1080
HEIGHT = 1920
FPS = 30
VIDEO_CODEC = "libx264"
AUDIO_CODEC = "aac"
CRF = 20
PRESET = "medium"
AUDIO_BITRATE = "192k"
PIXEL_FORMAT = "yuv420p"
DURATION_TOLERANCE_SECONDS = 0.5
FFMPEG_TIMEOUT_SECONDS = 300


class VideoEngineError(Exception):
    """Raised when video rendering fails."""


class FFmpegError(VideoEngineError):
    """Raised when FFmpeg is missing or a render command fails."""


class VideoValidationError(VideoEngineError):
    """Raised when rendered output does not meet Shorts specifications."""


@dataclass(frozen=True)
class VideoRenderConfig:
    width: int = WIDTH
    height: int = HEIGHT
    fps: int = FPS
    video_codec: str = VIDEO_CODEC
    audio_codec: str = AUDIO_CODEC
    crf: int = CRF
    preset: str = PRESET
    audio_bitrate: str = AUDIO_BITRATE
    pixel_format: str = PIXEL_FORMAT
    duration_tolerance: float = DURATION_TOLERANCE_SECONDS


def render_video(
    background_videos: str | Path | list[str | Path],
    audio_path: str | Path,
    subtitle_path: str | Path,
    output_path: str | Path,
    config: VideoRenderConfig | None = None,
) -> str:
    """Render a 1080x1920 MP4 from local b-roll, voiceover, and burned-in ASS captions."""
    settings = config or VideoRenderConfig()
    ffmpeg = _require_ffmpeg()
    backgrounds = _validate_backgrounds(background_videos)
    audio = _validate_readable_file(audio_path, "audio_path")
    subtitles = _validate_subtitle_file(subtitle_path)
    output = _prepare_output_path(output_path)
    audio_duration = _probe_duration(audio)
    if audio_duration <= 0:
        raise VideoEngineError(f"voiceover duration must be greater than 0: {audio}")

    clip_plan = _plan_background_clips(backgrounds, audio_duration)
    filter_complex = _build_filter_complex(clip_plan, subtitles, settings, audio_duration)

    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clip_plan:
        command.extend(["-i", str(clip)])
    command.extend(
        [
            "-i",
            str(audio),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            f"{len(clip_plan)}:a:0",
            "-t",
            f"{audio_duration:.3f}",
            "-r",
            str(settings.fps),
            "-c:v",
            settings.video_codec,
            "-preset",
            settings.preset,
            "-crf",
            str(settings.crf),
            "-pix_fmt",
            settings.pixel_format,
            "-c:a",
            settings.audio_codec,
            "-b:a",
            settings.audio_bitrate,
            "-ac",
            "2",
            "-ar",
            "44100",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )

    _run_ffmpeg(command)
    validate_rendered_video(output, audio_duration, settings)
    return str(output)


def validate_rendered_video(
    output_path: str | Path,
    expected_duration: float,
    config: VideoRenderConfig | None = None,
) -> dict[str, object]:
    """Validate container, streams, resolution, fps, and duration of a rendered MP4."""
    settings = config or VideoRenderConfig()
    path = Path(output_path)
    if not path.is_file():
        raise VideoValidationError(f"rendered file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise VideoValidationError(f"rendered file is empty: {path}")

    probe = _probe_media(path)
    format_info = probe.get("format") or {}
    format_name = str(format_info.get("format_name") or "")
    if "mp4" not in format_name.lower():
        raise VideoValidationError(f"output is not a valid MP4 container: {path}")

    streams = probe.get("streams") or []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise VideoValidationError(f"output is missing a video stream: {path}")
    if audio_stream is None:
        raise VideoValidationError(f"output is missing an audio stream: {path}")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width != settings.width or height != settings.height:
        raise VideoValidationError(
            f"output resolution is {width}x{height}, expected {settings.width}x{settings.height}"
        )

    fps = _parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    if abs(fps - settings.fps) > 1.0:
        raise VideoValidationError(f"output fps is {fps:.3f}, expected about {settings.fps}")

    duration = _duration_from_probe(probe)
    if duration <= 0:
        raise VideoValidationError("output duration must be greater than 0")
    if abs(duration - expected_duration) > settings.duration_tolerance:
        raise VideoValidationError(
            f"output duration {duration:.3f}s is not close to voiceover {expected_duration:.3f}s"
        )

    return {
        "path": str(path),
        "size": size,
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "has_audio": True,
        "has_video": True,
    }


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FFmpegError(
            "FFmpeg was not found on PATH. Install FFmpeg and ensure the ffmpeg executable is available."
        )
    if not shutil.which("ffprobe"):
        raise FFmpegError(
            "ffprobe was not found on PATH. Install a full FFmpeg build that includes ffprobe."
        )
    return ffmpeg


def _validate_backgrounds(background_videos: str | Path | list[str | Path]) -> list[Path]:
    if background_videos is None:
        raise VideoEngineError("background_videos is required")
    values: list[str | Path]
    if isinstance(background_videos, (str, Path)):
        values = [background_videos]
    else:
        values = list(background_videos)
    if not values:
        raise VideoEngineError("at least one background video is required")

    paths: list[Path] = []
    for item in values:
        path = _validate_readable_file(item, "background video")
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise VideoEngineError(f"unsupported background video type: {path}")
        probe = _probe_media(path)
        video_stream = next(
            (stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            raise VideoEngineError(f"background file has no video stream: {path}")
        if _probe_duration(path) <= 0:
            raise VideoEngineError(f"background video duration must be greater than 0: {path}")
        paths.append(path)
    return paths


def _validate_subtitle_file(subtitle_path: str | Path) -> Path:
    path = _validate_readable_file(subtitle_path, "subtitle_path")
    if path.suffix.lower() != ".ass":
        raise VideoEngineError("subtitle_path must be an .ass file for burned-in captions")
    return path


def _validate_readable_file(value: str | Path, label: str) -> Path:
    if value is None or not str(value).strip():
        raise VideoEngineError(f"{label} must be a non-empty path")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise VideoEngineError(f"{label} does not exist: {path}")
    try:
        size = path.stat().st_size
        handle = path.open("rb")
        handle.close()
    except OSError as exc:
        raise VideoEngineError(f"{label} is not readable: {path}") from exc
    if size <= 0:
        raise VideoEngineError(f"{label} is empty: {path}")
    return path


def _prepare_output_path(output_path: str | Path) -> Path:
    if output_path is None or not str(output_path).strip():
        raise VideoEngineError("output_path must be a non-empty path")
    path = Path(output_path).expanduser()
    if path.suffix.lower() != ".mp4":
        raise VideoEngineError("output_path must end with .mp4")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VideoEngineError(f"could not create output directory {path.parent}: {exc}") from exc
    return path


def _plan_background_clips(backgrounds: list[Path], target_duration: float) -> list[Path]:
    durations = [_probe_duration(path) for path in backgrounds]
    planned: list[Path] = []
    total = 0.0
    guard = 0
    while total < target_duration and guard < 1000:
        for path, duration in zip(backgrounds, durations):
            planned.append(path)
            total += duration
            if total >= target_duration:
                break
        guard += 1
    if not planned:
        raise VideoEngineError("could not build a background clip plan")
    return planned


def _build_filter_complex(
    clips: list[Path],
    subtitle_path: Path,
    config: VideoRenderConfig,
    duration: float,
) -> str:
    parts: list[str] = []
    labels: list[str] = []
    for index in range(len(clips)):
        label = f"v{index}"
        parts.append(
            f"[{index}:v:0]scale={config.width}:{config.height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={config.width}:{config.height},setsar=1,fps={config.fps},format={config.pixel_format}[{label}]"
        )
        labels.append(f"[{label}]")

    concat_input = "".join(labels)
    escaped_subs = _escape_filter_path(subtitle_path)
    parts.append(f"{concat_input}concat=n={len(clips)}:v=1:a=0[vcat]")
    parts.append(
        f"[vcat]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
        f"subtitles='{escaped_subs}'[vout]"
    )
    return ";".join(parts)


def _escape_filter_path(path: Path) -> str:
    text = path.resolve().as_posix()
    return (
        text.replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def _probe_media(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise FFmpegError("ffprobe was not found on PATH")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FFmpegError(f"ffprobe failed for {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(f"ffprobe failed for {path}: {detail or 'unknown error'}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise FFmpegError(f"ffprobe returned an unexpected payload for {path}")
    return payload


def _probe_duration(path: Path) -> float:
    return _duration_from_probe(_probe_media(path))


def _duration_from_probe(probe: dict) -> float:
    format_info = probe.get("format") or {}
    try:
        duration = float(format_info.get("duration") or 0.0)
        if duration > 0:
            return duration
    except (TypeError, ValueError):
        pass
    for stream in probe.get("streams") or []:
        try:
            duration = float(stream.get("duration") or 0.0)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return 0.0


def _parse_frame_rate(value: object) -> float:
    text = str(value or "")
    if not text or text == "0/0":
        return 0.0
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            denom = float(denominator)
            if denom == 0:
                return 0.0
            return float(numerator) / denom
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _run_ffmpeg(command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"FFmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise FFmpegError(f"failed to start FFmpeg: {exc}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise FFmpegError(f"FFmpeg render failed: {detail or f'exit code {completed.returncode}'}")


def _discover_local_backgrounds(root: Path = DEFAULT_ASSETS_DIR) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in VIDEO_EXTENSIONS and path.stat().st_size > 0:
            found.append(path.resolve())
    return found


if __name__ == "__main__":
    SAMPLE_AUDIO = PROJECT_ROOT / "output" / "test_voiceover.mp3"
    SAMPLE_SUBS = PROJECT_ROOT / "output" / "test_subtitles.ass"
    SAMPLE_OUTPUT = PROJECT_ROOT / "output" / "test_video.mp4"

    try:
        ffmpeg_path = _require_ffmpeg()
        print(f"FFmpeg: {ffmpeg_path}")
    except FFmpegError as exc:
        print(f"Video render test cannot run: {exc}", file=sys.stderr)
        sys.exit(1)

    backgrounds = _discover_local_backgrounds()
    if not backgrounds:
        print("Video render test cannot run because no local asset exists.")
        print(f"Place a royalty-free background video under: {DEFAULT_ASSETS_DIR}")
        print("This module does not download stock footage.")
        sys.exit(1)

    try:
        output = render_video(
            backgrounds,
            SAMPLE_AUDIO,
            SAMPLE_SUBS,
            SAMPLE_OUTPUT,
        )
        info = validate_rendered_video(output, _probe_duration(SAMPLE_AUDIO))
        print("Video render successful")
        print(f"output: {info['path']}")
        print(f"resolution: {info['width']}x{info['height']}")
        print(f"duration: {info['duration']:.3f}s")
        print(f"fps: {info['fps']:.3f}")
        print(f"file size: {info['size']} bytes")
    except VideoEngineError as exc:
        print(f"Video render failed: {exc}", file=sys.stderr)
        sys.exit(1)
