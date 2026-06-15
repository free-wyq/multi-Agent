"""
AgentInstance 服务层补充
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_instance import AgentInstance


async def create_instance(db: AsyncSession, data: dict) -> AgentInstance:
    """创建 AgentInstance 记录"""
    obj = AgentInstance(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def get_instance(db: AsyncSession, inst_id: str) -> AgentInstance | None:
    """获取 AgentInstance"""
    return await db.get(AgentInstance, inst_id)


async def update_instance(db: AsyncSession, inst_id: str, data: dict) -> AgentInstance | None:
    """更新 AgentInstance"""
    obj = await db.get(AgentInstance, inst_id)
    if not obj:
        return None
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    await db.flush()
    await db.refresh(obj)
    return obj
