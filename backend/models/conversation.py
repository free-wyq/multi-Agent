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
    """

    model_config = ConfigDict(extra="allow")

    id: str
    agent_id: str
    name: str = ""
    coordinator_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class ConversationCreatePayload(BaseModel):
    """Payload for POST /api/conversations (find-or-create semantics).

    Only ``agent_id`` is required; ``name`` is optional and defaults to the
    agent's name (filled by the CRUD layer when omitted).
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    name: str | None = None
