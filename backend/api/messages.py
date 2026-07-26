"""Message routes (M3: SQLite-backed via store.crud + mention routing + engine wake-up).

Routes map to frontend `messageApi`:
  GET    /api/messages?conversationId=&limit=    → list_messages
  GET    /api/messages/by-task/{taskId}?limit=   → list_messages_by_task
  POST   /api/messages                           → send_message (body = MessageCreatePayload)
  POST   /api/messages/regenerate?replyId=        → regenerate_reply (按 reply_id 重跑)
  DELETE /api/messages?conversationId=             → clear_messages_by_group

send_message persists the user message, pushes it over the WS bus, then calls
the routing layer. For group-chat conversations (conversation_id is a group_id)
this is ``route_user_message`` (group graph + @mention routing). For
single-chat conversations (conversation_id is a conversation_id) this is
``route_direct_message`` (resident worker engine, no group graph) — see
``engine/direct.py``.

Path C strict rename: ``group_id`` → ``conversation_id`` on Message + payload.
The ``conversationId`` query param is used by the GET/DELETE endpoints
(holds either a group_id or a conversation_id).

[需求2-后端] regenerate_reply：按 ``reply_id`` 重跑一次回复。回查 chat 路径落盘的
agent_reply（其 ``data.reply_id`` 由 persist_agent_reply 透传），取出原始用户输入
（同会话该回复前最近一条 user_input），合成一条新 user_input 落盘 + emit，再走与
send_message 相同的路由分流（群聊 route_user_message / 单聊 route_direct_message）。
新回复经现有 WS 事件流到达前端（与正常发送一致）。仅在已有回复可回查时生效——
execute 路径的模板 announce（data 无 reply_id）查不到 → 404，前端 disabled 态兜底。
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


@router.post("/regenerate")
async def regenerate_reply(replyId: str = Query("")) -> Message:
    """[需求2-后端] Regenerate the reply identified by ``replyId``.

    Re-runs the agent's reply for a given ``reply_id`` (the per-turn streaming
    key persisted on ``agent_reply.data.reply_id`` by chat-path replies). The
    regenerate re-issues the original user input as a fresh turn so the agent
    produces a new reply over the same prompt — the new reply streams in over
    the existing WS event channel exactly like a normal send.

    Why replay-by-input rather than a planner/execute re-dispatch: the only
    stable, conversation-scoped handle carried by every chat-path reply is
    ``reply_id`` (worker node_brain / coordinator node_chat both stamp it on
    ``data``). The original prompt is not stored on the reply row itself, so
    we recover it from the immediately preceding ``user_input`` in the same
    conversation (same as the user re-typing it). This mirrors send_message's
    persist-user-msg → route path and reuses the existing routing layer verbatim
    (group-chat → ``route_user_message`` / single-chat → ``route_direct_message``),
    so regenerate needs no new engine/graph wiring and no new event type — the
    new agent_reply flows through ``persist_agent_reply`` → ``emit_message_added``
    exactly like a fresh send.

    Path coverage:
      · group-chat coordinator reply (no @mention) — re-routes as a fresh
        engineering demand (route_user_message → centralized path).
      · group-chat @mention peer reply — the recovered input carries the
        original @mention, so route_user_message re-hands off to the same agent.
      · single-chat worker reply — route_direct_message re-triggers the resident
        worker engine.

    Errors:
      · 404 when ``reply_id`` matches no persisted agent_reply (execute-path
        template announce carries no ``data.reply_id``; stale reply_id after a
        cleared session; unknown id). The frontend's regenerate button is
        disabled when no callback is wired, so this is a defensive guard.

    Concurrency: regenerate is a fire-and-forget kick — the routing layer queues
    the turn onto the group graph / resident engine. If a turn is already active
    for the conversation, the new turn queues behind it (same as a user typing
    while the agent is mid-reply). No busy-guard here — that's the runtime's job
    (``GroupRuntime`` serializes turns; single-chat resident engine is single-
    inbox). The endpoint returns the persisted user_input row immediately so the
    caller isn't blocked on the (potentially long) regeneration.

    Args:
        replyId: the ``reply_id`` (bare uuid hex) stamped on the target reply's
            ``data.reply_id``.

    Returns:
        The freshly-persisted ``user_input`` message row that drives the
        regeneration (its ``content`` is the recovered original prompt).
    """
    if not replyId:
        raise HTTPException(status_code=400, detail="replyId 不能为空")
    target = await crud.get_message_by_reply_id(replyId)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 reply_id={replyId} 对应的回复（可能为模板公告/已清除会话/未知 id）",
        )
    # Recover the original user input: the most recent user_input in the same
    # conversation that precedes the target reply (by created_at). This is the
    # prompt the agent originally answered — regenerating re-asks it verbatim.
    original_input = await crud.find_preceding_user_input(
        target.conversation_id, target.created_at
    )
    if original_input is None or not original_input.content:
        raise HTTPException(
            status_code=409,
            detail="无法定位原用户输入（该回复前无 user_input 行，无法重跑）",
        )
    # Persist a fresh user_input row (new id/timestamp) carrying the recovered
    # prompt. This is what the agent re-answers; the new agent_reply lands as a
    # separate bubble (the old reply is left intact — regenerate is additive,
    # not destructive: history is preserved).
    user_msg = await crud.create_message(
        {
            "conversation_id": target.conversation_id,
            "sender_id": "user",
            "receiver_id": "broadcast",
            "type": "user_input",
            "content": original_input.content,
        }
    )
    await emit_message_added(user_msg.model_dump())
    # Route identically to send_message — same dual-track dispatch by kind.
    group = await crud.get_group(user_msg.conversation_id)
    try:
        if group is not None:
            await route_user_message(
                user_msg.conversation_id, user_msg.content or "", converge=False
            )
        else:
            await route_direct_message(user_msg.conversation_id, user_msg.content or "")
    except ValueError as e:
        # 收束 must @ — recovered input had no @ (regenerate drops converge) so
        # this branch is unreachable in practice, but mirror send_message's guard.
        raise HTTPException(status_code=400, detail=str(e)) from e
    return user_msg
