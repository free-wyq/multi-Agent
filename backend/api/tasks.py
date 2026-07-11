"""Task routes (M2: SQLite-backed via store.crud).

Routes map to frontend `taskApi`:
  GET    /api/tasks?groupId=...     → list_tasks
  GET    /api/tasks/ready?groupId=..→ task_ready
  GET    /api/tasks/{id}            → get_task
  POST   /api/tasks                 → create_task   (body = TaskCreatePayload)
  POST   /api/tasks/{id}/stop       → stop_task     (PL-11: cancel running task)
  PUT    /api/tasks/{id}            → update_task   (body = partial)
  DELETE /api/tasks/{id}            → delete_task
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from engine.inbox import cancel_task
from engine.registry import registry
from models import Task, TaskCreatePayload
from store import crud

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(groupId: str = Query("")) -> list[Task]:
    return await crud.list_tasks(groupId or None)


@router.get("/ready")
async def task_ready(groupId: str = Query("")) -> list[Task]:
    return await crud.list_ready_tasks(groupId or None)


@router.get("/{task_id}")
async def get_task(task_id: str) -> Task | None:
    return await crud.get_task(task_id)


@router.post("")
async def create_task(payload: TaskCreatePayload) -> Task:
    return await crud.create_task(payload)


@router.post("/{task_id}/stop")
async def stop_task(
    task_id: str,
    groupId: str | None = Query(
        None, description="optional group filter to narrow the engine scan"
    ),
) -> dict[str, Any]:
    """PL-11: stop a running task.

    A user-visible task id (``tq_`` runtime id, as dispatched by the
    coordinator) may be in one of two live states, both of which must be handled
    for a stop to be reliable:

    1. *currently executing* on some agent's engine — the child
       ``_worker_task`` is mid-LLM-call. ``registry.stop_task_by_id`` cancels
       it via ``request_cancel`` (next ``await`` raises ``CancelledError``;
       ``_handle_task`` absorbs it, runs ``_on_task_cancelled``, engine → idle).

    2. *queued / backlogged* — sitting in the asyncio.Queue channel or the
       engine's ``_pending_tasks`` list, not yet dequeued.
       ``inbox.cancel_task`` marks the item ``cancelled`` so ``_handle_task``'s
       front-door guard skips it without executing.

    Both are attempted unconditionally (neither throws if the task is in the
    other state), and the response reports each outcome. Returns ``200`` with
    ``executing``/``queued`` flags even when neither matched (task already
    finished/never existed) — a stop on a settled task is a no-op, not an error,
    so callers don't need to race the natural completion.

    ``groupId`` is optional: the runtime ``tq_`` ids are globally unique
    (uuid4 hex), so omitting it still matches at most one engine — convenient
    for the frontend, which may not always know which group a task belongs to.
    When the caller does know the group, passing it narrows the scan.
    """
    result = await registry.stop_task_by_id(task_id, groupId)
    # also mark queued copies (idempotent: returns None if already terminal)
    queued_item = await cancel_task(task_id)

    executing = result["cancelled"]
    queued = queued_item is not None

    return {
        "ok": True,
        "task_id": task_id,
        "executing": executing,
        "queued": queued,
        "group_id": result.get("group_id"),
        "agent_id": result.get("agent_id"),
        "message": (
            "任务已停止（执行中已中断）"
            if executing
            else (
                "任务已取消（队列中已标记跳过）"
                if queued
                else "任务不在执行/队列中（可能已完成）"
            )
        ),
    }


@router.put("/{task_id}")
async def update_task(task_id: str, payload: TaskCreatePayload) -> Task | None:
    return await crud.update_task(task_id, payload)


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> bool:
    return await crud.delete_task(task_id)
