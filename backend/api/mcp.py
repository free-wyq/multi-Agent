"""MCP 连接管理路由（PRD 3.4 MC-01~06, PL-07）.

路由映射：
  GET    /api/mcp                        → list_mcp_connections          (MC-01 浏览)
  GET    /api/mcp/{mcp_id}                → get_mcp_connection
  POST   /api/mcp                         → create_mcp_connection         (MC-02 添加连接)
  PUT    /api/mcp/{mcp_id}                → update_mcp_connection
  DELETE /api/mcp/{mcp_id}                → delete_mcp_connection         (MC-04 删除)
  POST   /api/mcp/{mcp_id}/enable         → set_mcp_enabled(id, True)     (MC-03 启用)
  POST   /api/mcp/{mcp_id}/disable        → set_mcp_enabled(id, False)    (MC-03 禁用)
  POST   /api/mcp/{mcp_id}/mount          → mount_mcp                     (MC-06 挂载到 Agent)
  POST   /api/mcp/{mcp_id}/unmount        → unmount_mcp
  GET    /api/mcp/{mcp_id}/tools          → list_mcp_tools 预览自省       (前端展示用)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from engine.mcp_manager import list_mcp_tools
from models import AgentDefinition, McpConnection, McpConnectionCreatePayload
from store import crud

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MountBody(BaseModel):
    """挂载/卸载请求体，与 skills.py 的 MountBody 一致（camelCase 参数）。"""
    agentId: str


@router.get("")
async def list_mcp_connections_route() -> list[McpConnection]:
    return await crud.list_mcp_connections()


@router.get("/{mcp_id}")
async def get_mcp_connection_route(mcp_id: str) -> McpConnection | None:
    return await crud.get_mcp_connection(mcp_id)


@router.post("")
async def create_mcp_connection_route(
    payload: McpConnectionCreatePayload,
) -> McpConnection:
    return await crud.create_mcp_connection(payload)


@router.put("/{mcp_id}")
async def update_mcp_connection_route(
    mcp_id: str, payload: McpConnectionCreatePayload
) -> McpConnection | None:
    return await crud.update_mcp_connection(mcp_id, payload)


@router.delete("/{mcp_id}")
async def delete_mcp_connection_route(mcp_id: str) -> bool:
    return await crud.delete_mcp_connection(mcp_id)


@router.post("/{mcp_id}/enable")
async def enable_mcp_connection_route(mcp_id: str) -> McpConnection | None:
    return await crud.set_mcp_enabled(mcp_id, True)


@router.post("/{mcp_id}/disable")
async def disable_mcp_connection_route(mcp_id: str) -> McpConnection | None:
    return await crud.set_mcp_enabled(mcp_id, False)


@router.post("/{mcp_id}/mount")
async def mount_mcp_route(mcp_id: str, body: MountBody) -> AgentDefinition | None:
    return await crud.mount_mcp(body.agentId, mcp_id)


@router.post("/{mcp_id}/unmount")
async def unmount_mcp_route(mcp_id: str, body: MountBody) -> AgentDefinition | None:
    return await crud.unmount_mcp(body.agentId, mcp_id)


@router.get("/{mcp_id}/tools")
async def list_mcp_connection_tools_route(mcp_id: str) -> list[dict[str, Any]]:
    """返回该 MCP 连接暴露的工具列表预览（自省，方便前端展示）。

    只加载 enabled 的连接；禁用的连接返回空列表。
    """
    return await list_mcp_tools([mcp_id])
