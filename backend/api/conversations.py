"""Conversation routes (Path C single-chat entity split, T86 豆包式常驻助手).

Routes map to frontend `conversationApi`:
  GET    /api/conversations                  → list_conversations (transient=0 only)
  POST   /api/conversations                  → create_conversation (每次新建)
  GET    /api/conversations/{id}             → get_conversation
  DELETE /api/conversations/{id}             → delete_conversation
  POST   /api/conversations/{id}/finalize    → finalize_conversation (体验→正式)
  GET    /api/conversations/{id}/files      → list_files        (任务14a·单聊文件列表)
  GET    /api/conversations/{id}/files/{name} → download_file  (任务14a·单聊产物下载)

T86 改语义：``POST /api/conversations`` 不再 find-or-create——每次都新建一个 row。
- 不传 ``agent_id`` → 绑平台常驻助手（slug='platform_assistant'），transient=0 进侧栏。
- 传 ``agent_id`` → 体验会话，transient=1 不进侧栏（智能体广场点 agent 场景）。
- ``finalize`` 端点把体验会话转正（transient=0），让用户可保留感兴趣的体验对话。

任务14a：单聊文件操作走 ``/api/conversations`` 命名空间，不滥用 group 路由。工作区按
``conversation_id`` 在磁盘落盘（``DATA_DIR/workspaces/{conversation_id}/``）——单聊驻留
worker engine 构造时 ``group_id=conversation_id``（见 ``registry.ensure_engine`` +
``direct.route_direct_message``），其 ``file_write`` 工具即落此 key 下。``crud.list_files``
是 key 无关的纯 ``workspace_path(key)`` 查找（无 group-entity 依赖），群聊 group_id 与
单聊 conversation_id 同一代码路径服务。
"""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from engine.workspace import safe_path
from models import Conversation, ConversationCreatePayload, GroupFile
from store import crud

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("")
async def list_conversations() -> list[Conversation]:
    """List non-transient single-chat conversations (T86 豆包式).

    体验会话（transient=1）不进侧栏——``crud.list_conversations`` 已过滤。
    """
    return await crud.list_conversations()


@router.post("")
async def create_conversation(payload: ConversationCreatePayload) -> Conversation:
    """Create a new single-chat conversation (T86 改语义：每次新建).

    - 不传 ``agent_id`` → 绑平台常驻助手（slug='platform_assistant'）。
    - 传 ``agent_id`` → 体验会话，caller 应同时传 ``transient=1``。

    平台助手未种时返 503（启动期 ``ensure_platform_assistant`` 应已执行）。
    """
    try:
        return await crud.create_conversation(payload)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str) -> Conversation | None:
    return await crud.get_conversation(conversation_id)


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str) -> bool:
    return await crud.delete_conversation(conversation_id)


@router.post("/{conversation_id}/finalize")
async def finalize_conversation(conversation_id: str) -> Conversation:
    """T86 转正端点：体验会话（transient=1）→ 正式（transient=0）。

    智能体广场点 agent 的体验对话用户想保留时，调此端点转正——之后会出现在侧栏
    【会话】列。语义幂等（已是正式仍返当前行）。404 当会话不存在。
    """
    conv = await crud.finalize_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=404,
            detail=f"会话不存在: {conversation_id}",
        )
    return conv


@router.get("/{conversation_id}/files")
async def list_files(conversation_id: str) -> list[GroupFile]:
    """List artifact files in a single-chat conversation's workspace.

    Mirrors ``GET /api/groups/{groupId}/files`` but keyed by
    ``conversation_id``. The single-chat resident worker engine writes its
    ``file_write`` output to ``DATA_DIR/workspaces/{conversation_id}/``
    (the engine is constructed with ``group_id=conversation_id``, so the
    inherited workspace binding applies unchanged). ``crud.list_files`` is
    a key-agnostic ``workspace_path(key)`` listing with no group-entity
    dependency — the same code path serves group ``group_id`` and single-chat
    ``conversation_id``.

    Returns ``[]`` when the workspace directory does not exist yet (no task
    has produced artifacts for this conversation).
    """
    return await crud.list_files(conversation_id)


@router.get("/{conversation_id}/files/{file_name:path}")
async def download_file(conversation_id: str, file_name: str) -> FileResponse:
    """Download a single-chat conversation workspace artifact by name.

    Mirrors the group ``GET /api/groups/{groupId}/files/{name}`` PL-12
    download: ``file_name`` is resolved against the conversation workspace via
    ``safe_path`` (path-traversal attempts like ``../../etc/passwd`` are
    rejected before any file is opened). 404 when the file does not exist;
    MIME guessed from extension (default ``application/octet-stream``).

    The ``{file_name:path}`` converter captures the rest of the URL including
    slashes, so a sub-directory artifact (e.g. ``out/result.txt``) recorded
    by ``scan_workspace_artifacts`` as a POSIX-relative ``path`` is delivered
    as one parameter.
    """
    rel = file_name
    # strip a leading slash so "/sub/f.md" (defensive client join) works
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    try:
        target = safe_path(conversation_id, rel)
    except ValueError as exc:
        # path escaped the workspace root — refuse rather than serving anything
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"file not found: {file_name}",
        )

    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
    )
