"""Unified agent_reply persistence + emit + mention route (split out, task B10).

Three near-identical copies of the reply path previously existed:

- ``engine.registry.AgentEngine._reply`` — the execute-path announce
  (``任务完成 🎉`` / ``执行出错了`` / ``⏹ 任务已停止`` / ``⏱ 超时``),
  ``data`` always ``None`` (template text, not brain LLM output, no stats),
  routes ``@mention`` directly via ``route_mentions``.
- ``engine.coordinator._unified_reply`` — the coordinator graph's reply path,
  ``data`` carries the streaming run-stats from ``_stream_coordinator_decision``
  so the finalized bubble keeps its "Ns · ↓ N tokens" status line; routes
  ``@mention`` via the engine's reply callback (set per-invoke).
- ``engine.worker._unified_reply`` — the worker graph's reply path, identical
  shape to the coordinator's (``data`` carries brain run-stats); routes via
  the same callback mechanism.

All three build the same ``agent_reply`` message dict
(``{group_id, task_id=None, sender_id, receiver_id="broadcast", type=
"agent_reply", content, data}``), persist it via ``crud.create_message``, and
``emit_message_added``. The only variation is *how* the ``@mention`` route is
invoked: the registry calls ``route_mentions`` directly (it owns the engine
context), while the graph nodes call an engine-installed callback (they can't
reach the engine instance). This module factors the shared persist+emit core
into ``persist_agent_reply``; each caller keeps its own routing choice but
reuses the single persistence truth.

Why split (B10): the three copies had drifted in comment density and small
details (the registry copy hard-codes ``data=None``; the two graph copies
accept ``data`` and call the callback). A bug in one (e.g. the message dict
shape, the emit payload) would have to be fixed three times. Centralizing the
persist+emit core means a future change to the agent_reply shape (a new field,
a different emit payload) is one edit. The routing divergence is preserved
intentionally — it reflects a real architectural seam (graph nodes vs engine
instance) and merging it would force the graph nodes to reach the engine,
re-introducing the coupling B9 just removed.
"""
from __future__ import annotations

from typing import Any

from events import emit_message_added
from store import crud


async def persist_agent_reply(
    group_id: str,
    agent_id: str,
    content: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an ``agent_reply`` message + emit ``message_added``. Return the row.

    Single source for the agent_reply shape (``task_id=None``, ``receiver_id=
    "broadcast"``, ``type="agent_reply"``) and the persist+emit sequence. Both
    the registry's execute-path announce and the coordinator/worker graph
    nodes' reply paths delegate here; the message dict can no longer drift
    between the three former copies (B10).

    ``data`` is threaded onto the persisted message so it survives reload /
    reconnect. The coordinator/worker chat paths pass the streaming run-stats
    (``{reply_id, elapsed_ms, tokens, model, reasoning_tokens, reasoning?}``)
    so the finalized bubble keeps rendering the "model · Ns · ↓ N tokens" status
    line after the streaming bubble retires. ``data=None`` (the registry's
    execute-path announce) leaves no stats — the frontend's ``extractCoordStats``
    returns null on a missing ``elapsed_ms`` and renders no status line, which
    is correct for template announce text (not brain LLM output).

    Routing (``@mention`` / ``route_mentions``) is deliberately NOT done here —
    the registry owns the engine context and routes directly, while the graph
    nodes route via an engine-installed callback (set per-invoke). That seam is
    real and preserved. This helper is only the persist+emit truth.

    Returns the persisted message model dict (``msg.model_dump()``) so callers
    that need the row id / timestamp (e.g. ``emit_message_added`` already
    consumes it here; a future caller could log it) can use it without a second
    DB round-trip.
    """
    msg = await crud.create_message(
        {
            "group_id": group_id,
            "task_id": None,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "agent_reply",
            "content": content,
            "data": data,
        }
    )
    await emit_message_added(msg.model_dump())
    return msg.model_dump()


__all__ = ["persist_agent_reply"]
