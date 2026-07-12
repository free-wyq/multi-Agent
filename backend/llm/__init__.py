"""LLM client + JSON extraction + prompt templates (Rust llm.rs + prompts.rs)."""
from __future__ import annotations

from .client import chat_completion, get_llm_config
from .extract_json import extract_json
from .json_stream import ContentExtractor
from .prompts import (
    COORDINATOR_SYSTEM,
    TEAM_INTERACTION_SUFFIX,
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
    "ContentExtractor",
    "COORDINATOR_SYSTEM",
    "TEAM_INTERACTION_SUFFIX",
    "build_agent_generate_prompt",
    "build_brain_prompt",
    "build_coordinator_prompt",
    "build_group_name_desc_prompt",
    "build_plan_adjust_prompt",
    "build_step_recovery_prompt",
]
