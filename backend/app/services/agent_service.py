"""
智能体服务层
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_definition import AgentDefinition
from app.models.agent_instance import AgentInstance


# ── AgentDefinition ──────────────────────────────────────────────

async def create_definition(db: AsyncSession, data: dict) -> AgentDefinition:
    obj = AgentDefinition(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def get_definition(db: AsyncSession, def_id: str) -> AgentDefinition | None:
    return await db.get(AgentDefinition, def_id)


async def list_definitions(db: AsyncSession, skip: int = 0, limit: int = 50) -> list[AgentDefinition]:
    result = await db.execute(select(AgentDefinition).offset(skip).limit(limit).order_by(AgentDefinition.created_at.desc()))
    return list(result.scalars().all())


async def update_definition(db: AsyncSession, def_id: str, data: dict) -> AgentDefinition | None:
    obj = await db.get(AgentDefinition, def_id)
    if not obj:
        return None
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_definition(db: AsyncSession, def_id: str) -> bool:
    obj = await db.get(AgentDefinition, def_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ── AgentInstance ────────────────────────────────────────────────

async def create_instance(db: AsyncSession, data: dict) -> AgentInstance:
    obj = AgentInstance(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def get_instance(db: AsyncSession, inst_id: str) -> AgentInstance | None:
    return await db.get(AgentInstance, inst_id)


async def list_instances_by_definition(db: AsyncSession, def_id: str) -> list[AgentInstance]:
    result = await db.execute(select(AgentInstance).where(AgentInstance.definition_id == def_id))
    return list(result.scalars().all())


async def update_instance(db: AsyncSession, inst_id: str, data: dict) -> AgentInstance | None:
    obj = await db.get(AgentInstance, inst_id)
    if not obj:
        return None
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    await db.flush()
    await db.refresh(obj)
    return obj
