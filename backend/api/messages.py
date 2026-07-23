"""Message routes (M3: SQLite-backed via store.crud + mention routing + engine wake-up).

Routes map to frontend `messageApi`:
  GET    /api/messages?conversationId=&limit=    → list_messages
  GET    /api/messages/by-task/{taskId}?limit=   → list_messages_by_task
  POST   /api/messages                           → send_message (body = MessageCreatePayload)
  DELETE /api/messages?conversationId=           → clear_messages_by_group

send_message persists the user message, pushes it over the WS bus, then calls
the routing layer. For group-chat conversations (conversation_id is a group_id)
this is ``route_user_message`` (group graph + @mention routing). For
single-chat conversations (conversation_id is a conversation_id) this is
``route_direct_message`` (resident worker engine, no group graph) — see
``engine/direct.py``.

Path C strict rename: ``group_id`` → ``conversation_id`` on Message + payload.
The ``conversationId`` query param is used by the GET/DELETE endpoints
(holds either a group_id or a conversation_id).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from engine.direct import route_direct_message
from engine.mention import route_user_message
from events import emit_message_added
from models import Message, MessageCreatePayload
from store import crud

router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get("")
async def list_messages(conversationId: str = Query(""), limit: int = Query(100)) -> list[Message]:
    return await crud.list_messages(conversationId or None, limit)


@router.get("/by-task/{task_id}")
async def list_messages_by_task(task_id: str, limit: int = Query(100)) -> list[Message]:
    return await crud.list_messages_by_task(task_id, limit)


@router.post("")
async def send_message(payload: MessageCreatePayload) -> Message:
    msg = await crud.create_message(payload)
    # push the user message over the WS bus
    await emit_message_added(msg.model_dump())
    # Route by conversation kind. If the conversation_id matches a Group row →
    # group-chat path (route_user_message + group graph + @mention routing).
    # Otherwise → single-chat path (route_direct_message → resident worker
    # engine, no group graph). Path C: the single-chat bypass that used to
    # live in route_user_message (mention.py:298-305) now lives as
    # route_direct_message in engine/direct.py (mention.py was 410 lines).
    group = await crud.get_group(msg.conversation_id)
    converge = bool(getattr(payload, "converge", False))
    try:
        if group is not None:
            await route_user_message(msg.conversation_id, msg.content or "", converge=converge)
        else:
            await route_direct_message(msg.conversation_id, msg.content or "")
    except ValueError as e:
        # 收束必须 @ 收口对象 — the toggle was on but the message had no @人.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return msg


@router.delete("")
async def clear_messages_by_group(conversationId: str = Query("")) -> bool:
    return await crud.clear_messages_by_group(conversationId)
