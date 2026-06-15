"""
任务服务层
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task


async def create_task(db: AsyncSession, data: dict) -> Task:
    obj = Task(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def get_task(db: AsyncSession, task_id: str) -> Task | None:
    return await db.get(Task, task_id)


async def list_tasks_by_group(db: AsyncSession, group_id: str) -> list[Task]:
    result = await db.execute(
        select(Task).where(Task.group_id == group_id).order_by(Task.dag_order.nulls_last(), Task.created_at)
    )
    return list(result.scalars().all())


async def update_task(db: AsyncSession, task_id: str, data: dict) -> Task | None:
    obj = await db.get(Task, task_id)
    if not obj:
        return None
    now = datetime.now(timezone.utc)

    # 状态变更时更新时间戳
    new_status = data.get("status")
    if new_status == "working" and obj.status != "working":
        data["started_at"] = now
    elif new_status in ("completed", "failed", "canceled") and obj.status not in ("completed", "failed", "canceled"):
        data["completed_at"] = now

    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_task(db: AsyncSession, task_id: str) -> bool:
    obj = await db.get(Task, task_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


async def get_ready_tasks(db: AsyncSession, group_id: str) -> list[Task]:
    """获取依赖已满足、可以派发的任务（status=submitted 且所有 dependencies 已 completed）"""
    tasks = await list_tasks_by_group(db, group_id)
    completed_ids = {t.id for t in tasks if t.status == "completed"}
    return [
        t for t in tasks
        if t.status == "submitted" and all(dep in completed_ids for dep in t.dependencies)
    ]
