"""Conversation + ConversationCreatePayload Pydantic models (Path C single-chat split).

``Conversation`` mirrors ``ConversationEntity`` (the single-chat counterpart of
``Group``). It carries a ``coordinator_id`` field (value=``agent_id``) so the
frontend ``ChatPanel`` — which reads ``group.coordinator_id`` to resolve the
streaming-bubble sender — works unchanged for single-chat conversations (C2
shared-UI principle: ChatPanel 零改).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Conversation(BaseModel):
    """A single-agent (1:1) conversation — direct-chat counterpart of Group.

    ``coordinator_id`` mirrors ``agent_id`` so ChatPanel (reads
    ``group.coordinator_id``) renders the streaming bubble with the right
    sender without code changes. ``name`` defaults to the agent's name when
    unset (filled at creation time by the CRUD layer).

    T86 豆包式常驻助手：``transient`` 标记体验会话（1=不进侧栏，0=正式）。
    智能体广场点 agent 开的是体验会话（transient=1），侧栏【会话】列的正式会话
    都绑 platform_assistant（transient=0）。``list_conversations`` 过滤此列。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    agent_id: str
    name: str = ""
    coordinator_id: str = ""
    transient: int = 0
    created_at: str = ""
    updated_at: str = ""


class ConversationCreatePayload(BaseModel):
    """Payload for POST /api/conversations.

    T86 后语义：``agent_id`` 为空时绑定平台常驻助手（slug='platform_assistant'）。
    智能体广场点 agent 的体验对话传 ``agent_id=<that_agent>`` + ``transient=1``；
    侧栏【会话】新建会话不传 ``agent_id``（默认绑平台助手）+ ``transient=0``。
    ``name`` 默认空，由首条用户消息触发自动生成（见 ``api/messages.py``）。
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str | None = None
    name: str | None = None
    transient: int = 0


class ConversationUpdatePayload(BaseModel):
    """Payload for PUT /api/conversations/{id}（会话管理·重命名）。

    部分更新语义（仅传字段被写）。目前后端只支持改 ``name``——``crud.update_conversation_name``
    覆写 name + updated_at。``extra="allow"`` 容忍未来扩展字段（pin/置顶等）而不破现有 caller。
    侧栏「管理」重命名入口走此端点；写回后 emit ``conversation_updated`` 让前端订阅了该通道
    的组件刷新标题（与首条消息自动命名的 emit 同一通道复用）。
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
