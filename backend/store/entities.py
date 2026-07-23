"""ORM entities for the five core tables.

Column names are snake_case and match the Rust serde shapes and the frontend
TS interfaces exactly (verified against src-tauri/src/core/types.rs). JSON
columns use sqlalchemy.JSON so Python reads/writes native list/dict. Timestamps
are stored as ISO8601 strings (front-end expects strings, not DateTime types).

Note on Message.type: the database column is named `type`; the ORM attribute is
`type_` to avoid shadowing the Python builtin. Serialization to the frontend
uses the key `type` (handled in crud.py by aliasing on the Pydantic model).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AgentEntity(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="")
    system_prompt: Mapped[str] = mapped_column(String, nullable=False, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    extra_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # IDs of SkillEntity rows mounted onto this agent (PRD AG-08/SK-04).
    # At execution time the engine resolves these to skill content and injects
    # it into the worker system prompt (PL-06 技能自主使用).
    mounted_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # IDs of McpConnectionEntity rows mounted onto this agent (PRD MC-06).
    # At execution time the engine loads these as LangChain tools via
    # langchain-mcp-adapters and merges with the framework tools (PL-07).
    mounted_mcp: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    denied_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    startup_strategy: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata_", JSON, nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class GroupEntity(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    coordinator_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class ConversationEntity(Base):
    """A single-agent (1:1) conversation — the direct-chat counterpart of GroupEntity.

    Path C (single-chat entity split): single-chat conversations are no longer
    degenerate ``GroupEntity`` rows with ``config.single_chat=True``. They have
    their own table + entity, with ``agent_id`` pointing to the single agent
    (the conversation partner). The ``coordinator_id`` field mirrors
    ``GroupEntity.coordinator_id`` (value=``agent_id``) so the frontend
    ``ChatPanel`` — which reads ``group.coordinator_id`` — works unchanged
    (C2 共享该共享的：ChatPanel 零改).

    Messages and tasks reference a conversation via ``conversation_id``
    (renamed from ``group_id`` — semantically neutral: holds either a
    ``group_id`` or a ``conversation_id``). The WS channel
    ``bus-event:{conversationId}`` reuses the same BusManager — one id one
    channel, no protocol change.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # coordinator_id mirrors agent_id so ChatPanel (reads group.coordinator_id)
    # works unchanged for single-chat conversations (C2 shared-UI principle).
    coordinator_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class MemberEntity(Base):
    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    alias: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)

    __table_args__ = (
        UniqueConstraint("group_id", "agent_id", name="uq_group_agent"),
    )


class TaskEntity(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # conversation_id: holds either a group_id (group-chat task) or a
    # conversation_id (single-chat task). Renamed from group_id (Path C strict
    # rename) — semantically neutral FK to either entity.
    conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="submitted")
    assigned_agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dependencies: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    artifact_path: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    dag_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class MessageEntity(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # conversation_id: holds either a group_id (group-chat message) or a
    # conversation_id (single-chat message). Renamed from group_id (Path C
    # strict rename) — semantically neutral FK to either entity.
    conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    receiver_id: Mapped[str] = mapped_column(String, nullable=False)
    # Column is "type" in DB; ORM attr is type_ to avoid builtin clash.
    type_: Mapped[str] = mapped_column("type", String, nullable=False, default="agent_reply")
    content: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso, index=True)


