"""LLM client + JSON extraction + prompt templates (Rust llm.rs + prompts.rs)."""
from __future__ import annotations

from .client import chat_completion, get_llm_config
from .extract_json import extract_json
from .prompts import (
    COORDINATOR_SYSTEM,
    build_brain_prompt,
    build_coordinator_prompt,
)

__all__ = [
    "chat_completion",
    "get_llm_config",
    "extract_json",
    "COORDINATOR_SYSTEM",
    "build_brain_prompt",
    "build_coordinator_prompt",
]
