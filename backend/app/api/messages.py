"""
消息 API 路由
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.message import MessageCreate, MessageResponse
from app.core.database import get_db
from app.services import message_service

router = APIRouter(prefix="/messages", tags=["消息"])


@router.post("", response_model=MessageResponse, status_code=201)
async def create_message(body: MessageCreate, db: AsyncSession = Depends(get_db)):
    obj = await message_service.create_message(db, body.model_dump())
    return obj


@router.get("/group/{group_id}", response_model=list[MessageResponse])
async def list_group_messages(group_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)):
    return await message_service.list_messages_by_group(db, group_id, limit)


@router.get("/task/{task_id}", response_model=list[MessageResponse])
async def list_task_messages(task_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)):
    return await message_service.list_messages_by_task(db, task_id, limit)
