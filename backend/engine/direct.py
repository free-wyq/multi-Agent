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
  - ``push_notify`` to the resident worker engine (worker graph, not coordinator)
  - ``task_token`` streaming with ``reply_id`` (the CodeBuddy bubble contract)

The resident worker engine is keyed by ``{conversation_id}:{agent_id}`` —
``conversation_id`` takes the role ``group_id`` used to play for single-chat
engines. The engine is built worker-graph (``coordinator_id=""`` →
``is_coordinator=False`` → worker graph naturally, see ``registry.AgentEngine``).
"""
from __future__ import annotations

from engine.inbox import push_notify
from store import crud


async def route_direct_message(conversation_id: str, content: str) -> None:
    """Route a single-chat user message to the resident worker engine.

    Looks up the ``ConversationEntity`` to find the ``agent_id`` (the single
    conversation partner), then pushes a notify onto that agent's resident
    engine inbox. The engine was built worker-graph at load time
    (``coordinator_id=""`` → ``is_coordinator=False`` → worker graph), so its
    ``node_brain_decide`` streams ``task_token`` with a ``reply_id`` — the
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
    await push_notify(
        conversation_id,
        "coordinator_reply",
        "user",
        conversation.agent_id,
        content,
        None,
    )
