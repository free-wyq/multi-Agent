"""Skill + SkillCreatePayload Pydantic models.

A Skill is a reusable capability document: a natural-language description of
an ability an agent can use (PRD 3.2). Skills are mounted onto agents by id;
at execution time the engine injects the mounted skills' content into the
worker's system prompt (PL-06).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Skill(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str = ""
    # source: builtin | market | custom (PRD SK-09)
    source: str = "custom"
    # installation status: installed | not_installed (PRD SK-09)
    installed: bool = True
    # the natural-language skill body — what gets injected into the agent prompt
    content: str = ""
    # optional tags for search/filter (PRD SK-09)
    tags: list[str] = []
    # agent ids this skill is mounted on (computed at read time, not stored)
    mounted_to: list[str] = []
    created_at: str = ""
    updated_at: str = ""


class SkillCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    content: str | None = None
    source: str = "custom"
    tags: list[str] = []
