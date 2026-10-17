"""Local Edge TTS voiceover engine."""

from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
from dotenv import load_dotenv

DEFAULT_TTS_VOICE = "en-IN-NeerjaNeural"
DEFAULT_TTS_RATE = "+0%"
DEFAULT_TTS_VOLUME = "+0%"


class TTSEngineError(Exception):
    """Raised when voiceover generation fails."""


def _load_tts_settings() -> tuple[str, str, str]:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    voice = os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE).strip() or DEFAULT_TTS_VOICE
    rate = os.getenv("TTS_RATE", DEFAULT_TTS_RATE).strip() or DEFAULT_TTS_RATE
    volume = os.getenv("TTS_VOLUME", DEFAULT_TTS_VOLUME).strip() or DEFAULT_TTS_VOLUME
    return voice, rate, volume


def _validate_text(text: str) -> str:
    if text is None or not str(text).strip():
        raise TTSEngineError("text must be a non-empty string")
    return str(text).strip()


def _validate_output_path(output_path: str) -> Path:
    if output_path is None or not str(output_path).strip():
        raise TTSEngineError("output_path must be a non-empty string")

    path = Path(output_path).expanduser()
    if not path.name:
        raise TTSEngineError("output_path must include a file name")
    if path.suffix.lower() != ".mp3":
        raise TTSEngineError("output_path must end with .mp3")
    if path.exists() and path.is_dir():
        raise TTSEngineError("output_path must be a file path, not a directory")
    return path


async def generate_voiceover(text: str, output_path: str, *, voice: str | None = None) -> str:
    """Generate an MP3 voiceover from text using Microsoft Edge TTS."""
    clean_text = _validate_text(text)
    path = _validate_output_path(output_path)
    configured_voice, rate, volume = _load_tts_settings()
    configured_voice = (voice or configured_voice).strip()

    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        communicate = edge_tts.Communicate(
            clean_text,
            configured_voice,
            rate=rate,
            volume=volume,
        )
        await communicate.save(str(path))
    except TTSEngineError:
        raise
    except Exception as exc:
        raise TTSEngineError(f"edge-tts failed to generate audio: {exc}") from exc

    if not path.is_file() or path.stat().st_size <= 0:
        raise TTSEngineError(f"TTS completed but the output file is missing or empty: {path}")

    return str(path)


def generate_voiceover_sync(text: str, output_path: str, *, voice: str | None = None) -> str:
    """Run generate_voiceover() from a synchronous CLI or test environment."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(generate_voiceover(text, output_path, voice=voice))

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, generate_voiceover(text, output_path, voice=voice)).result()


if __name__ == "__main__":
    SAMPLE_TEXT = (
        "Success is not about working harder every minute. "
        "It is about focusing on the things that actually matter."
    )
    SAMPLE_OUTPUT = "output/test_voiceover.mp3"

    try:
        result_path = generate_voiceover_sync(SAMPLE_TEXT, SAMPLE_OUTPUT)
        output = Path(result_path)
        if not output.is_file() or output.stat().st_size <= 0:
            print("TTS generation failed: output file is missing or empty", file=sys.stderr)
            sys.exit(1)
        print(f"TTS generation successful: {result_path}")
    except Exception as exc:
        print(f"TTS generation failed: {exc}", file=sys.stderr)
        sys.exit(1)
