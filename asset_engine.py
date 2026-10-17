"""Local background-video asset selection for the Shorts pipeline."""

from __future__ import annotations

import json
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
DEFAULT_ASSET_COUNT = 3
FALLBACK_CATEGORY = "fallback"
DEFAULT_CATEGORIES = ("sunrise", "morning_routine", "productivity", FALLBACK_CATEGORY)
STATE_FILENAME = ".selection_state.json"
MIN_TOKEN_LENGTH = 3

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ASSETS_DIR = PROJECT_ROOT / "data" / "assets"


class AssetEngineError(Exception):
    """Raised when background-asset selection fails."""


class AssetProvider(ABC):
    """Abstraction for background-video sources.

    Swap this later for a licensed stock provider without changing callers.
    """

    @abstractmethod
    def find_assets(self, keywords: list[str], count: int = DEFAULT_ASSET_COUNT) -> list[str]:
        """Return one or more valid local video paths for the given keywords."""


class LocalAssetProvider(AssetProvider):
    """Search a local data/assets library by category and keyword."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser() if root else DEFAULT_ASSETS_DIR
        self._ensure_library()

    def find_assets(self, keywords: list[str], count: int = DEFAULT_ASSET_COUNT) -> list[str]:
        clean_keywords = _normalize_keywords(keywords)
        if count < 1:
            raise AssetEngineError("count must be at least 1")

        videos = self._list_valid_videos()
        if not videos:
            return []

        scored = sorted(
            ((self._score(path, clean_keywords), path) for path in videos),
            key=lambda item: (-item[0], str(item[1]).lower()),
        )
        matches = [path for score, path in scored if score > 0]

        if matches:
            selected = self._rotate(matches, count)
        else:
            selected = self._fallback(videos, count)

        return [str(path) for path in selected]

    def _ensure_library(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for category in DEFAULT_CATEGORIES:
                (self.root / category).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AssetEngineError(
                f"Could not create local asset directory structure at {self.root}: {exc}"
            ) from exc

    def _list_valid_videos(self) -> list[Path]:
        if not self.root.exists():
            return []

        files: list[Path] = []
        for path in sorted(self.root.rglob("*"), key=lambda item: str(item).lower()):
            if path.name.startswith("."):
                continue
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if _is_readable_video(path):
                files.append(path.resolve())
        return files

    def _score(self, path: Path, keywords: list[str]) -> int:
        category = _normalize_text(path.parent.name)
        haystack = f"{category} {_normalize_text(path.stem)}"
        haystack_tokens = _tokens(category) | _tokens(path.stem)
        score = 0

        for keyword in keywords:
            keyword_tokens = _tokens(keyword)
            slug = _normalize_text(keyword)
            if not slug:
                continue
            if category == slug:
                score += 100
            elif slug and slug in haystack:
                score += 60
            overlap = haystack_tokens & keyword_tokens
            if overlap == keyword_tokens and keyword_tokens:
                score += 40
            elif overlap:
                score += 10 * len(overlap)

        return score

    def _fallback(self, videos: list[Path], count: int) -> list[Path]:
        fallback_dir = (self.root / FALLBACK_CATEGORY).resolve()
        fallback_videos = [
            path
            for path in videos
            if path.parent.resolve() == fallback_dir
        ]
        # Only use general footage when the library marks it as a fallback.
        return self._rotate(fallback_videos, count)

    def _rotate(self, candidates: list[Path], count: int) -> list[Path]:
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)

        if not unique:
            return []

        used = self._load_used()
        unused = [path for path in unique if str(path) not in used]
        pool = unused or unique
        selected = pool[: min(count, len(pool))]

        updated = used + [str(path) for path in selected]
        self._save_used(updated[-50:])
        return selected

    def _state_path(self) -> Path:
        return self.root / STATE_FILENAME

    def _load_used(self) -> list[str]:
        path = self._state_path()
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        used = payload.get("used") if isinstance(payload, dict) else None
        if not isinstance(used, list):
            return []
        return [item for item in used if isinstance(item, str)]

    def _save_used(self, used: list[str]) -> None:
        path = self._state_path()
        try:
            path.write_text(
                json.dumps({"used": used}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # Rotation state is best-effort; selection still remains deterministic.
            return


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", value.lower())
        if len(token) >= MIN_TOKEN_LENGTH
    }


def _normalize_keywords(keywords: list[str] | None) -> list[str]:
    if not keywords:
        raise AssetEngineError("keywords must be a non-empty list")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in keywords:
        if not isinstance(item, str) or not item.strip():
            continue
        text = " ".join(item.split())
        key = _normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    if not cleaned:
        raise AssetEngineError("keywords must contain at least one non-empty string")
    return cleaned


def _is_readable_video(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        return os.access(path, os.R_OK)
    except OSError:
        return False


def get_asset_provider(root: str | Path | None = None) -> AssetProvider:
    """Return the active asset provider. Local files only for this module."""
    assets_root = root or os.getenv("ASSETS_DIR", "").strip() or None
    return LocalAssetProvider(assets_root)


def find_background_assets(
    keywords: list[str],
    count: int = DEFAULT_ASSET_COUNT,
    root: str | Path | None = None,
) -> list[str]:
    """Select local background videos for Gemini bg_video_keywords."""
    provider = get_asset_provider(root)
    return provider.find_assets(keywords, count=count)


def count_local_assets(root: str | Path | None = None) -> int:
    """Count readable local video candidates for the setup/readiness screen."""
    provider = get_asset_provider(root)
    return len(provider._list_valid_videos())


if __name__ == "__main__":
    SAMPLE_KEYWORDS = ["sunrise", "morning routine", "productivity"]

    try:
        provider = LocalAssetProvider()
        assets = provider.find_assets(SAMPLE_KEYWORDS)
    except AssetEngineError as exc:
        print(f"Asset search failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Keywords: {SAMPLE_KEYWORDS}")
    print(f"Asset library: {provider.root}")

    if assets:
        print("Suitable local assets found:")
        for asset in assets:
            print(f"  {asset}")
        sys.exit(0)

    print("No suitable local assets were found.")
    print(
        "Add royalty-free background videos you own or are licensed to use into "
        "category folders under data/assets, for example:"
    )
    print("  data/assets/sunrise/")
    print("  data/assets/morning_routine/")
    print("  data/assets/productivity/")
    print("  data/assets/fallback/")
    print("Supported extensions: " + ", ".join(sorted(VIDEO_EXTENSIONS)))
    sys.exit(0)
