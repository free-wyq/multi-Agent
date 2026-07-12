"""Pydantic models — field names aligned to frontend api.ts interfaces (snake_case)."""
from __future__ import annotations

from .agent import AgentCreatePayload, AgentDefinition
from .group import (
    Group,
    GroupConfig,
    GroupCreatePayload,
    GroupFile,
    GroupMember,
    get_leader_strategy,
)
from .llm_provider import LlmModel, LlmProvider, LlmProviderCreatePayload
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
from .skill import Skill, SkillCreatePayload, SkillUploadPayload
from .task import Task, TaskCreatePayload, TaskStatus

__all__ = [
    "AgentDefinition",
    "AgentCreatePayload",
    "Group",
    "GroupConfig",
    "GroupCreatePayload",
    "GroupMember",
    "GroupFile",
    "get_leader_strategy",
    "Task",
    "TaskCreatePayload",
    "TaskStatus",
    "Message",
    "MessageCreatePayload",
    "BusEventData",
    "Skill",
    "SkillCreatePayload",
    "SkillUploadPayload",
    "McpConnection",
    "McpConnectionCreatePayload",
    "ScheduledTask",
    "ScheduledTaskCreatePayload",
    "ScheduledTaskRun",
    "LlmModel",
    "LlmProvider",
    "LlmProviderCreatePayload",
]
