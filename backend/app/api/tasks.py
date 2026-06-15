"""
任务 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from app.api.schemas.common import OK
from app.core.database import get_db
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["任务"])


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    obj = await task_service.create_task(db, body.model_dump())
    return obj


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    obj = await task_service.get_task(db, task_id)
    if not obj:
        raise HTTPException(404, "任务不存在")
    return obj


@router.get("", response_model=list[TaskResponse])
async def list_tasks(group_id: str, db: AsyncSession = Depends(get_db)):
    return await task_service.list_tasks_by_group(db, group_id)


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, body: TaskUpdate, db: AsyncSession = Depends(get_db)):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    obj = await task_service.update_task(db, task_id, data)
    if not obj:
        raise HTTPException(404, "任务不存在")
    return obj


@router.delete("/{task_id}", response_model=OK)
async def delete_task(task_id: str, db: AsyncSession = Depends(get_db)):
    ok = await task_service.delete_task(db, task_id)
    if not ok:
        raise HTTPException(404, "任务不存在")
    return OK()


@router.get("/group/{group_id}/ready", response_model=list[TaskResponse])
async def get_ready_tasks(group_id: str, db: AsyncSession = Depends(get_db)):
    """获取依赖已满足、可派发的任务"""
    return await task_service.get_ready_tasks(db, group_id)
