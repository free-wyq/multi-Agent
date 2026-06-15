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
)
from app.api.schemas.common import OK
from app.core.database import get_db
from app.services import group_service

router = APIRouter(prefix="/groups", tags=["群组"])


# ── Group CRUD ──────────────────────────────────────────────────

@router.post("", response_model=GroupResponse, status_code=201)
async def create_group(body: GroupCreate, db: AsyncSession = Depends(get_db)):
    obj = await group_service.create_group(db, body.model_dump())
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
    return await group_service.add_member(db, group_id, body.agent_id, body.alias)


@router.get("/{group_id}/members", response_model=list[GroupMemberResponse])
async def list_members(group_id: str, db: AsyncSession = Depends(get_db)):
    return await group_service.list_members(db, group_id)


@router.delete("/{group_id}/members/{member_id}", response_model=OK)
async def remove_member(group_id: str, member_id: str, db: AsyncSession = Depends(get_db)):
    ok = await group_service.remove_member(db, member_id)
    if not ok:
        raise HTTPException(404, "成员不存在")
    return OK()
