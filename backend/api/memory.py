"""记忆模块路由（PRD 记忆模块 · 任务17b · L2 长期记忆层）.

路由映射：
  GET    /api/memory                        → list_memories        (浏览/筛选)
  GET    /api/memory/{memory_id}             → get_memory
  POST   /api/memory                         → create_memory        (手动新增)
  PUT    /api/memory/{memory_id}             → update_memory         (编辑)
  DELETE /api/memory/{memory_id}             → delete_memory        (删除)
  POST   /api/memory/{memory_id}/enable      → set_memory_enabled(True)
  POST   /api/memory/{memory_id}/disable     → set_memory_enabled(False) (软删除)
  POST   /api/memory/search                  → search_memories      (FTS5 全文检索)

设计文档：docs/memory-module-design.md（分层模型 / MemoryEntity schema /
生命周期四阶段 / 检索方案 / prompt 注入落点）。本端点是 L2 长期记忆层的
CRUD + 检索；L1 会话上下文（_memory / _build_context_from_db）不在此处。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from models import (
    Memory,
    MemoryCreatePayload,
    MemorySearchResponse,
    MemorySearchResult,
    MemoryUpdatePayload,
)
from store import crud

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("")
async def list_memories_route(
    user_id: str | None = Query(default=None, description="按 user_id 过滤；None=全部。"),
    scope: str | None = Query(
        default=None,
        description="按 scope 过滤：global / agent / conversation。None=全部。",
    ),
    scope_ref: str | None = Query(
        default=None,
        description="按 scope_ref 过滤（agent_id / conversation_id）。None=全部。",
    ),
    enabled: bool | None = Query(
        default=None,
        description="按 enabled 过滤：True=仅启用 / False=仅禁用 / None=全部。",
    ),
    keyword: str | None = Query(
        default=None,
        description="关键字 LIKE 过滤（管理 UI 文本框，非 FTS5 排序检索）。",
    ),
    limit: int = Query(default=200, ge=1, le=2000, description="返回上限。"),
) -> list[Memory]:
    """List memories, optionally filtered. Ordered by importance DESC."""
    return await crud.list_memories(
        user_id=user_id,
        scope=scope,
        scope_ref=scope_ref,
        enabled=enabled,
        keyword=keyword,
        limit=limit,
    )


@router.get("/{memory_id}")
async def get_memory_route(memory_id: str) -> Memory | None:
    return await crud.get_memory(memory_id)


@router.post("")
async def create_memory_route(payload: MemoryCreatePayload) -> Memory:
    if not (payload.content or "").strip():
        raise HTTPException(status_code=400, detail="content 不能为空")
    if payload.scope not in ("global", "agent", "conversation"):
        raise HTTPException(
            status_code=400,
            detail=f"scope 必须是 global/agent/conversation，收到 {payload.scope!r}",
        )
    # scope=agent/conversation 必须带 scope_ref（指向 agent_id / conversation_id）。
    if payload.scope in ("agent", "conversation") and not (payload.scope_ref or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"scope={payload.scope} 必须提供 scope_ref",
        )
    return await crud.create_memory(payload)


@router.put("/{memory_id}")
async def update_memory_route(
    memory_id: str, payload: MemoryUpdatePayload
) -> Memory | None:
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "scope" in data and data["scope"] not in ("global", "agent", "conversation"):
        raise HTTPException(
            status_code=400,
            detail=f"scope 必须是 global/agent/conversation，收到 {data['scope']!r}",
        )
    if (
        "scope" in data
        and data["scope"] in ("agent", "conversation")
        and not (data.get("scope_ref") or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail=f"scope={data['scope']} 必须提供 scope_ref",
        )
    return await crud.update_memory(memory_id, payload)


@router.delete("/{memory_id}")
async def delete_memory_route(memory_id: str) -> bool:
    return await crud.delete_memory(memory_id)


@router.post("/{memory_id}/enable")
async def enable_memory_route(memory_id: str) -> Memory | None:
    return await crud.set_memory_enabled(memory_id, True)


@router.post("/{memory_id}/disable")
async def disable_memory_route(memory_id: str) -> Memory | None:
    return await crud.set_memory_enabled(memory_id, False)


@router.post("/search")
async def search_memories_route(payload: dict) -> MemorySearchResponse:
    """FTS5 全文检索：按 ``query`` 检索 enabled 记忆，返回 top_k（按 bm25+importance 排序）.

    Body: ``{"query": str, "scope": str?, "scope_ref": str?, "user_id": str?, "top_k": int?}``.
    ``top_k`` 默认 5，上限 50（防止灌爆——设计 §6.3 L2 段总长截断 ~500 字符）。
    """
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    top_k = int(payload.get("top_k", 5))
    if top_k < 1:
        top_k = 5
    if top_k > 50:
        top_k = 50
    hits = await crud.search_memories(
        query,
        user_id=payload.get("user_id"),
        scope=payload.get("scope"),
        scope_ref=payload.get("scope_ref"),
        top_k=top_k,
    )
    return MemorySearchResponse(
        query=query,
        top_k=top_k,
        results=[
            MemorySearchResult(memory=mem, score=score) for mem, score in hits
        ],
    )
