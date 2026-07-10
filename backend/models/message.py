"""Message + MessageCreatePayload + BusEventData Pydantic models.

Critical: the persisted field is `type` (not `kind`), matching the Rust
`#[serde(rename = "type")]` and the frontend `Message.type` field.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    group_id: str
    task_id: str | None = None
    sender_id: str
    receiver_id: str
    type: str = Field(default="agent_reply")
    content: str | None = None
    data: dict[str, Any] | None = None
    created_at: str = ""


class MessageCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    group_id: str
    task_id: str | None = None
    sender_id: str
    receiver_id: str | None = None
    type: str | None = Field(default=None)
    content: str | None = None
    data: dict[str, Any] | None = None


class BusEventData(BaseModel):
    """Event payload pushed over WebSocket `bus-event:{groupId}` channel."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    group_id: str
    task_id: str | None = None
    sender_id: str
    receiver_id: str
    type: str
    content: str | None = None
    data: Any = None
    timestamp: str = ""
