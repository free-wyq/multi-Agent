"""
群组服务层
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group
from app.models.group_member import GroupMember


async def create_group(db: AsyncSession, data: dict) -> Group:
    obj = Group(**data)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def get_group(db: AsyncSession, group_id: str) -> Group | None:
    return await db.get(Group, group_id)


async def list_groups(db: AsyncSession, skip: int = 0, limit: int = 50) -> list[Group]:
    result = await db.execute(select(Group).offset(skip).limit(limit).order_by(Group.created_at.desc()))
    return list(result.scalars().all())


async def update_group(db: AsyncSession, group_id: str, data: dict) -> Group | None:
    obj = await db.get(Group, group_id)
    if not obj:
        return None
    for k, v in data.items():
        if v is not None:
            setattr(obj, k, v)
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_group(db: AsyncSession, group_id: str) -> bool:
    obj = await db.get(Group, group_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


# ── GroupMember ──────────────────────────────────────────────────

async def add_member(db: AsyncSession, group_id: str, agent_id: str, alias: str | None = None) -> GroupMember:
    obj = GroupMember(group_id=group_id, agent_id=agent_id, alias=alias)
    db.add(obj)
    await db.flush()
    await db.refresh(obj)
    return obj


async def remove_member(db: AsyncSession, member_id: str) -> bool:
    obj = await db.get(GroupMember, member_id)
    if not obj:
        return False
    await db.delete(obj)
    await db.flush()
    return True


async def list_members(db: AsyncSession, group_id: str) -> list[GroupMember]:
    result = await db.execute(select(GroupMember).where(GroupMember.group_id == group_id))
    return list(result.scalars().all())


async def list_members_with_agent(db: AsyncSession, group_id: str) -> list[dict]:
    """查询群组成员，并 JOIN 智能体定义获取名称/角色"""
    from app.models.agent_definition import AgentDefinition
    result = await db.execute(
        select(GroupMember, AgentDefinition.name, AgentDefinition.role)
        .join(AgentDefinition, GroupMember.agent_id == AgentDefinition.id)
        .where(GroupMember.group_id == group_id)
    )
    members = []
    for member, name, role in result.all():
        members.append({
            "id": member.id,
            "group_id": member.group_id,
            "agent_id": member.agent_id,
            "alias": member.alias,
            "joined_at": member.joined_at,
            "agent_name": name,
            "agent_role": role,
        })
    return members
