"""Conversation routes (Path C single-chat entity split).

Routes map to frontend `conversationApi`:
  GET    /api/conversations                  → list_conversations
  POST   /api/conversations                  → create_conversation (find-or-create)
  GET    /api/conversations/{id}             → get_conversation
  DELETE /api/conversations/{id}             → delete_conversation

The single-chat conversation is its own entity (``ConversationEntity``) — no
longer a degenerate ``GroupEntity`` row with ``config.single_chat=True``.
Messages and tasks reference the conversation via ``conversation_id`` (the
renamed ``group_id``). The WS channel ``bus-event:{conversationId}`` reuses
the same BusManager (one id one channel, no protocol change).
"""
from __future__ import annotations

from fastapi import APIRouter

from models import Conversation, ConversationCreatePayload
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
