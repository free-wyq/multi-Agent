"""BusManager — WebSocket connections per group, plus event projection helpers.

Frontend subscribes to `ws://localhost:8000/ws/bus/{groupId}`; the manager keeps
a set of sockets per group and fans out event dicts. Event projection (domain →
BusEventData) is implemented here too; the engine layer calls these in M3+.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class BusManager:
    """Maintains a set of live WebSocket connections per group_id."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    def subscribe(self, group_id: str, ws: WebSocket) -> None:
        self._connections.setdefault(group_id, set()).add(ws)

    def unsubscribe(self, group_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(group_id)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            del self._connections[group_id]

    async def emit(self, group_id: str, event_data: dict[str, Any]) -> None:
        """Push an event dict to every live socket in the group. Prunes dead ones."""
        conns = self._connections.get(group_id)
        if not conns:
            return
        dead: list[WebSocket] = []
        for ws in list(conns):
            try:
                await ws.send_json(event_data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(group_id, ws)


bus_manager = BusManager()


# ── event projection helpers (DomainEvent → BusEventData) ─────────────

async def emit_message_added(msg: dict[str, Any]) -> None:
    await bus_manager.emit(
        msg["group_id"],
        {
            "id": msg["id"],
            "group_id": msg["group_id"],
            "task_id": msg.get("task_id"),
            "sender_id": msg["sender_id"],
            "receiver_id": msg["receiver_id"],
            "type": msg["type"],
            "content": msg.get("content"),
            "data": msg.get("data"),
            "timestamp": msg["created_at"],
        },
    )


async def emit_task_dispatched(
    group_id: str,
    task_id: str,
    step: int,
    agent_id: str,
    agent_name: str,
    instruction: str,
) -> None:
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": "coordinator",
            "receiver_id": agent_id,
            "type": "task_dispatch",
            "content": instruction,
            "data": {"step": step, "agent_name": agent_name, "agent_id": agent_id},
            "timestamp": _ts(),
        },
    )


async def emit_task_completed(
    group_id: str,
    task_id: str,
    agent_id: str,
    success: bool,
    result: str,
    exit_code: int | None,
) -> None:
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "task_complete" if success else "task_failed",
            "content": result,
            "data": {"exit_code": exit_code},
            "timestamp": _ts(),
        },
    )


async def emit_task_log(group_id: str, task_id: str, sender_id: str, line: str) -> None:
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": sender_id,
            "receiver_id": "broadcast",
            "type": "task_log",
            "content": line,
            "data": None,
            "timestamp": _ts(),
        },
    )
