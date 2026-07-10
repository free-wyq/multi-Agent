"""Scheduled task routes (M8: PRD 3.5 定时任务).

Routes map to the frontend ``scheduledTaskApi``:
  GET    /api/scheduled-tasks                        → list            (TM-01)
  GET    /api/scheduled-tasks/{id}                    → get
  POST   /api/scheduled-tasks                        → create          (TM-02/03)
  PUT    /api/scheduled-tasks/{id}                    → update
  DELETE /api/scheduled-tasks/{id}                    → delete          (TM-06)
  POST   /api/scheduled-tasks/{id}/run               → run_now         (TM-04 立即执行)
  POST   /api/scheduled-tasks/{id}/pause              → set_enabled(False)  (TM-05)
  POST   /api/scheduled-tasks/{id}/resume             → set_enabled(True)   (TM-05)
  GET    /api/scheduled-tasks/{id}/runs               → history         (TM-07)
"""
from __future__ import annotations

from fastapi import APIRouter

from engine.scheduler import add_job, remove_job
from models import ScheduledTask, ScheduledTaskCreatePayload, ScheduledTaskRun
from store import crud

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])


@router.get("")
async def list_scheduled_tasks() -> list[ScheduledTask]:
    return await crud.list_scheduled_tasks()


@router.get("/{task_id}")
async def get_scheduled_task(task_id: str) -> ScheduledTask | None:
    return await crud.get_scheduled_task(task_id)


@router.post("")
async def create_scheduled_task(payload: ScheduledTaskCreatePayload) -> ScheduledTask:
    task = await crud.create_scheduled_task(payload)
    if task.enabled:
        add_job(task.model_dump())
    return task


@router.put("/{task_id}")
async def update_scheduled_task(task_id: str, payload: ScheduledTaskCreatePayload) -> ScheduledTask | None:
    task = await crud.update_scheduled_task(task_id, payload)
    if task:
        # rebuild the job (replace_existing handles the swap)
        remove_job(task_id)
        if task.enabled:
            add_job(task.model_dump())
    return task


@router.delete("/{task_id}")
async def delete_scheduled_task(task_id: str) -> bool:
    remove_job(task_id)
    return await crud.delete_scheduled_task(task_id)


@router.post("/{task_id}/run")
async def run_now(task_id: str) -> dict:
    """TM-04: fire the task immediately, out of schedule (force, even if paused)."""
    from engine.scheduler import _fire

    await _fire(task_id, force=True)
    return {"ok": True}


@router.post("/{task_id}/pause")
async def pause(task_id: str) -> ScheduledTask | None:
    """TM-05: disable (remove the scheduled job)."""
    task = await crud.set_scheduled_task_enabled(task_id, False)
    if task:
        remove_job(task_id)
    return task


@router.post("/{task_id}/resume")
async def resume(task_id: str) -> ScheduledTask | None:
    """TM-05: enable (re-register the job)."""
    task = await crud.set_scheduled_task_enabled(task_id, True)
    if task:
        add_job(task.model_dump())
    return task


@router.get("/{task_id}/runs")
async def list_runs(task_id: str) -> list[ScheduledTaskRun]:
    """TM-07: execution history for a scheduled task."""
    return await crud.list_scheduled_task_runs(task_id)
