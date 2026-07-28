"""Pydantic models — field names aligned to frontend api.ts interfaces (snake_case)."""
from __future__ import annotations

from .agent import AgentCreatePayload, AgentDefinition
from .conversation import Conversation, ConversationCreatePayload, ConversationUpdatePayload
from .group import (
    Group,
    GroupConfig,
    GroupCreatePayload,
    GroupFile,
    GroupMember,
    get_collaboration_mode,
    get_leader_strategy,
)
from .im import ImChannel, ImChannelCreatePayload, ImChannelTestResult
from .llm_provider import LlmModel, LlmProvider, LlmProviderCreatePayload
from .mcp import McpConnection, McpConnectionCreatePayload
from .memory import (
    Memory,
    MemoryCreatePayload,
    MemorySearchResponse,
    MemorySearchResult,
    MemoryUpdatePayload,
)
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
from .usage import UsageReport, UsageRow, UsageTotals

__all__ = [
    "AgentDefinition",
    "AgentCreatePayload",
    "Conversation",
    "ConversationCreatePayload",
    "ConversationUpdatePayload",
    "Group",
    "GroupConfig",
    "GroupCreatePayload",
    "GroupMember",
    "GroupFile",
    "get_leader_strategy",
    "get_collaboration_mode",
    "ImChannel",
    "ImChannelCreatePayload",
    "ImChannelTestResult",
    "Task",
    "TaskCreatePayload",
    "TaskStatus",
    "Message",
    "MessageCreatePayload",
    "BusEventData",
    "Skill",
    "SkillCreatePayload",
    "SkillUploadPayload",
    "UsageReport",
    "UsageRow",
    "UsageTotals",
    "McpConnection",
    "McpConnectionCreatePayload",
    "Memory",
    "MemoryCreatePayload",
    "MemoryUpdatePayload",
    "MemorySearchResult",
    "MemorySearchResponse",
    "ScheduledTask",
    "ScheduledTaskCreatePayload",
    "ScheduledTaskRun",
    "LlmModel",
    "LlmProvider",
    "LlmProviderCreatePayload",
]
