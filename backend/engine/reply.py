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

import logging
from typing import Any

from events import emit_message_added
from llm.card_fragment import count_card_fragments
from store import crud

logger = logging.getLogger("multi-agent.reply")


async def persist_agent_reply(
    group_id: str,
    agent_id: str,
    content: str,
    data: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Persist an ``agent_reply`` message + emit ``message_added``. Return the row.

    Single source for the agent_reply shape (``receiver_id="broadcast"``,
    ``type="agent_reply"``) and the persist+emit sequence. Both the registry's
    execute-path announce and the coordinator/worker graph nodes' reply paths
    delegate here; the message dict can no longer drift between the three
    former copies (B10).

    ``data`` is threaded onto the persisted message so it survives reload /
    reconnect. The coordinator/worker chat paths pass the streaming run-stats
    (``{reply_id, elapsed_ms, tokens, model, reasoning_tokens, reasoning?}``)
    so the finalized bubble keeps rendering the "model · Ns · ↓ N tokens" status
    line after the streaming bubble retires. ``data=None`` (the registry's
    execute-path announce) leaves no stats — the frontend's ``extractCoordStats``
    returns null on a missing ``elapsed_ms`` and renders no status line, which
    is correct for template announce text (not brain LLM output).

    回放 trace（持久化气泡回放，落 ``data.trace`` 子键，不改 DB schema）：
    execute 路径（registry._run_worker_task 的 on_log）把 tool_start/tool_end/
    think/answer 这类结构化步累加到 ``self._turn_trace[turn_reply_id]``，reply 时
    塞进 ``reply_data["trace"]`` 落到 ``message.data.trace``。前端持久化气泡复用
    ChatMessageBubble 把 ``msg.data.trace`` 解析成 toolEvents/thinkEvents 渲染
    思考折叠区 + 工具调用折叠区（与流式期同一渲染管线）。token 流式增量不落 trace
    （reload 后回放逐字无意义、量大），log bookkeeping 行不落（无结构化语义且与最终
    回复不重复）。失败/取消/超时路径也落 trace（用户要看失败走到哪步了）。chat 路径
    （coordinator/worker node_chat，无 tool/think emit）data 不带 trace key。

    ``task_id`` (B22): the task this reply closes, for the registry's
    execute-path announce (``任务完成 🎉`` / ``执行出错了`` / ``⏹ 任务已停止``
    / ``⏱ 超时``). Threaded onto the persisted row + the emitted
    ``message_added`` WS event so the frontend ``finalizedBubbles`` auto-retire
    can match the reply to its closing ``task_complete``/``task_failed`` event by
    exact ``task_id`` (primary), falling back to sender+timestamp only when the
    reply carries no ``task_id`` (single-chat worker chat path — worker graph
    replies have no task_id; their ``_stream_stats`` carries a ``reply_id``
    instead). Default ``None`` preserves every existing caller: the coordinator/
    worker graph ``_unified_reply`` paths pass only ``data`` and leave
    ``task_id`` unset, so their agent_reply rows keep ``task_id=None`` exactly as
    before B22. The registry's ``_reply`` is the only caller that passes a real
    task_id (B22 wires it; see ``_run_worker_task`` / ``_on_task_cancelled`` /
    ``_on_task_timed_out`` passing ``task["id"]``).

    Why thread task_id through the reply row (B22) rather than the WS event
    alone: the frontend ``finalizedBubbles`` retire check reads
    ``chatMessages`` (the persisted-message list, rebuilt from
    ``messageApi.listByGroup`` on reconnect/switch-group). The WS
    ``task_complete`` event already carries ``task_id``, but the retiring reply
    was matched to it only by ``sender_id`` + ``created_at >= event.timestamp``
    — fragile (the prior comment self-flagged "fragile": the logs-append path
    coerces WS messages and task_id "may be lost"). Persisting ``task_id`` on
    the reply row makes the match exact and reload-safe: the same task_id is on
    both the closing event and the retiring reply regardless of which transport
    (live WS vs reload-from-DB) delivered them. The sender+timestamp fallback
    stays (for the task_id-less chat paths), so B22 is strictly additive.

    Routing (``@mention`` / ``route_mentions``) is deliberately NOT done here —
    the registry owns the engine context and routes directly, while the graph
    nodes route via an engine-installed callback (set per-invoke). That seam is
    real and preserved. This helper is only the persist+emit truth.

    需求2-后端·卡片观测（[需求2-设计] commit 9df5116 单真源
    ``docs/structured-result-card-schema.md``）：落盘后用
    ``count_card_fragments(content)`` 统计 `````card```` 围栏块数，仅当 >0 时
    ``logger.info`` 记一行（验证 LLM 是否遵守 ``CARD_OUTPUT_CONTRACT`` 提示词）。
    纯观测，不阻断、不解析 payload、不改 message dict / ``data``（卡片是
    ``content`` 子串，走现有透传，不改 DB/事件）。0 块不记（避免对纯散文回复刷
    日志）。统计包在 try/except best-effort 内，正则/计数失败不影响落盘主流程。

    Returns the persisted message model dict (``msg.model_dump()``) so callers
    that need the row id / timestamp (e.g. ``emit_message_added`` already
    consumes it here; a future caller could log it) can use it without a second
    DB round-trip.
    """
    msg = await crud.create_message(
        {
            # conversation_id 是 Path C 严格改名后的 group_id（群聊消息持 group_id，
            # 单聊消息持 conversation_id，同一字段）。persist_agent_reply 的 group_id
            # 入参在群聊场景就是 group_id，单聊走 route_direct_message 不经此函数。
            "conversation_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "agent_reply",
            "content": content,
            "data": data,
        }
    )
    # 需求2-后端：可选的卡片片段观测——当 LLM 按 CARD_OUTPUT_CONTRACT 在 content
    # 里产出了 `````card```` 围栏块时，记录片段数（验证提示词是否被遵守）。不阻断、
    # 不解析、不改 message dict（卡片是 content 子串，走现有透传）。0 块时不记（避免
    # 对绝大多数纯散文回复刷 info 日志）。count_card_fragments 是纯函数，零成本短路。
    try:
        n_cards = count_card_fragments(content)
        if n_cards:
            logger.info(
                "[reply %s] agent_reply 含 %d 个结构化卡片片段（card 围栏）",
                msg.id, n_cards,
            )
    except Exception:
        # 观测是 best-effort：正则/计数不应影响落盘主流程。
        logger.debug("[reply %s] count_card_fragments 失败（忽略）", msg.id, exc_info=True)
    await emit_message_added(msg.model_dump())
    # 任务19c IM 出站钩子：reply 真源落盘 + emit 后，把回复推给绑定了该会话的 IM
    # channel。无 channel → no-op（纯前端对话零副作用，行为同 19c 前）。reply.py
    # 只调一个 ``maybe_*`` 函数，不知 IM 是否存在 / 几个 channel / 什么平台——
    # adapter 查表 + 分发全在 ``engine.im.outbound``（见 docs/im-gateway-design.md
    # §5.2 方案 C）。延迟 import 防 reply↔IM 循环（outbound 反向 import store.crud，
    # 不 import reply）。per-channel try/except 隔离：单 channel 失败不 raise 到此
    # 处崩 reply 主流程（落盘 + emit 已完成，出站是 best-effort 投递）。
    try:
        from engine.im.outbound import maybe_deliver_outbound
        await maybe_deliver_outbound(msg.model_dump())
    except Exception:
        logger.debug(
            "[reply %s] IM outbound hook failed (reply already persisted + emitted)",
            msg.id, exc_info=True,
        )
    return msg.model_dump()


__all__ = ["persist_agent_reply"]
