"""Message routes (M3: SQLite-backed via store.crud + mention routing + engine wake-up).

Routes map to frontend `messageApi`:
  GET    /api/messages?groupId=&limit=          → list_messages
  GET    /api/messages/by-task/{taskId}?limit=   → list_messages_by_task
  POST   /api/messages                           → send_message (body = MessageCreatePayload)
  DELETE /api/messages?groupId=                  → clear_messages_by_group

send_message persists the user message, pushes it over the WS bus, then calls
``route_user_message`` which @mention-routes to the target agent or, if no
mention, to the coordinator. The route pushes a notify onto the target
engine's asyncio.Queue inbox, waking its run loop.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from engine.mention import route_user_message
from events import emit_message_added
from models import Message, MessageCreatePayload
from store import crud

router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get("")
async def list_messages(groupId: str = Query(""), limit: int = Query(100)) -> list[Message]:
    return await crud.list_messages(groupId or None, limit)


@router.get("/by-task/{task_id}")
async def list_messages_by_task(task_id: str, limit: int = Query(100)) -> list[Message]:
    return await crud.list_messages_by_task(task_id, limit)


@router.post("")
async def send_message(payload: MessageCreatePayload) -> Message:
    msg = await crud.create_message(payload)
    # push the user message over the WS bus
    await emit_message_added(msg.model_dump())
    # route by @mention -> target agent, else -> coordinator (wakes engine inbox)
    await route_user_message(msg.group_id, msg.content or "")
    return msg


@router.delete("")
async def clear_messages_by_group(groupId: str = Query("")) -> bool:
    return await crud.clear_messages_by_group(groupId)
