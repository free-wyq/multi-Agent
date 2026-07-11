"""Configuration: load project root .env and expose settings."""
from __future__ import annotations

import os
from pathlib import Path

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

# LLM config (read at import time; engine modules consume via getters)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-5.1")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")

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
