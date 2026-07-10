"""Events package — WebSocket bus (BusManager + projection helpers)."""
from __future__ import annotations

from .bus import (
    bus_manager,
    emit_message_added,
    emit_task_completed,
    emit_task_dispatched,
    emit_task_log,
)

__all__ = [
    "bus_manager",
    "emit_message_added",
    "emit_task_dispatched",
    "emit_task_completed",
    "emit_task_log",
]
