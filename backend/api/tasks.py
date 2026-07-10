"""Task routes (M2: SQLite-backed via store.crud).

Routes map to frontend `taskApi`:
  GET    /api/tasks?groupId=...     → list_tasks
  GET    /api/tasks/ready?groupId=..→ task_ready
  GET    /api/tasks/{id}            → get_task
  POST   /api/tasks                 → create_task   (body = TaskCreatePayload)
  PUT    /api/tasks/{id}            → update_task   (body = partial)
  DELETE /api/tasks/{id}            → delete_task
"""
from __future__ import annotations

from fastapi import APIRouter, Query

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


@router.put("/{task_id}")
async def update_task(task_id: str, payload: TaskCreatePayload) -> Task | None:
    return await crud.update_task(task_id, payload)


@router.delete("/{task_id}")
async def delete_task(task_id: str) -> bool:
    return await crud.delete_task(task_id)
