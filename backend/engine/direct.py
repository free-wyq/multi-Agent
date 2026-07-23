"""Single-chat direct routing (Path C single-chat entity split).

``route_direct_message`` is the single-chat counterpart of ``route_user_message``.
It replaces the bypass that used to live at ``mention.py:298-305`` (the
``if group and group.config.get('single_chat')`` early return). With Path C,
single-chat conversations are their own entity (``ConversationEntity``), so
they no longer flow through ``route_user_message`` at all — the API layer
(``api/messages.py``) dispatches to ``route_direct_message`` when the
``conversation_id`` does not match a Group row.

Behavior preserved exactly (C2 shared-runtime principle):
  - persist user message (done by the caller before routing)
  - ``ensure_engine`` the resident worker engine (lazily built — a conversation
    created after startup via ``POST /api/conversations`` has no engine yet;
    without one the ``push_notify`` below drops into an unread inbox and the
    agent never replies, see ``registry.ensure_engine``).
  - ``push_notify`` to the resident worker engine (worker graph, not coordinator)
  - ``task_token`` streaming with ``reply_id`` (the CodeBuddy bubble contract)

The resident worker engine is keyed by ``{conversation_id}:{agent_id}`` —
``conversation_id`` takes the role ``group_id`` used to play for single-chat
engines. The engine is built worker-graph (``coordinator_id=""`` →
``is_coordinator=False`` → worker graph naturally, see ``registry.AgentEngine``).
"""
from __future__ import annotations

from engine.inbox import push_notify
from engine.registry import registry
from store import crud


async def route_direct_message(conversation_id: str, content: str) -> None:
    """Route a single-chat user message to the resident worker engine.

    Looks up the ``ConversationEntity`` to find the ``agent_id`` (the single
    conversation partner), lazily ensures a resident worker-graph engine exists
    for ``(conversation_id, agent_id)`` (a conversation created after startup has
    none — ``registry.ensure_engine`` builds one idempotently, mirroring the
    group-chat ``ensure_runtime``), then pushes a notify onto that agent's
    resident engine inbox. The engine was built worker-graph at load time or
    here (``coordinator_id=""`` → ``is_coordinator=False`` → worker graph), so
    its ``node_brain_decide`` streams ``task_token`` with a ``reply_id`` — the
    CodeBuddy bubble streaming contract verified by ``test_vb3``.

    No group graph, no @mention routing, no collaboration mode — single-chat
    has no collaboration surface. The resident engine drives the whole turn.
    """
    conversation = await crud.get_conversation(conversation_id)
    if not conversation or not conversation.agent_id:
        # Unknown conversation or no agent — nothing to route to. The user
        # message is already persisted (the API did that before routing), so
        # the user sees their own message even if no agent replies.
        return
    # Lazily build the resident engine for a post-startup conversation. A
    # conversation created via POST /api/conversations (the selectAgent path)
    # has no engine until this point — load_from_store only runs at startup.
    # Without an engine the push_notify below lands in an unread inbox and the
    # agent never replies (live-e2e 链路 1 FAIL 根因, 2026-07-23). ensure_engine
    # is idempotent: a startup-built engine is returned as-is, so no double loop.
    await registry.ensure_engine(conversation_id, conversation.agent_id)
    await push_notify(
        conversation_id,
        "coordinator_reply",
        "user",
        conversation.agent_id,
        content,
        None,
    )
