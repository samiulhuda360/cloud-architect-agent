"""Settings from environment variables (and an optional .env file).

Any OpenAI-compatible endpoint with tool calling works. Set AI_API_KEY alone to use Google Gemini
(`gemini-flash-lite-latest` through Gemini's OpenAI-compatible endpoint), or LLM_BASE_URL,
LLM_API_KEY and LLM_MODEL for OpenRouter, OpenAI, Azure OpenAI or a local server (Ollama, vLLM).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def _gemini() -> bool:
    return bool(_env("AI_API_KEY")) and not _env("LLM_BASE_URL") and not _env("AI_BASE_URL")


@dataclass(frozen=True)
class Settings:
    price_cache: Path = field(default_factory=lambda: Path(_env("PRICE_CACHE", str(ROOT / "cache" / "prices.sqlite"))))
    llm_base_url: str = field(
        default_factory=lambda: _env("LLM_BASE_URL", _env("AI_BASE_URL", GEMINI_URL if _gemini() else "https://openrouter.ai/api/v1"))
    )
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", _env("AI_API_KEY", _env("OPENROUTER_API_KEY"))))
    llm_model: str = field(
        default_factory=lambda: (
            _env("AI_MODEL", "gemini-flash-lite-latest") if _gemini() else _env("LLM_MODEL", _env("AI_MODEL", "openai/gpt-4.1-mini"))
        )
    )
    max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "8000")))
    # Free and shared endpoints rate-limit; calls are spaced at least this many seconds apart.
    min_interval: float = field(default_factory=lambda: float(_env("AI_MIN_INTERVAL", "2.5")))
    # Model replies are cached on disk by request, so re-running an evaluation or extraction costs nothing.
    llm_cache: Path | None = field(
        default_factory=lambda: None if _env("LLM_CACHE") == "off" else Path(_env("LLM_CACHE", str(ROOT / "cache" / "llm.sqlite")))
    )

    @property
    def llm_configured(self) -> bool:
        hosted = "openrouter.ai" in self.llm_base_url or GEMINI_URL in self.llm_base_url
        return bool(self.llm_api_key) or not hosted


def settings() -> Settings:
    return Settings()
