"""Agent CRUD routes (M2: SQLite-backed via store.crud).

Routes map 1:1 to the frontend `agentApi` in src/services/api.ts:
  GET    /api/agents         → list_agents
  GET    /api/agents/{id}     → get_agent
  POST   /api/agents          → create_agent   (body = AgentCreatePayload)
  PUT    /api/agents/{id}     → update_agent   (body = partial payload)
  DELETE /api/agents/{id}     → delete_agent
"""
from __future__ import annotations

from fastapi import APIRouter

from models import AgentCreatePayload, AgentDefinition
from store import crud

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents() -> list[AgentDefinition]:
    return await crud.list_agents()


@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> AgentDefinition | None:
    return await crud.get_agent(agent_id)


@router.post("")
async def create_agent(payload: AgentCreatePayload) -> AgentDefinition:
    return await crud.create_agent(payload)


@router.put("/{agent_id}")
async def update_agent(agent_id: str, payload: AgentCreatePayload) -> AgentDefinition | None:
    return await crud.update_agent(agent_id, payload)


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str) -> bool:
    return await crud.delete_agent(agent_id)
