"""
智能体 API 路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.agent import (
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    AgentDefinitionResponse,
    AgentInstanceResponse,
)
from app.api.schemas.common import OK
from app.core.database import get_db
from app.services import agent_service

router = APIRouter(prefix="/agents", tags=["智能体"])


# ── AgentDefinition CRUD ────────────────────────────────────────

@router.post("", response_model=AgentDefinitionResponse, status_code=201)
async def create_agent(body: AgentDefinitionCreate, db: AsyncSession = Depends(get_db)):
    obj = await agent_service.create_definition(db, body.model_dump(by_name=True))
    return obj


@router.get("", response_model=list[AgentDefinitionResponse])
async def list_agents(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    return await agent_service.list_definitions(db, skip, limit)


@router.get("/{agent_id}", response_model=AgentDefinitionResponse)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    obj = await agent_service.get_definition(db, agent_id)
    if not obj:
        raise HTTPException(404, "智能体不存在")
    return obj


@router.patch("/{agent_id}", response_model=AgentDefinitionResponse)
async def update_agent(agent_id: str, body: AgentDefinitionUpdate, db: AsyncSession = Depends(get_db)):
    data = {k: v for k, v in body.model_dump(by_name=True).items() if v is not None}
    obj = await agent_service.update_definition(db, agent_id, data)
    if not obj:
        raise HTTPException(404, "智能体不存在")
    return obj


@router.delete("/{agent_id}", response_model=OK)
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    ok = await agent_service.delete_definition(db, agent_id)
    if not ok:
        raise HTTPException(404, "智能体不存在")
    return OK()


# ── AgentInstance ────────────────────────────────────────────────

@router.get("/{agent_id}/instances", response_model=list[AgentInstanceResponse])
async def list_instances(agent_id: str, db: AsyncSession = Depends(get_db)):
    return await agent_service.list_instances_by_definition(db, agent_id)
