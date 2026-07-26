"""Memory (long-term, cross-session) Pydantic models (PRD 记忆模块 · 任务17b).

A memory is a single persistent fact/preference/conclusion scoped by ``scope``
(global / agent / conversation). ``content`` is the natural-language statement;
``importance`` (0.0–1.0) ranks relevance at retrieval time. Retrieved memories
are injected into the system prompt as a「关于用户的长期记忆」section,
distinct from the L1 session-context (recent messages) which is unchanged.

This is the L2 long-term layer (design: docs/memory-module-design.md); the L1
session-context (``engine/group_runtime.py _memory`` /
``worker._build_context_from_db``) is NOT replaced.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Memory(BaseModel):
    """A long-term memory record as returned by the API."""

    model_config = ConfigDict(extra="allow")

    id: str
    user_id: str = "local"
    scope: str = "global"  # global | agent | conversation
    scope_ref: str = ""
    content: str = ""
    metadata_: dict[str, Any] | None = None
    importance: float = 0.5
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str | None = None
    access_count: int = 0


class MemoryCreatePayload(BaseModel):
    """POST /api/memory body — manual create (user types a memory in the UI).

    ``importance`` defaults to 1.0 for manual entries (user explicitly saved
    it → high value); auto-extracted memories (future v2 LLM extractor) would
    default lower. ``scope`` + ``scope_ref`` are optional (default global).
    """

    model_config = ConfigDict(extra="allow")

    content: str
    scope: str = "global"
    scope_ref: str = ""
    importance: float = 1.0
    metadata_: dict[str, Any] | None = None
    enabled: bool = True
    user_id: str = "local"


class MemoryUpdatePayload(BaseModel):
    """PUT /api/memory/{id} body — partial update of content/importance/enabled.

    All fields optional (``exclude_unset`` semantics on the crud side → only
    provided fields are written).
    """

    model_config = ConfigDict(extra="allow")

    content: str | None = None
    scope: str | None = None
    scope_ref: str | None = None
    importance: float | None = None
    enabled: bool | None = None
    metadata_: dict[str, Any] | None = None


class MemorySearchResult(BaseModel):
    """One hit from POST /api/memory/search."""

    model_config = ConfigDict(extra="allow")

    memory: Memory
    score: float = 0.0  # FTS5 bm25-derived relevance (lower bm25 = better; we negate)


class MemorySearchResponse(BaseModel):
    """POST /api/memory/search response — top-k memories ranked by relevance."""

    model_config = ConfigDict(extra="allow")

    query: str
    top_k: int = 5
    results: list[MemorySearchResult] = []
