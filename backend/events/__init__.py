"""Events package — WebSocket bus (BusManager + projection helpers)."""
from __future__ import annotations

from .bus import (
    bus_manager,
    emit_agent_status,
    emit_coordinator_plan,
    emit_coordinator_think,
    emit_message_added,
    emit_task_completed,
    emit_task_dispatched,
    emit_task_log,
    emit_task_think,
    emit_task_tool,
)

__all__ = [
    "bus_manager",
    "emit_message_added",
    "emit_task_dispatched",
    "emit_task_completed",
    "emit_task_log",
    "emit_task_tool",
    "emit_task_think",
    "emit_agent_status",
    "emit_coordinator_plan",
    "emit_coordinator_think",
]