class McpConnectionEntity(Base):
    """A configured external MCP tool connection (PRD 3.4).

    Each connection is either a stdio spawn (command + args + env) or a remote
    SSE endpoint (url + headers). Agents mount connections by id
    (``AgentEntity.mounted_mcp``); at execution time the engine builds a
    ``MultiServerMCPClient`` from the enabled ones and loads LangChain tools
    (PL-07). ``enabled`` is the on/off toggle (PRD MC-03).
    """

    __tablename__ = "mcp_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    transport: Mapped[str] = mapped_column(String, nullable=False, default="stdio")
    command: Mapped[str] = mapped_column(String, nullable=False, default="")
    args: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    env: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    url: Mapped[str] = mapped_column(String, nullable=False, default="")
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class SkillEntity(Base):
    """A reusable skill capability document (PRD 3.2).

    A skill is a natural-language description of an ability. Agents mount skills
    by id (``AgentEntity.mounted_skills``); at execution time the engine resolves
    the mounted ids to ``content`` and injects it into the worker system prompt.
    ``source`` distinguishes builtin / market / custom skills (SK-09).
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    source: Mapped[str] = mapped_column(String, nullable=False, default="custom")
    installed: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[str] = mapped_column(String, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # ── frontmatter（Claude Skills 化 · 阶段一地基2）───────────────
    # 三列皆 NOT NULL DEFAULT '[]'：新库 create_all 直接建带这三列；老库由
    # _migrate_schema 的 ALTER TABLE ADD COLUMN ... DEFAULT '[]' 在启动时补齐，
    # 旧行读到空 list 而非崩溃（additive migration，与 agents.mounted_skills 同款）。
    requires_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    triggers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    outputs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class ScheduledTaskEntity(Base):
    """A scheduled task that fires a prompt at an agent on a schedule (PRD 3.5).

    ``schedule_type`` is one of ``cron`` / ``interval`` / ``once``:
    - cron: ``cron`` holds a cron expression
    - interval: ``interval_seconds`` holds the seconds between runs
    - once: ``run_at`` holds an ISO8601 datetime to fire a single time

    At fire time the scheduler pushes the ``content`` prompt onto the agent's
    inbox (reusing the resident engine), so scheduled execution uses the same
    agentic loop as interactive dispatch. ``enabled`` is the pause/resume toggle
    (TM-05).
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False, default="")
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    schedule_type: Mapped[str] = mapped_column(String, nullable=False, default="interval")
    cron: Mapped[str] = mapped_column(String, nullable=False, default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_at: Mapped[str] = mapped_column(String, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class ScheduledTaskRunEntity(Base):
    """One execution record of a scheduled task (PRD TM-07 执行历史)."""

    __tablename__ = "scheduled_task_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    scheduled_task_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


class LlmProviderEntity(Base):
    """A configured LLM service provider (PRD 多模型服务商).

    Multiple providers can be configured (OpenAI / DeepSeek / Kimi / GLM …);
    exactly one is active at a time (``is_active=1``). The active provider's
    raw config (model/base_url/api_key/temperature/max_tokens) is loaded into
    ``config._ACTIVE_CACHE`` at startup and on switch, so the sync
    ``config.get_config()`` call path stays sync (the DB is async — the cache
    bridges sync callers to the async store). The raw ``api_key`` is stored
    plaintext (single-user local desktop app, same trust level as ``.env``)
    but NEVER returned raw over HTTP — the crud mapper masks it via
    ``config._mask_key`` before building the Pydantic output model.

    Multi-model catalog: ``models`` is the provider's list of model entries
    (each carrying capability metadata — see ``LlmModel``). The connection-
    level columns (``api_version``/``organization``/``extra_headers``/
    ``request_timeout``/``max_retries``/``proxy``) describe how to reach the
    upstream endpoint and are shared by every model under this provider. The
    active model is resolved from ``models`` first (is_default → legacy
    ``model`` match → first entry), falling back to the flat ``model`` column;
    see ``crud._select_model``. ``models`` mirrors the JSON-column pattern of
    ``AgentEntity.mounted_skills`` / ``mounted_mcp`` (provider + models are
    always read/written together, never queried independently).
    """

    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String, nullable=False, default="glm-5.1")
    base_url: Mapped[str] = mapped_column(
        String, nullable=False, default="https://api.openai.com/v1"
    )
    api_key: Mapped[str] = mapped_column(String, nullable=False, default="")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    # Multi-model catalog (provider owns N models, exactly one is_default).
    # Empty list [] means "no catalog, use legacy flat model column".
    models: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Connection-level config (applies to the endpoint, shared by all models).
    # Defaults mirror LlmProvider output model / LlmProviderCreatePayload.
    api_version: Mapped[str] = mapped_column(String, nullable=False, default="")
    organization: Mapped[str] = mapped_column(String, nullable=False, default="")
    extra_headers: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    request_timeout: Mapped[float] = mapped_column(Float, nullable=False, default=120.0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    proxy: Mapped[str] = mapped_column(String, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
