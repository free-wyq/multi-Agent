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
