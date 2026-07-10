"""Pydantic models — field names aligned to frontend api.ts interfaces (snake_case)."""
from __future__ import annotations

from .agent import AgentCreatePayload, AgentDefinition
from .group import (
    Group,
    GroupCreatePayload,
    GroupFile,
    GroupMember,
)
from .message import (
    BusEventData,
    Message,
    MessageCreatePayload,
)
from .task import Task, TaskCreatePayload, TaskStatus

__all__ = [
    "AgentDefinition",
    "AgentCreatePayload",
    "Group",
    "GroupCreatePayload",
    "GroupMember",
    "GroupFile",
    "Task",
    "TaskCreatePayload",
    "TaskStatus",
    "Message",
    "MessageCreatePayload",
    "BusEventData",
]
