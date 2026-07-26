"""Conversation routes (Path C single-chat entity split).

Routes map to frontend `conversationApi`:
  GET    /api/conversations                  → list_conversations
  POST   /api/conversations                  → create_conversation (find-or-create)
  GET    /api/conversations/{id}             → get_conversation
  DELETE /api/conversations/{id}             → delete_conversation
  GET    /api/conversations/{id}/files      → list_files        (任务14a·单聊文件列表)
  GET    /api/conversations/{id}/files/{name} → download_file  (任务14a·单聊产物下载)

The single-chat conversation is its own entity (``ConversationEntity``) — no
longer a degenerate ``GroupEntity`` row with ``config.single_chat=True``.
Messages and tasks reference the conversation via ``conversation_id`` (the
renamed ``group_id``). The WS channel ``bus-event:{conversationId}`` reuses
the same BusManager (one id one channel, no protocol change).

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
    return await crud.list_conversations()


@router.post("")
async def create_conversation(payload: ConversationCreatePayload) -> Conversation:
    """Find-or-create a single-chat conversation for ``agent_id``.

    Idempotent: returns the existing conversation for the agent if one exists,
    otherwise creates a new one. Used by the frontend ``selectAgent`` path
    (clicking an agent in the sidebar opens a single-chat with that agent).
    """
    return await crud.get_or_create_conversation(payload.agent_id)


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str) -> Conversation | None:
    return await crud.get_conversation(conversation_id)


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str) -> bool:
    return await crud.delete_conversation(conversation_id)


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
