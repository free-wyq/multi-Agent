"""A2A inbox: asyncio.Queue per (group_id, agent_id) channel (Rust inbox.rs).

Each agent gets one ``asyncio.Queue`` as its message channel. ``push_task``
and ``push_notify`` drop items into the target queue, waking the resident
``AgentEngine._run_loop``. Task and notify queues are also held in-memory dicts
keyed by group_id so ``claim_task`` / ``complete_task`` can mutate status
without a separate store. No cross-process persistence in M3 (M2 already
persists entities to SQLite); the queue dicts are the single in-memory source.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# (group_id, agent_id) -> asyncio.Queue
_inboxes: dict[tuple[str, str], asyncio.Queue] = {}
# group_id -> list[TaskQueueItem dict]
_task_queues: dict[str, list[dict]] = defaultdict(list)
# group_id -> list[NotifyQueueItem dict]
_notify_queues: dict[str, list[dict]] = defaultdict(list)
_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_inbox(group_id: str, agent_id: str) -> asyncio.Queue:
    """Return (creating if absent) the asyncio.Queue channel for an agent."""
    key = (group_id, agent_id)
    if key not in _inboxes:
        _inboxes[key] = asyncio.Queue()
    return _inboxes[key]


def register_inbox(group_id: str, agent_id: str) -> asyncio.Queue:
    """Register an agent inbox channel (idempotent). Returns the queue."""
    return get_inbox(group_id, agent_id)


def unregister_inbox(group_id: str, agent_id: str) -> None:
    """Remove an agent inbox channel (engine stop)."""
    _inboxes.pop((group_id, agent_id), None)


async def push_task(
    group_id: str,
    sender_id: str,
    receiver_id: str,
    content: str,
    data: dict | None = None,
) -> dict[str, Any]:
    """Push a task item onto the receiver's queue and into the task list.

    Constructs a TaskQueueItem dict (id prefixed ``tq_``), appends to
    ``_task_queues[group_id]`` (truncated to 2000), and puts onto the target
    asyncio.Queue channel. Returns the item.
    """
    item: dict[str, Any] = {
        "id": f"tq_{uuid.uuid4().hex}",
        "group_id": group_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "content": content,
        "data": data,
        "created_at": _now_iso(),
        "status": "pending",
        "claimed_by": None,
        "result": None,
        "result_data": None,
        "completed_at": None,
    }
    async with _lock:
        _task_queues[group_id].append(item)
        if len(_task_queues[group_id]) > 2000:
            _task_queues[group_id] = _task_queues[group_id][-2000:]
    inbox = get_inbox(group_id, receiver_id)
    await inbox.put({"kind": "task", "item": item})
    return item


async def push_notify(
    group_id: str,
    kind: str,
    sender_id: str,
    receiver_id: str,
    content: str,
    data: dict | None = None,
) -> dict[str, Any]:
    """Push a notify item onto the receiver's queue and into the notify list.

    If ``receiver_id == "broadcast"`` the item is delivered to every agent
    inbox in the group. Otherwise it goes to the single target. Returns the item.
    """
    item: dict[str, Any] = {
        "id": f"nq_{uuid.uuid4().hex}",
        "group_id": group_id,
        "type": kind,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "content": content,
        "data": data,
        "created_at": _now_iso(),
    }
    async with _lock:
        _notify_queues[group_id].append(item)
        if len(_notify_queues[group_id]) > 500:
            _notify_queues[group_id] = _notify_queues[group_id][-500:]

    if receiver_id == "broadcast":
        for (gid, _aid), inbox in _inboxes.items():
            if gid == group_id:
                await inbox.put({"kind": "notify", "item": item})
    else:
        inbox = get_inbox(group_id, receiver_id)
        await inbox.put({"kind": "notify", "item": item})
    return item


async def claim_task(group_id: str, agent_id: str, instance_id: str) -> dict | None:
    """Claim the first pending task whose receiver_id matches ``agent_id``.

    Mutates status to ``claimed`` and records ``claimed_by``. Returns the item
    or ``None`` if no pending task is found.
    """
    async with _lock:
        for t in _task_queues[group_id]:
            if t["receiver_id"] == agent_id and t["status"] == "pending":
                t["status"] = "claimed"
                t["claimed_by"] = instance_id
                return t
    return None


async def complete_task(
    task_id: str,
    success: bool,
    result: str | None = None,
    result_data: dict | None = None,
) -> dict | None:
    """Mark a task completed/failed. Does NOT auto-push a notify (anti-double-notify).

    The caller (AgentEngine) is responsible for pushing the single agent_reply
    notify to the coordinator carrying the result. Returns the updated item or
    ``None`` if the task_id was not found.
    """
    async with _lock:
        for tasks in _task_queues.values():
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = "completed" if success else "failed"
                    t["result"] = result
                    t["result_data"] = result_data
                    t["completed_at"] = _now_iso()
                    return t
    return None


async def cancel_task(task_id: str) -> dict | None:
    """PL-11: mark a queued/pending task ``cancelled`` so the engine loop skips it.

    Mutates status to ``cancelled`` (records ``completed_at``). Returns the
    updated item, or ``None`` if the task_id was not found or was already
    terminal (``completed``/``failed``/``cancelled``).

    Complements ``AgentEngine.request_cancel``: that cancels the *currently
    executing* task by cancelling its child ``asyncio.Task``; this marks tasks
    that are still *queued* — sitting in the asyncio.Queue channel or the
    engine's ``_pending_tasks`` backlog — which ``request_cancel`` cannot reach
    because no ``_worker_task`` exists for them yet. When the engine loop later
    dequeues or drains such a marked task, it detects the ``cancelled`` status
    and skips execution (see ``AgentEngine._handle_inbox_item`` and
    ``_drain_pending``). The task item is the same dict object referenced by
    both ``_task_queues`` and the asyncio.Queue channel (``push_task`` appends
    then ``put``s the same ``item``), so the marker is visible at dequeue
    without a re-query.
    """
    async with _lock:
        for tasks in _task_queues.values():
            for t in tasks:
                if t["id"] == task_id:
                    if t["status"] in ("completed", "failed", "cancelled"):
                        return None
                    t["status"] = "cancelled"
                    t["completed_at"] = _now_iso()
                    return t
    return None
