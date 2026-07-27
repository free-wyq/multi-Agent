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
    # NOTE: ``allowed_tools`` / ``denied_tools`` columns removed (死代码清理 ·
    # 2026-07-27). Seed always wrote []; the engine never consumed them
    # (tool gating is the skill-sandbox denylist in engine/tools.py + the MCP
    # stdio whitelist, per [[mcp-security-vh61-2026-07-23]]). Legacy DBs keep
    # the columns physically (SQLite never DROPs on its own) but the ORM no
    # longer maps/reads/writes them — additive migration never drops.
    startup_strategy: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    # PRD AG-08: english slug for URL-safe identification + icon emoji for UI avatar.
    # Both nullable for backward-compat with pre-existing agent rows.
    slug: Mapped[str | None] = mapped_column(String, nullable=True)
    icon_emoji: Mapped[str | None] = mapped_column(String, nullable=True)
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
    # T86 豆包式常驻助手：1=体验会话不进侧栏（点智能体广场 agent 临时会话），
    # 0=正式会话（侧栏列出）。list_conversations 过滤此列，只返非体验会话。
    # 默认 0——存量库行为不变；新建会话默认进侧栏，体验会话由 caller 显式传 1。
    transient: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
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


class ImChannelEntity(Base):
    """An IM platform channel — a bidirectional bridge to an external IM bot.

    One row = one configured IM bot connection (e.g. one DingTalk robot). The
    ``platform`` selects the ``ImChannelAdapter``; ``config`` holds the
    platform-specific credentials (app_id / app_secret / verify_token /
    webhook_url); ``target_conversation_id`` is the internal conversation/group
    the inbound message routes to (and whose agent replies get pushed back out).
    At fire time the gateway (任务19c) calls ``route_user_message`` /
    ``route_direct_message`` to reuse the existing inbound routing — IM is just
    another ``push_notify`` caller (same shape as the scheduler). ``enabled`` is
    the connect/disconnect toggle (mirrors ``ScheduledTaskEntity.enabled``); it
    defaults to 0 (disabled) so a channel must be explicitly enabled before
    accepting inbound — prevents stray inbound when credentials are misconfigured.

    See ``docs/im-gateway-design.md`` §4 for the full schema + relationships.
    """

    __tablename__ = "im_channels"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # wechat | dingtalk | feishu — selects the adapter (见 engine/im/adapters)
    platform: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 平台特异凭证 JSON：{app_id, app_secret, verify_token, webhook_url, ...}
    # mock 阶段可空/占位；真平台时填实。敏感字段 API 返回时脱敏（任务19c 参考
    # mcp._mask_sensitive）。
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 入站投递目标：单聊 conversation_id 或群聊 group_id（Path C 后两语义统一于
    # conversation_id 字段）。gateway 按 target_kind 分流到 route_direct_message /
    # route_user_message。
    target_conversation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # single | group — 决定入站走 route_direct_message 还是 route_user_message
    target_kind: Mapped[str] = mapped_column(String, nullable=False, default="single")
    # 该 channel 绑定的 agent（单聊即 conversation.agent_id；群聊可空，走 @mention 路由）
    target_agent_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    # 默认 disabled——channel 显式 enable 后才接收入站（防误配凭证时被外部刷入站）
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    # 出站 mock 日志开关：mock 阶段恒 True，logger.info 记出站；档四真平台时此字段
    # 语义变为「是否记出站审计日志」。
    outbound_log: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    # 平台会话映射：{platform_session_id → internal_conversation_id}
    # 一对多：一个 channel 可能对应平台上多个群/多个单聊用户，每个映射到不同的内部会话。
    # MVP 可空（target_conversation_id 即唯一投递点）；多会话路由留档四。
    session_bindings: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata_", JSON, nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class MemoryEntity(Base):
    """A long-term memory record (PRD 记忆模块 · 任务17).

    A memory is a single persistent fact/preference/conclusion extracted from
    conversations and surfaced across sessions. Scoped by ``scope`` (global /
    agent / conversation); ``content`` is the natural-language statement;
    ``importance`` (0.0–1.0) ranks relevance at retrieval time. Retrieved
    memories are injected into the system prompt as a「关于用户的长期记忆」
    section, distinct from the L1 session-context (recent messages) which is
    unchanged.

    v1 retrieval uses SQLite FTS5 full-text search over ``content`` via a
    sidecar ``memories_fts`` virtual table (``trigram`` tokenizer — Chinese
    substring match, zero external deps). v2 may add a ``vector_embedding``
    column + semantic search (sqlite-vss / external store); the schema reserves
    no column slot here (added later via additive ALTER) so v1 keeps the row
    narrow.
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, default="local", index=True)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="global", index=True)
    # global | agent | conversation — see §4.3 of docs/memory-module-design.md
    scope_ref: Mapped[str] = mapped_column(String, nullable=False, default="")
    # scope=agent → agent_id; scope=conversation → conversation_id; global → ""
    content: Mapped[str] = mapped_column(String, nullable=False, default="")
    # 自然语言陈述，如「用户是 Java 后端工程师，偏好简洁回复」
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 来源溯源：{source_conversation_id, source_agent_id, extracted_at, ...}
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # 0.0–1.0，检索时与相关性加权排序；MVP 规则版赋值，v2 LLM 评估
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)
    # 软删除/禁用开关（用户可在前端手动禁用某条记忆）
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    last_accessed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 检索命中时更新（last_accessed_at + access_count），用于衰减排序 + 前端「最近用过」展示


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
