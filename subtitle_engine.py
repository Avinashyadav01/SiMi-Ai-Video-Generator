"""Deterministic ASS/SRT subtitle generation for vertical Shorts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Vertical Shorts/Reels canvas
PLAY_RES_X = 1080
PLAY_RES_Y = 1920

# Caption style (ASS). Colours are &HAABBGGRR.
FONT_NAME = "Nirmala UI"
FONT_SIZE = 68
PRIMARY_COLOUR = "&H00FFFFFF"
SECONDARY_COLOUR = "&H00FFFFFF"
OUTLINE_COLOUR = "&H00000000"
BACK_COLOUR = "&H64000000"
BOLD = -1
ITALIC = 0
BORDER_STYLE = 1
OUTLINE = 3.0
SHADOW = 1.0
ALIGNMENT = 2  # bottom-center
MARGIN_L = 90
MARGIN_R = 90
MARGIN_V = 240

MAX_WORDS_PER_CUE = 6
MAX_CHARS_PER_CUE = 42
LEAD_IN_SECONDS = 0.08
TAIL_OUT_SECONDS = 0.08
MIN_CUE_SECONDS = 0.45
MIN_AUDIO_SECONDS = 0.25
CUE_GAP_SECONDS = 0.01

DEFAULT_ASS_PATH = "output/test_subtitles.ass"
DEFAULT_SRT_PATH = "output/test_subtitles.srt"

SENTENCE_SPLIT_RE = r"(?<=[.!?।؟])\s+"


class SubtitleEngineError(Exception):
    """Raised when subtitle generation fails."""


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str


def validate_inputs(text: str, audio_path: str | Path) -> tuple[str, Path]:
    """Validate voiceover text and the generated audio file."""
    if text is None or not str(text).strip():
        raise SubtitleEngineError("voiceover text must be a non-empty string")

    if audio_path is None or not str(audio_path).strip():
        raise SubtitleEngineError("audio_path must be a non-empty path")

    path = Path(audio_path).expanduser()
    if not path.is_file():
        raise SubtitleEngineError(f"audio file does not exist: {path}")
    try:
        size = path.stat().st_size
        readable = path.open("rb")
        readable.close()
    except OSError as exc:
        raise SubtitleEngineError(f"audio file is not readable: {path}") from exc
    if size <= 0:
        raise SubtitleEngineError(f"audio file is empty: {path}")

    return str(text).strip(), path.resolve()


def get_audio_duration(audio_path: str | Path) -> float:
    """Read the actual duration of a local audio file."""
    path = Path(audio_path)
    duration = _duration_ffprobe(path)
    if duration is None:
        duration = _duration_moviepy(path)
    if duration is None:
        raise SubtitleEngineError(f"could not determine audio duration for {path}")
    if duration < MIN_AUDIO_SECONDS:
        raise SubtitleEngineError(
            f"audio duration is too short ({duration:.3f}s) to generate subtitles"
        )
    return duration


def split_voiceover(text: str) -> list[str]:
    """Split voiceover into short, readable caption chunks without reordering words."""
    cleaned = " ".join(str(text).split())
    sentences = [part.strip() for part in re.split(SENTENCE_SPLIT_RE, cleaned) if part.strip()]
    if not sentences:
        sentences = [cleaned]

    chunks: list[str] = []
    for sentence in sentences:
        chunks.extend(_chunk_sentence(sentence))
    if not chunks:
        raise SubtitleEngineError("voiceover text produced no subtitle chunks")
    return chunks


def build_cues(chunks: list[str], duration: float) -> list[SubtitleCue]:
    """Assign deterministic, non-overlapping timings across the audio duration."""
    if not chunks:
        raise SubtitleEngineError("cannot build subtitles from an empty chunk list")
    if duration < MIN_AUDIO_SECONDS:
        raise SubtitleEngineError("audio duration is too short")

    lead = min(LEAD_IN_SECONDS, duration * 0.05)
    tail = min(TAIL_OUT_SECONDS, duration * 0.05)
    usable = max(duration - lead - tail, duration * 0.9)
    start_at = max((duration - usable) / 2.0, 0.0)

    weights = [max(len(chunk), 1) for chunk in chunks]
    total_weight = float(sum(weights))
    raw = [usable * (weight / total_weight) for weight in weights]

    scale = 1.0
    if min(raw) < MIN_CUE_SECONDS and sum(raw) > 0:
        needed = MIN_CUE_SECONDS * len(raw)
        if needed <= usable:
            extra = usable - needed
            long_indexes = [i for i, value in enumerate(raw) if value > MIN_CUE_SECONDS]
            long_total = sum(max(raw[i] - MIN_CUE_SECONDS, 0.0) for i in long_indexes) or extra
            adjusted: list[float] = []
            for i, value in enumerate(raw):
                if value < MIN_CUE_SECONDS:
                    adjusted.append(MIN_CUE_SECONDS)
                else:
                    share = extra * (max(value - MIN_CUE_SECONDS, 0.0) / long_total) if long_indexes else extra / len(raw)
                    adjusted.append(MIN_CUE_SECONDS + share if i in long_indexes else MIN_CUE_SECONDS)
            raw = adjusted
        else:
            scale = usable / needed
            raw = [MIN_CUE_SECONDS * scale for _ in raw]

    cues: list[SubtitleCue] = []
    cursor = start_at
    for index, (chunk, length) in enumerate(zip(chunks, raw), start=1):
        start = round(cursor, 3)
        if index == len(chunks):
            end = round(duration - min(tail, duration - start - 0.05), 3)
        else:
            end = round(start + length, 3)
        end = min(end, round(duration, 3))
        if end <= start:
            end = round(min(start + 0.05, duration), 3)
        next_start = round(end + CUE_GAP_SECONDS, 3) if index < len(chunks) else end
        cues.append(SubtitleCue(index=index, start=start, end=end, text=chunk))
        cursor = next_start if index < len(chunks) else end

    if cues:
        last = cues[-1]
        if last.end > duration:
            cues[-1] = SubtitleCue(last.index, last.start, round(duration, 3), last.text)
        elif last.end < duration - tail:
            cues[-1] = SubtitleCue(
                last.index,
                last.start,
                round(max(last.end, duration - tail), 3),
                last.text,
            )

    validate_cues(cues, duration)
    return cues


def validate_cues(cues: list[SubtitleCue], duration: float) -> None:
    """Ensure timestamps are valid, ordered, and non-overlapping."""
    if not cues:
        raise SubtitleEngineError("no subtitle cues were generated")

    previous_end = -1.0
    for cue in cues:
        if not cue.text.strip():
            raise SubtitleEngineError(f"subtitle {cue.index} is empty")
        if cue.start < 0 or cue.end < 0:
            raise SubtitleEngineError(f"subtitle {cue.index} has a negative timestamp")
        if cue.end > duration + 0.02:
            raise SubtitleEngineError(
                f"subtitle {cue.index} ends after the audio ({cue.end:.3f}s > {duration:.3f}s)"
            )
        if cue.start >= cue.end:
            raise SubtitleEngineError(
                f"subtitle {cue.index} start must be before end ({cue.start:.3f} >= {cue.end:.3f})"
            )
        if cue.start < previous_end:
            raise SubtitleEngineError(
                f"subtitle {cue.index} overlaps the previous cue ({cue.start:.3f} < {previous_end:.3f})"
            )
        previous_end = cue.end


def generate_srt(cues: list[SubtitleCue], output_path: str | Path) -> str:
    """Write an SRT file and return its path."""
    path = _prepare_output_path(output_path, ".srt")
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{_format_srt_time(cue.start)} --> {_format_srt_time(cue.end)}\n{cue.text}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    _assert_nonempty_file(path)
    return str(path)


def generate_ass(cues: list[SubtitleCue], output_path: str | Path) -> str:
    """Write an ASS file styled for 1080x1920 Shorts and return its path."""
    path = _prepare_output_path(output_path, ".ass")
    events = [
        (
            "Dialogue: 0,"
            f"{_format_ass_time(cue.start)},{_format_ass_time(cue.end)},"
            f"Default,,0,0,0,,{_escape_ass(cue.text)}"
        )
        for cue in cues
    ]
    content = "\n".join(
        [
            "[Script Info]",
            "Title: AI Shorts Generator Captions",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            f"PlayResX: {PLAY_RES_X}",
            f"PlayResY: {PLAY_RES_Y}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            (
                f"Style: Default,{FONT_NAME},{FONT_SIZE},{PRIMARY_COLOUR},{SECONDARY_COLOUR},"
                f"{OUTLINE_COLOUR},{BACK_COLOUR},{BOLD},{ITALIC},0,0,100,100,0,0,"
                f"{BORDER_STYLE},{OUTLINE},{SHADOW},{ALIGNMENT},{MARGIN_L},{MARGIN_R},{MARGIN_V},1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    _assert_nonempty_file(path)
    return str(path)


def generate_subtitles(
    text: str,
    audio_path: str | Path,
    ass_path: str | Path = DEFAULT_ASS_PATH,
    srt_path: str | Path = DEFAULT_SRT_PATH,
) -> dict[str, object]:
    """Generate ASS and SRT captions timed to the voiceover audio."""
    clean_text, audio = validate_inputs(text, audio_path)
    duration = get_audio_duration(audio)
    chunks = split_voiceover(clean_text)
    cues = build_cues(chunks, duration)
    written_ass = generate_ass(cues, ass_path)
    written_srt = generate_srt(cues, srt_path)
    return {
        "audio_path": str(audio),
        "duration": duration,
        "segment_count": len(cues),
        "cues": cues,
        "ass_path": written_ass,
        "srt_path": written_srt,
        "valid": True,
    }


def _chunk_sentence(sentence: str) -> list[str]:
    words = sentence.split()
    if not words:
        return []

    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = current + [word]
        line = " ".join(candidate)
        too_long = current and (
            len(candidate) > MAX_WORDS_PER_CUE or len(line) > MAX_CHARS_PER_CUE
        )
        if too_long:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current = candidate
    if current:
        chunks.append(" ".join(current))
    return chunks


def _duration_ffprobe(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
        return duration if duration > 0 else None
    except (OSError, subprocess.SubprocessError, TimeoutError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _duration_moviepy(path: Path) -> float | None:
    try:
        from moviepy import AudioFileClip
    except ImportError:
        return None
    clip = None
    try:
        clip = AudioFileClip(str(path))
        duration = float(clip.duration or 0.0)
        return duration if duration > 0 else None
    except Exception:
        return None
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def _prepare_output_path(output_path: str | Path, suffix: str) -> Path:
    if output_path is None or not str(output_path).strip():
        raise SubtitleEngineError("output path must be a non-empty string")
    path = Path(output_path).expanduser()
    if path.suffix.lower() != suffix:
        raise SubtitleEngineError(f"output path must end with {suffix}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SubtitleEngineError(f"could not create output directory {path.parent}: {exc}") from exc
    return path


def _assert_nonempty_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise SubtitleEngineError(f"failed to write subtitle file: {path}")


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", r"\N")


def _format_ass_time(seconds: float) -> str:
    total_cs = max(int(round(seconds * 100)), 0)
    hours, rem = divmod(total_cs, 3600 * 100)
    minutes, rem = divmod(rem, 60 * 100)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _format_srt_time(seconds: float) -> str:
    total_ms = max(int(round(seconds * 1000)), 0)
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


if __name__ == "__main__":
    SAMPLE_AUDIO = "output/test_voiceover.mp3"
    SAMPLE_TEXT = (
        "Success is not about working harder every minute. "
        "It is about focusing on the things that actually matter."
    )

    try:
        result = generate_subtitles(SAMPLE_TEXT, SAMPLE_AUDIO)
        print(f"audio duration: {result['duration']:.3f}s")
        print(f"number of subtitle segments: {result['segment_count']}")
        print(f"ASS: {result['ass_path']}")
        print(f"SRT: {result['srt_path']}")
        print(f"validation result: {'passed' if result['valid'] else 'failed'}")
    except SubtitleEngineError as exc:
        print(f"Subtitle generation failed: {exc}", file=sys.stderr)
        sys.exit(1)
