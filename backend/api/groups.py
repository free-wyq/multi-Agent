"""Group + member + file routes (M2: SQLite-backed via store.crud).

Routes map to frontend `groupApi`:
  GET    /api/groups                          → list_groups
  GET    /api/groups/{id}                     → get_group
  POST   /api/groups                          → create_group     (body = GroupCreatePayload)
  PUT    /api/groups/{id}                     → update_group     (body = partial)
  DELETE /api/groups/{id}                     → delete_group
  GET    /api/groups/{groupId}/members        → group_list_members   (flat: +agent_name/role)
  POST   /api/groups/{groupId}/members        → group_add_member     (body = {agentId, alias?})
  DELETE /api/groups/{groupId}/members/{mid}  → group_remove_member
  GET    /api/groups/{groupId}/files          → group_list_files
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from models import Group, GroupCreatePayload, GroupFile, GroupMember
from store import crud

router = APIRouter(prefix="/api/groups", tags=["groups"])


class AddMemberBody(BaseModel):
    agentId: str
    alias: str | None = None


@router.get("")
async def list_groups() -> list[Group]:
    return await crud.list_groups()


@router.get("/{group_id}")
async def get_group(group_id: str) -> Group | None:
    return await crud.get_group(group_id)


@router.post("")
async def create_group(payload: GroupCreatePayload) -> Group:
    return await crud.create_group(payload)


@router.put("/{group_id}")
async def update_group(group_id: str, payload: GroupCreatePayload) -> Group | None:
    return await crud.update_group(group_id, payload)


@router.delete("/{group_id}")
async def delete_group(group_id: str) -> bool:
    return await crud.delete_group(group_id)


@router.get("/{group_id}/members")
async def list_members(group_id: str) -> list[GroupMember]:
    return await crud.list_group_members_with_agent(group_id)


@router.post("/{group_id}/members")
async def add_member(group_id: str, body: AddMemberBody) -> GroupMember | None:
    return await crud.add_member(group_id, body.agentId, body.alias)


@router.delete("/{group_id}/members/{member_id}")
async def remove_member(group_id: str, member_id: str) -> bool:
    return await crud.remove_member(group_id, member_id)


@router.get("/{group_id}/files")
async def list_files(group_id: str) -> list[GroupFile]:
    return await crud.list_files(group_id)
