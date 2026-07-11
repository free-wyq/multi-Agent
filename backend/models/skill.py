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


class SkillUploadPayload(BaseModel):
    """SK-05: metadata for uploading an existing SKILL.md file as a skill.

    Used by ``POST /api/skills/upload`` as a multipart ``Form()`` body — the
    file itself is a separate ``UploadFile`` parameter, and this model carries
    only the optional metadata. ``name`` is optional because the endpoint
    falls back to the uploaded file's stem (filename without ``.md``) when the
    user does not supply an explicit name. ``source`` defaults to ``custom``
    (uploaded skills are user-provided, not ``builtin``/``market``) to stay
    within the existing ``Skill.source`` taxonomy consumed by SK-09 search.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    description: str | None = None
    source: str = "custom"
    tags: list[str] = []
