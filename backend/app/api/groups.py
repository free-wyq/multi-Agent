"""
群组 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.group import (
    GroupCreate,
    GroupUpdate,
    GroupResponse,
    GroupMemberAdd,
    GroupMemberResponse,
    GroupMemberWithAgentResponse,
    GroupFileResponse,
)
from app.api.schemas.common import OK
from app.core.database import get_db
from app.services import group_service

router = APIRouter(prefix="/groups", tags=["群组"])


# ── Group CRUD ──────────────────────────────────────────────────

@router.post("", response_model=GroupResponse, status_code=201)
async def create_group(body: GroupCreate, db: AsyncSession = Depends(get_db)):
    obj = await group_service.create_group(db, body.model_dump(exclude={"member_ids"}))

    # 如果传了 member_ids，自动添加成员 + 注册 AgentEngine
    if body.member_ids:
        # 群主不能同时在成员列表中
        if body.coordinator_id in body.member_ids:
            raise HTTPException(400, "群主不能同时作为成员，群主自动在群内")

        from app.agent_engine import get_registry
        from app.services import agent_service
        registry = get_registry()
        for agent_id in body.member_ids:
            await group_service.add_member(db, obj.id, agent_id)
            agent_def = await agent_service.get_definition(db, agent_id)
            if agent_def:
                await registry.add_engine(agent_def, obj.id)
        await db.flush()

    return obj


@router.get("", response_model=list[GroupResponse])
async def list_groups(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    return await group_service.list_groups(db, skip, limit)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(group_id: str, db: AsyncSession = Depends(get_db)):
    obj = await group_service.get_group(db, group_id)
    if not obj:
        raise HTTPException(404, "群组不存在")
    return obj


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group(group_id: str, body: GroupUpdate, db: AsyncSession = Depends(get_db)):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    obj = await group_service.update_group(db, group_id, data)
    if not obj:
        raise HTTPException(404, "群组不存在")
    return obj


@router.delete("/{group_id}", response_model=OK)
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)):
    ok = await group_service.delete_group(db, group_id)
    if not ok:
        raise HTTPException(404, "群组不存在")
    return OK()


# ── GroupMember ─────────────────────────────────────────────────

@router.post("/{group_id}/members", response_model=GroupMemberResponse, status_code=201)
async def add_member(group_id: str, body: GroupMemberAdd, db: AsyncSession = Depends(get_db)):
    group = await group_service.get_group(db, group_id)
    if not group:
        raise HTTPException(404, "群组不存在")
    member = await group_service.add_member(db, group_id, body.agent_id, body.alias)

    # 同步注册 AgentEngine 常驻协程
    from app.agent_engine import get_registry
    from app.services import agent_service
    registry = get_registry()
    agent_def = await agent_service.get_definition(db, body.agent_id)
    if agent_def:
        await registry.add_engine(agent_def, group_id)

    return member


@router.get("/{group_id}/members", response_model=list[GroupMemberWithAgentResponse])
async def list_members(group_id: str, db: AsyncSession = Depends(get_db)):
    return await group_service.list_members_with_agent(db, group_id)


@router.delete("/{group_id}/members/{member_id}", response_model=OK)
async def remove_member(group_id: str, member_id: str, db: AsyncSession = Depends(get_db)):
    member = await db.get(__import__('app.models.group_member', fromlist=['GroupMember']).GroupMember, member_id)
    if not member:
        raise HTTPException(404, "成员不存在")

    agent_id = member.agent_id
    ok = await group_service.remove_member(db, member_id)

    # 同步停止 AgentEngine
    from app.agent_engine import get_registry
    registry = get_registry()
    engine = registry.get_engine(agent_id, group_id)
    if engine:
        await registry.remove_engine(agent_id, group_id)

    return OK()


# ── GroupFile (群共享文件) ───────────────────────────────────────

@router.get("/{group_id}/files", response_model=list[GroupFileResponse])
async def list_group_files(group_id: str):
    """列出群共享根目录下的所有文件（人类只读）"""
    from app.services.group_file_service import list_files
    files = list_files(group_id)
    return files


@router.post("/{group_id}/files/ensure_dir")
async def ensure_group_dir(group_id: str):
    """确保群共享目录存在（内部调用）"""
    from app.services.group_file_service import ensure_group_dir as ensure_dir
    ensure_dir(group_id)
    return OK()
