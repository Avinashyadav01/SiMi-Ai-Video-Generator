"""Gemini content-generation engine for short-form video scripts."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
KEYWORD_MIN = 5
KEYWORD_MAX = 8
VOICEOVER_MIN_WORDS = 30
VOICEOVER_MAX_WORDS = 230

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)
_MARKDOWN_RE = re.compile(r"[*_`#\[\]]")
_LABEL_RE = re.compile(
    r"^\s*(hook|voiceover|title|caption|script|scene)\s*:",
    flags=re.IGNORECASE,
)


class GeminiEngineError(Exception):
    """Raised when Gemini content generation fails."""


class VideoContent(BaseModel):
    """Structured short-form video content produced by Gemini."""

    title: str = Field(
        ...,
        min_length=3,
        max_length=80,
        description="Short, catchy, curiosity-driven title for YouTube Shorts or Reels.",
    )
    voiceover_text: str = Field(
        ...,
        min_length=80,
        description=(
            "Spoken voiceover of about 30-60 seconds with a strong first-sentence hook. "
            "Natural spoken language only. No stage directions, labels, markdown, or emojis."
        ),
    )
    bg_video_keywords: list[str] = Field(
        ...,
        min_length=KEYWORD_MIN,
        max_length=KEYWORD_MAX,
        description=(
            "5 to 8 concise visual-search keywords describing background footage "
            "that matches the topic."
        ),
    )
    caption: str = Field(
        ...,
        min_length=10,
        max_length=400,
        description=(
            "Short social-media caption with topic-relevant hashtags only. "
            "Do not include unrelated hashtags."
        ),
    )

    @field_validator("title", "voiceover_text", "caption", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if _EMOJI_RE.search(value):
            raise ValueError("title must not contain emojis")
        return value

    @field_validator("voiceover_text")
    @classmethod
    def _validate_voiceover(cls, value: str) -> str:
        if _EMOJI_RE.search(value):
            raise ValueError("voiceover_text must not contain emojis")
        if _MARKDOWN_RE.search(value):
            raise ValueError("voiceover_text must not contain markdown")
        first_line = value.splitlines()[0]
        if _LABEL_RE.match(first_line):
            raise ValueError("voiceover_text must not include labels such as 'Hook:'")
        word_count = len(value.split())
        if word_count < VOICEOVER_MIN_WORDS or word_count > VOICEOVER_MAX_WORDS:
            raise ValueError(
                f"voiceover_text should be about 30-60 seconds "
                f"({VOICEOVER_MIN_WORDS}-{VOICEOVER_MAX_WORDS} words); got {word_count} words"
            )
        return value

    @field_validator("bg_video_keywords", mode="before")
    @classmethod
    def _normalize_keywords(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("each background keyword must be a string")
            keyword = " ".join(item.split())
            if not keyword:
                raise ValueError("background keywords must not be empty")
            if len(keyword) > 60:
                raise ValueError("background keywords must be concise")
            cleaned.append(keyword)
        return cleaned

    @field_validator("caption")
    @classmethod
    def _validate_caption(cls, value: str) -> str:
        hashtags = re.findall(r"#\w+", value)
        if not hashtags:
            raise ValueError("caption must include at least one relevant hashtag")
        return value


def _require_topic(topic: str) -> str:
    if topic is None or not str(topic).strip():
        raise GeminiEngineError("topic must be a non-empty string")
    return str(topic).strip()


def _load_settings() -> tuple[str, str]:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiEngineError(
            "Missing GEMINI_API_KEY. Copy .env.example to .env and set a valid Gemini API key."
        )
    model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    return api_key, model


def _build_prompt(topic: str, preferences: dict[str, Any] | None = None) -> str:
    preferences = preferences or {}
    duration = str(preferences.get("duration", "30-60 sec"))
    word_range = {"15-30 sec": (35, 75), "30-60 sec": (70, 170), "60-90 sec": (140, 230)}.get(duration, (70, 170))
    prompt = (
        "Create original short-form video content for YouTube Shorts, Instagram Reels, "
        "and Facebook Reels.\n\n"
        f"Topic: {topic}\n\n"
        f"Language: {preferences.get('language', 'English')}\n"
        f"Tone: {preferences.get('tone', 'Friendly')}\n"
        f"Target audience: {preferences.get('audience', 'General')}\n"
        f"Video style: {preferences.get('style', 'Auto Style')}\n\n"
        "Field requirements:\n"
        "- title: short, catchy, curiosity-driven, suitable for Shorts/Reels. No emojis.\n"
        f"- voiceover_text: natural spoken language for TTS, about {duration} ({word_range[0]}-{word_range[1]} words). The first sentence must be a "
        "strong hook. Do not include stage directions, labels like 'Hook:' or 'Voiceover:', "
        "markdown, or emojis.\n"
        f"- bg_video_keywords: {KEYWORD_MIN}-{KEYWORD_MAX} concise visual-search keywords "
        "for stock/background footage that matches this topic.\n"
        "- caption: a short social caption plus hashtags that are actually relevant to "
        "this topic. Do not invent unrelated hashtags.\n"
    )
    correction = preferences.get("_correction")
    if correction:
        prompt += (
            "\nCORRECTION REQUIRED: Your previous draft failed validation. "
            f"{correction} Regenerate the entire object and satisfy every field constraint exactly. "
            "Return only the JSON object.\n"
        )
    return prompt


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        if isinstance(parsed, VideoContent):
            return parsed.model_dump_json()
        if isinstance(parsed, BaseModel):
            return parsed.model_dump_json()
        try:
            return json.dumps(parsed)
        except TypeError as exc:
            raise GeminiEngineError("Gemini returned an unreadable structured response") from exc

    raise GeminiEngineError("Empty Gemini response: no text or parsed content was returned")


def _strip_code_fences(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)
    return stripped.strip()


def _validate_content(raw_text: str, duration: str = "30-60 sec") -> dict[str, Any]:
    try:
        payload = json.loads(_strip_code_fences(raw_text))
    except json.JSONDecodeError as exc:
        raise GeminiEngineError(
            f"Gemini returned invalid JSON: {exc.msg}"
        ) from exc

    try:
        content = VideoContent.model_validate(payload)
    except ValidationError as exc:
        raise GeminiEngineError(
            f"Gemini returned JSON that failed schema validation: {exc}"
        ) from exc

    word_range = {"15-30 sec": (35, 75), "30-60 sec": (70, 170), "60-90 sec": (140, 230)}.get(duration, (70, 170))
    count = len(content.voiceover_text.split())
    if not word_range[0] <= count <= word_range[1]:
        raise GeminiEngineError(f"voiceover_text should contain {word_range[0]}-{word_range[1]} words for {duration}; got {count}")
    return content.model_dump()


def generate_video_content(topic: str, *, preferences: dict[str, Any] | None = None) -> dict:
    """Generate title, voiceover, background keywords, and caption for a short video."""
    clean_topic = _require_topic(topic)
    load_dotenv(Path(__file__).resolve().parent / ".env")
    duration = str((preferences or {}).get("duration", "30-60 sec"))
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider not in {"ollama", "gemini", "auto"}:
        raise GeminiEngineError("AI_PROVIDER must be one of: gemini, ollama, auto")
    request_preferences = dict(preferences or {})
    for attempt in range(2):
        if provider == "ollama":
            raw = _generate_with_ollama(clean_topic, request_preferences)
        elif provider == "gemini":
            raw = _generate_with_gemini(clean_topic, request_preferences)
        else:
            try:
                raw = _generate_with_ollama(clean_topic, request_preferences)
            except GeminiEngineError as local_error:
                try:
                    raw = _generate_with_gemini(clean_topic, request_preferences)
                except GeminiEngineError as cloud_error:
                    raise GeminiEngineError(
                        f"Local Ollama and Gemini both failed. Ollama: {local_error}. Gemini: {cloud_error}"
                    ) from cloud_error
        try:
            return _validate_content(raw, duration)
        except GeminiEngineError as exc:
            if attempt:
                raise
            request_preferences["_correction"] = str(exc)
    raise GeminiEngineError("Content generation exhausted its validation retry")


def _generate_with_gemini(topic: str, preferences: dict[str, Any] | None) -> str:
    api_key, model = _load_settings()
    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=_build_prompt(topic, preferences),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoContent,
            ),
        )
        return _extract_response_text(response)
    except GeminiEngineError:
        raise
    except Exception as exc:
        # Avoid echoing request headers or credential-bearing client details.
        raise GeminiEngineError(f"Gemini API request failed: {type(exc).__name__}: {exc}") from exc


def _generate_with_ollama(topic: str, preferences: dict[str, Any] | None) -> str:
    """Use Ollama's local HTTP API; no account, API key, or per-call charge."""
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
    request_body = {
        "model": model,
        "stream": False,
        "think": False,
        "format": VideoContent.model_json_schema(),
        "messages": [
            {"role": "system", "content": "Return content matching the supplied JSON schema. Follow the requested language and content requirements."},
            {"role": "user", "content": _build_prompt(topic, preferences)},
        ],
        "options": {"temperature": 0.7, "num_predict": 1000},
    }
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.getenv("OLLAMA_TIMEOUT", "240"))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise GeminiEngineError("Ollama returned an empty response")
        return content
    except urllib.error.URLError as exc:
        raise GeminiEngineError(
            f"Ollama is unavailable at {base_url}. Install/start Ollama and download {model} first. Details: {exc.reason}"
        ) from exc
    except (TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise GeminiEngineError(f"Ollama request failed: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    result = generate_video_content("Why successful people wake up early")
    print(json.dumps(result, indent=2, ensure_ascii=False))
