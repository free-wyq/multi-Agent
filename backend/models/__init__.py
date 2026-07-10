"""Pydantic models — field names aligned to frontend api.ts interfaces (snake_case)."""
from __future__ import annotations

from .agent import AgentCreatePayload, AgentDefinition
from .group import (
    Group,
    GroupCreatePayload,
    GroupFile,
    GroupMember,
)
from .mcp import McpConnection, McpConnectionCreatePayload
from .message import (
    BusEventData,
    Message,
    MessageCreatePayload,
)
from .scheduled_task import (
    ScheduledTask,
    ScheduledTaskCreatePayload,
    ScheduledTaskRun,
)
from .skill import Skill, SkillCreatePayload
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
    "Skill",
    "SkillCreatePayload",
    "McpConnection",
    "McpConnectionCreatePayload",
    "ScheduledTask",
    "ScheduledTaskCreatePayload",
    "ScheduledTaskRun",
]
