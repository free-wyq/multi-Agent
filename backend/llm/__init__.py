"""LLM client + JSON extraction + prompt templates (Rust llm.rs + prompts.rs)."""
from __future__ import annotations

from .client import chat_completion, get_llm_config
from .extract_json import extract_json
from .prompts import (
    COORDINATOR_SYSTEM,
    build_agent_generate_prompt,
    build_brain_prompt,
    build_coordinator_prompt,
    build_group_name_desc_prompt,
    build_plan_adjust_prompt,
    build_step_recovery_prompt,
)

__all__ = [
    "chat_completion",
    "get_llm_config",
    "extract_json",
    "COORDINATOR_SYSTEM",
    "build_agent_generate_prompt",
    "build_brain_prompt",
    "build_coordinator_prompt",
    "build_group_name_desc_prompt",
    "build_plan_adjust_prompt",
    "build_step_recovery_prompt",
]
