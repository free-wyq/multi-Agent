"""Configuration: load project root .env and expose settings.

LLM config source of truth: the **active provider** cache. ``get_config()``
returns the in-memory ``_ACTIVE_CACHE`` dict (populated at startup from the
DB-backed active ``LlmProviderEntity`` and refreshed on every provider
switch / model hot-switch). If the cache is not yet populated (pre-init or
no provider configured), it falls back to the env-driven dict so early
startup and tests that don't boot the DB still get a valid config.

``get_config()`` MUST stay sync — it is called from sync code paths
(``llm/client.py get_llm_config()``) that run inside async engine code. The
DB is async (aiosqlite), so the cache bridges sync callers to the async
store: async route handlers + startup populate/refresh the cache; sync
``get_config()`` just reads it.

``get_config_public()`` masks the API key for safe HTTP exposure
(GET /api/config). ``set_config(model)`` updates both ``os.environ`` (env
fallback path) and the cache's ``model`` key (if cache is set), so
PUT /api/config model switches are visible on the next GET without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Project root is two levels up from backend/ (backend/config.py -> backend/ -> root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from project root (contains OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL)
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Data directory: env var (set by Electron) or default ~/.local/share/multi-agent
DATA_DIR = os.environ.get(
    "MULTI_AGENT_DATA_DIR",
    str(Path.home() / ".local" / "share" / "multi-agent"),
)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# LLM config (import-time snapshots for backward compat; get_config() is the
# fresh single source of truth used by new code).
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-5.1")

# Default sampling params (single source — get_config() reads these constants,
# callers never hardcode their own).
_DEFAULT_TEMPERATURE = 0.0
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_MODEL = "glm-5.1"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_PROVIDER = "openai"

# In-memory cache of the ACTIVE provider's raw config. Populated by the async
# loader at startup (``crud.load_active_provider_into_cache``) and refreshed by
# async route handlers on every provider switch / model change. ``get_config()``
# reads this synchronously so it never blocks on the async DB. None = not yet
# loaded (fall back to env-driven dict).
_ACTIVE_CACHE: dict[str, Any] | None = None


def _mask_key(key: str) -> str:
    """Mask an API key for safe display: show first 3 + last 3 chars.

    Short/empty keys collapse to ``***`` / ``""`` so no full secret is ever
    returned over HTTP.
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


def _env_config() -> dict[str, Any]:
    """Build the config dict from environment variables (the pre-cache fallback)."""
    return {
        "provider": os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER),
        "model": os.environ.get("LLM_MODEL", _DEFAULT_MODEL),
        "base_url": os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL),
        "api_key": os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", ""),
        "temperature": _DEFAULT_TEMPERATURE,
        "max_tokens": _DEFAULT_MAX_TOKENS,
    }


def get_config() -> dict[str, Any]:
    """Single source of truth for the LLM config (SYNC — must not await).

    Returns the in-memory ``_ACTIVE_CACHE`` copy if populated (the normal
    runtime path — set by the async loader from the DB-backed active provider).
    Falls back to the env-driven dict if the cache is not yet loaded (pre-init
    or no provider configured). Returns snake_case fields:

    - ``provider`` / ``model`` / ``base_url`` / ``api_key``
    - ``temperature`` / ``max_tokens``

    Engine callers (``ChatOpenAI``, ``chat_completion``) consume this dict;
    never read ``OPENAI_API_KEY`` etc. directly in new code.
    """
    if _ACTIVE_CACHE is not None:
        return dict(_ACTIVE_CACHE)
    return _env_config()


def get_config_public() -> dict[str, Any]:
    """LLM config with the API key masked — safe to return over HTTP.

    Used by GET /api/config (CF-04). Mirrors ``get_config()`` but replaces
    ``api_key`` with a masked preview so the raw secret never leaves the
    process. Also reports ``has_key`` so the UI can show "configured" without
    exposing the key.
    """
    cfg = get_config()
    key = cfg["api_key"]
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key": _mask_key(key),
        "has_key": bool(key),
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
    }


def set_active_cache(cfg: dict[str, Any]) -> None:
    """Populate the in-memory active-provider cache (called by async loaders/routes).

    Normalizes the dict to the 6 keys ``get_config()`` returns. Called from:
    - ``init_db`` → ``crud.load_active_provider_into_cache`` at startup
    - ``POST /api/providers/{id}/activate`` route handler
    - ``PUT /api/config`` model hot-switch route handler
    - ``POST/PUT/DELETE /api/providers`` when the active provider changes
    """
    global _ACTIVE_CACHE
    _ACTIVE_CACHE = {
        "provider": cfg.get("provider", _DEFAULT_PROVIDER),
        "model": cfg.get("model", _DEFAULT_MODEL),
        "base_url": cfg.get("base_url", _DEFAULT_BASE_URL),
        "api_key": cfg.get("api_key", ""),
        "temperature": float(cfg.get("temperature", _DEFAULT_TEMPERATURE)),
        "max_tokens": int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS)),
    }


def set_config(model: str | None = None) -> dict[str, Any]:
    """Hot-update the LLM model (env fallback path + cache sync).

    Writes ``model`` back to ``os.environ`` (so the env fallback branch of
    ``get_config()`` sees it) AND updates ``_ACTIVE_CACHE["model"]`` if the
    cache is set (so the cache path sees it too). None / empty ``model`` is a
    no-op. Returns the fresh ``get_config()``.

    NOTE: the primary model-switch path is now ``PUT /api/config`` which
    persists to the active provider in DB + refreshes the cache directly.
    This function is the fallback when no active provider row exists and is
    kept for backward compatibility (and the env-fallback test path).
    """
    if model:
        os.environ["LLM_MODEL"] = model
        if _ACTIVE_CACHE is not None:
            _ACTIVE_CACHE["model"] = model
    return get_config()

# MT-17: default wall-clock timeout (seconds) for a worker task execution.
# A worker that produces no result within this bound is treated as hung
# ("长时间无响应") and degraded — its in-flight LLM call is cancelled and a
# synthesized failure report-back wakes the coordinator's MT-15 recovery
# (retry/skip/reassign/keep_failed) so the plan doesn't deadlock on a
# "dispatched" step that will never complete. Generous default (300s) so a
# legitimate multi-tool task is never falsely killed (MT-13/MT-16 workers
# finish <60s; recursion_limit already bounds the turn count). A per-group
# override ``config.worker_timeout`` takes precedence (read fresh per task);
# <=0 here or per-group disables the timeout (hang-tolerant legacy behaviour).
WORKER_TASK_TIMEOUT = float(os.environ.get("WORKER_TASK_TIMEOUT", "300") or "300")
