"""
消息服务层
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


async def create_message(db: AsyncSession, data: dict) -> Message:
    obj = Message(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def list_messages_by_group(db: AsyncSession, group_id: str, limit: int = 100) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.group_id == group_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_messages_by_task(db: AsyncSession, task_id: str, limit: int = 100) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.task_id == task_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def clear_messages_by_group(db: AsyncSession, group_id: str) -> int:
    """清空群组的所有聊天记录，返回删除条数"""
    from sqlalchemy import delete
    result = await db.execute(
        delete(Message).where(Message.group_id == group_id)
    )
    return result.rowcount or 0
