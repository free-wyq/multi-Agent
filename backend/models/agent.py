"""AgentDefinition + AgentCreatePayload Pydantic models.

Field names align with the frontend `AgentDefinition` interface in src/services/api.ts
and Rust `AgentDefinition` in src-tauri/src/core/types.rs (snake_case, no rename_all).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    role: str
    system_prompt: str = ""
    skills: list[str] = []
    extra_skills: list[str] = []
    mounted_skills: list[str] = []
    allowed_tools: list[str] = []
    denied_tools: list[str] = []
    startup_strategy: str = ""
    model: str = ""
    max_turns: int = 0
    description: str | None = None
    metadata_: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


class AgentCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    role: str
    system_prompt: str | None = None
    extra_skills: list[str] = []
    skills: list[str] = []
    mounted_skills: list[str] = []
    description: str | None = None
