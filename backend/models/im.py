"""ImChannel + payload + test-result Pydantic models (PRD 任务19 · IM 网关).

An ``ImChannel`` is a bidirectional bridge to an external IM bot (WeChat Work /
DingTalk / Feishu). One row = one configured bot connection. The ``platform``
selects the ``ImChannelAdapter`` (``engine/im/adapters``); ``config`` holds
platform-specific credentials (``app_id`` / ``app_secret`` / ``verify_token`` /
``webhook_url``); ``target_conversation_id`` is the internal conversation/group
the inbound message routes to (and whose agent replies get pushed back out).

See ``docs/im-gateway-design.md`` §4 (schema) + §6 (API). Mirrors
``McpConnection`` (credentials JSON + enable toggle + namespace prefix) and
``ScheduledTask`` (external trigger source → agent inbox).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ImChannel(BaseModel):
    """A configured IM platform channel (serialized to the frontend).

    ``config`` is returned **masked** by the API layer (``api/im.py`` reuses
    ``mcp._mask_sensitive``) — sensitive credential keys (``app_secret`` /
    ``verify_token`` / ``*token*`` / ``*secret*``) are replaced with ``"***"``
    on GET so the UI never renders raw secrets. POST/PUT accept plaintext; PUT
    merges ``"***"``-valued fields with the stored original (see
    ``mcp._merge_masked_fields``).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    # wechat | dingtalk | feishu — selects the adapter (见 engine/im/adapters)
    platform: str
    # 平台特异凭证 JSON：{app_id, app_secret, verify_token, webhook_url, ...}.
    # GET 返回脱敏副本；POST/PUT 接受明文。
    config: dict[str, Any] | None = None
    # 入站投递目标：单聊 conversation_id 或群聊 group_id（Path C 后两语义统一于
    # conversation_id 字段）。gateway 按 target_kind 分流。
    target_conversation_id: str
    # single | group — 决定入站走 route_direct_message 还是 route_user_message
    target_kind: str = "single"
    # 该 channel 绑定的 agent（单聊即 conversation.agent_id；群聊可空，走 @mention 路由）
    target_agent_id: str = ""
    # 默认 disabled——channel 显式 enable 后才接收入站
    enabled: bool = False
    # 出站 mock 日志开关：mock 阶段恒 True，logger.info 记出站
    outbound_log: bool = True
    # 平台会话映射：{platform_session_id → internal_conversation_id}（MVP 可空）
    session_bindings: dict[str, Any] | None = None
    metadata_: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


class ImChannelCreatePayload(BaseModel):
    """Payload for POST/PUT /api/im-channels.

    All fields optional except ``name`` / ``platform`` /
    ``target_conversation_id`` so a minimal channel can be created and edited
    incrementally. ``enabled`` defaults to False (a channel must be explicitly
    enabled before accepting inbound — prevents stray inbound when credentials
    are misconfigured, mirroring ``ScheduledTaskEntity.enabled`` default).
    """

    model_config = ConfigDict(extra="allow")

    name: str
    platform: str
    config: dict[str, Any] | None = None
    target_conversation_id: str
    target_kind: str = "single"
    target_agent_id: str = ""
    enabled: bool = False
    outbound_log: bool = True
    session_bindings: dict[str, Any] | None = None
    metadata_: dict[str, Any] | None = None


class ImChannelTestResult(BaseModel):
    """Result of POST /api/im-channels/{id}/test (mock outbound probe).

    The test endpoint instantiates the channel's adapter and calls
    ``send_outbound`` with a probe payload targeted at ``config.default_session``
    (or ``"default"``). Mock stage: ``send_outbound`` is ``logger.info``, so
    ``ok=True`` means the adapter loaded + the log line fired (caplog asserts
    the line in the e2e test). Real platforms (档四): ``ok=True`` means the
    HTTP push succeeded; ``error`` carries the failure detail on ``ok=False``.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    platform: str = ""
    target: str = ""
    error: str | None = None
