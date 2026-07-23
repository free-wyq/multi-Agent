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

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from engine.mcp_manager import list_mcp_tools
from models import AgentDefinition, McpConnection, McpConnectionCreatePayload
from store import crud

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


# ── stdio 命令白名单 + shell 元字符拒绝（任务1，借鉴 deer-flow MCP router） ──────
# 仅允许裸可执行名（单用户桌面应用，无需 admin/OAuth）。扩展方式：往 frozenset 加名字。
DEFAULT_STDIO_COMMAND_WHITELIST: frozenset[str] = frozenset(
    {"npx", "uvx", "python", "node", "uv"}
)

# shell 元字符拒绝集：命中任一即拒（防注入）。
_SHELL_METACHARS: frozenset[str] = frozenset(";|&`$<>\n\r")


def _validate_stdio_command(command: str) -> None:
    """校验 stdio command 是裸可执行名且在白名单内，否则 raise HTTPException(400)。

    规则（借鉴 deer-flow MCP router）：
    1. 非空（stdio transport 必须有 command）；
    2. 无路径分隔符 ``/``（防 ``/bin/sh`` / ``./evil``）；
    3. 无空格（防 ``npx;rm -rf`` 式注入 / ``npx foo`` 实际是两段）；
    4. 无 shell 元字符（``;|&\\`$<>\\n\\r``）；
    5. 在白名单内（默认 {npx,uvx,python,node,uv}）。
    """
    if not command:
        raise HTTPException(status_code=400, detail="stdio 连接的 command 不能为空")
    if "/" in command:
        raise HTTPException(
            status_code=400,
            detail=f"stdio command 含非法字符（路径分隔符/空格/shell 元字符），拒绝落库：{command!r}",
        )
    if " " in command:
        raise HTTPException(
            status_code=400,
            detail=f"stdio command 含非法字符（路径分隔符/空格/shell 元字符），拒绝落库：{command!r}",
        )
    for ch in command:
        if ch in _SHELL_METACHARS:
            raise HTTPException(
                status_code=400,
                detail=f"stdio command 含非法字符（路径分隔符/空格/shell 元字符），拒绝落库：{command!r}",
            )
    if command not in DEFAULT_STDIO_COMMAND_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail=f"stdio command {command!r} 不在允许的白名单 {sorted(DEFAULT_STDIO_COMMAND_WHITELIST)} 内，拒绝落库",
        )


def _validate_mcp_payload(payload: McpConnectionCreatePayload) -> None:
    """按 transport 分流校验。stdio 走 command 校验；sse 不受影响。"""
    if (payload.transport or "stdio") == "stdio":
        _validate_stdio_command(payload.command or "")


# ── 敏感字段脱敏（任务2，env/headers） ─────────────────────────────────────────
# 敏感 key 模式（deer-flow 正则，大小写不敏感）。命中任一子串即把整个 value 替换为 "***"。
_SENSITIVE_KEY_RE = re.compile(
    r"api_key|apikey|access_key|private_key|client_secret|secret|token|password|credential|authorization|bearer",
    re.IGNORECASE,
)

_MASK = "***"


def _mask_sensitive(d: dict | None) -> dict | None:
    """返回 env/headers 的脱敏副本：敏感 key 的 value → "***"，其余原值。"""
    if not d:
        return d
    return {k: (_MASK if _SENSITIVE_KEY_RE.search(k) else v) for k, v in d.items()}


def _apply_mcp_mask(conn: McpConnection) -> McpConnection:
    """对单条 McpConnection 的 env/headers 脱敏（返回新对象，不改原）。"""
    return conn.model_copy(
        update={"env": _mask_sensitive(conn.env), "headers": _mask_sensitive(conn.headers)}
    )


def _merge_one_dict(existing: dict | None, incoming: dict | None) -> dict | None:
    """PUT 时合并：incoming 中值为 "***" 的 key 用 existing 的原值替换；其余用 incoming 值。

    - incoming=None → 视为不更新（返回 None 保持 payload 语义，与 model_dump exclude_unset 一致）；
    - existing=None → 视为空 dict；
    - incoming 中 "***" 且库里无此 key → 原样落 "***"（无原值可保留，兜底）。
    """
    if incoming is None:
        return None
    if existing is None:
        existing = {}
    merged = dict(incoming)
    for k, v in merged.items():
        if v == _MASK:
            if k in existing:
                merged[k] = existing[k]
            # 库里没这个 key —— "***" 无原值可保留，原样落库（注释说明）
    return merged


async def _merge_masked_fields(
    mcp_id: str, payload: McpConnectionCreatePayload
) -> McpConnectionCreatePayload:
    """PUT 时若 env/headers 某值是 "***"，用库中原值替换（保留原值语义）。

    仅处理字面 "***"（deer-flow 语义）。其他值正常覆盖。env/headers 均未传时直接返回原 payload。
    """
    # model_dump(exclude_unset=True) 看是否显式传了 env/headers（没传则不合并）
    dumped = payload.model_dump(exclude_unset=True)
    if "env" not in dumped and "headers" not in dumped:
        return payload
    existing = await crud.get_mcp_connection(mcp_id)
    if not existing:
        return payload  # 连接不存在，让 update 路径返回 None
    new_env = _merge_one_dict(existing.env, payload.env)
    new_headers = _merge_one_dict(existing.headers, payload.headers)
    return payload.model_copy(update={"env": new_env, "headers": new_headers})


class MountBody(BaseModel):
    """挂载/卸载请求体，与 skills.py 的 MountBody 一致（camelCase 参数）。"""
    agentId: str


@router.get("")
async def list_mcp_connections_route() -> list[McpConnection]:
    conns = await crud.list_mcp_connections()
    return [_apply_mcp_mask(c) for c in conns]


@router.get("/{mcp_id}")
async def get_mcp_connection_route(mcp_id: str) -> McpConnection | None:
    conn = await crud.get_mcp_connection(mcp_id)
    return _apply_mcp_mask(conn) if conn else None


@router.post("")
async def create_mcp_connection_route(
    payload: McpConnectionCreatePayload,
) -> McpConnection:
    _validate_mcp_payload(payload)
    # create 时 "***" 不特殊处理（原样落库，GET 再脱敏；create 表单不应发 "***"）
    return await crud.create_mcp_connection(payload)


@router.put("/{mcp_id}")
async def update_mcp_connection_route(
    mcp_id: str, payload: McpConnectionCreatePayload
) -> McpConnection | None:
    _validate_mcp_payload(payload)
    payload = await _merge_masked_fields(mcp_id, payload)
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
