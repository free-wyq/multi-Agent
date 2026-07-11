"""Configuration: load project root .env and expose settings.

LLM config single source of truth: ``get_config()`` reads the environment
live on every call (not cached at import), so ``set_config(model)`` — which
writes back to ``os.environ`` — takes effect on the next engine invoke without
a process restart. ``get_config_public()`` masks the API key for safe HTTP
exposure (GET /api/config). ``get_llm_config()`` and the engine ChatOpenAI
callers are migrated to these getters in CF-02/CF-03.

The module-level ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``LLM_MODEL``
constants are kept for backward compatibility with not-yet-migrated callers
(import-time snapshots; ``get_config()`` is the fresh source). ``LLM_PROVIDER``
was read-but-never-used dead config and has been removed; provider is now
surfaced live via ``get_config()["provider"]``.
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


def get_config() -> dict[str, Any]:
    """Single source of truth for the LLM config.

    Reads the environment live on every call (not an import-time snapshot) so
    ``set_config()`` model switches are picked up by the next engine invoke
    without a restart. Returns snake_case fields:

    - ``provider``: LLM_PROVIDER (default ``openai``)
    - ``model``: LLM_MODEL (default ``glm-5.1``)
    - ``base_url``: OPENAI_BASE_URL (default OpenAI)
    - ``api_key``: OPENAI_API_KEY, falling back to ANTHROPIC_API_KEY
    - ``temperature`` / ``max_tokens``: sampling defaults

    Engine callers (``ChatOpenAI``, ``chat_completion``) consume this dict;
    never read ``OPENAI_API_KEY`` etc. directly in new code.
    """
    return {
        "provider": os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER),
        "model": os.environ.get("LLM_MODEL", _DEFAULT_MODEL),
        "base_url": os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE_URL),
        "api_key": os.environ.get("OPENAI_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", ""),
        "temperature": _DEFAULT_TEMPERATURE,
        "max_tokens": _DEFAULT_MAX_TOKENS,
    }


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


def set_config(model: str | None = None) -> dict[str, Any]:
    """Hot-update the LLM config by writing back to ``os.environ``.

    None / empty args are skipped (no-op for that key). Because
    ``get_config()`` reads the environment live, the change is effective on the
    next engine invoke — no restart needed (CF-05). Returns the fresh
    ``get_config()`` so the caller can echo the post-write state.

    Currently only ``model`` is mutable (PUT /api/config switches model);
    base_url / provider remain env-driven.
    """
    if model:
        os.environ["LLM_MODEL"] = model
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
