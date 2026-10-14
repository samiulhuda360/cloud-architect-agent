"""The language-model client shared by the agent and the document extractors.

One OpenAI-compatible client, calls spaced at least `min_interval` seconds apart, retries with
back-off on rate limits and server errors (but not on a daily quota, which will not clear in
minutes), and a disk cache keyed by the full request, so the same question is never paid for twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import Settings

RETRY_WAITS = (15, 30, 60, 120)  # seconds; free and shared endpoints rate-limit in bursts
_last_call = [0.0]
_lock = threading.Lock()


class DailyQuotaError(RuntimeError):
    """The provider's daily quota is used up: stop, do not retry."""


def client(cfg: Settings):
    import openai

    return openai.OpenAI(api_key=cfg.llm_api_key or "not-needed", base_url=cfg.llm_base_url, timeout=180)


def extra_body(cfg: Settings) -> dict | None:
    return {"reasoning": {"effort": "low"}} if "openrouter.ai" in cfg.llm_base_url else None


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, reply TEXT)")

    @staticmethod
    def key(kw: dict) -> str:
        return hashlib.sha256(json.dumps(kw, sort_keys=True, default=str).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        with _lock:
            row = self._db.execute("SELECT reply FROM replies WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, reply: dict) -> None:
        with _lock:
            self._db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?)", (key, json.dumps(reply)))
            self._db.commit()


def _daily(exc: Exception) -> bool:
    text = str(exc).lower().replace("-", "").replace(" ", "")
    return "perday" in text


def complete(llm, cache: Cache | None = None, min_interval: float = 0.0, retry_waits=None, **kw):
    """One chat completion: from the cache if this exact request was answered before, otherwise from the
    provider, spaced and retried with back-off on rate limits, timeouts and 5xx errors."""
    import openai
    from openai.types.chat import ChatCompletion

    key = Cache.key(kw) if cache else ""
    if cache and (hit := cache.get(key)):
        return ChatCompletion.model_validate(hit)
    waits = RETRY_WAITS if retry_waits is None else retry_waits
    err: Exception = RuntimeError("provider returned no choices")
    for wait in (*waits, None):
        with _lock:
            gap = min_interval - (time.time() - _last_call[0])
            _last_call[0] = time.time() + max(0.0, gap)
        if gap > 0:
            time.sleep(gap)
        try:
            resp = llm.chat.completions.create(**kw)
            if resp.choices:
                if cache and hasattr(resp, "model_dump"):
                    cache.put(key, resp.model_dump(mode="json"))
                return resp
            err = RuntimeError("provider returned no choices")
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            if _daily(exc):
                raise DailyQuotaError(str(exc)[:300]) from exc
            err = exc
        if wait is None:
            raise err
        time.sleep(wait)
    raise err
