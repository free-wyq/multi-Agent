"""BusManager — WebSocket connections per group, plus event projection helpers.

Frontend subscribes to `ws://localhost:8000/ws/bus/{groupId}`; the manager keeps
a set of sockets per group and fans out event dicts. Event projection (domain →
BusEventData) is implemented here too; the engine layer calls these in M3+.

B15 全量审计 emit_* 错误处理 + 背压兜底（见 ``BusManager.emit`` docstring）：
  - 13 个 ``emit_*`` helper 是纯投影器（构 dict → ``await bus_manager.emit``），
    无 try/except、无静默吞没——错误处理单一真源在 ``BusManager.emit``。
  - ``BusManager.emit`` 的 ``except Exception`` 不再静默：``logger.debug`` + exc_info
    捕获（debug 非 exception——per-token 流式对掉线客户端重试会洪水，debug 在默认级
    静默、排障时开 DEBUG 可见）。
  - ``ws.send_json`` 包 ``asyncio.wait_for(timeout=WS_SEND_TIMEOUT)``——慢客户端
    不再无限阻塞 emit 协程（流式 per-token 背压兜底），超时即 prune 该 socket。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("multi-agent.bus")

# B15 背压兜底：单 socket send 超时上限（秒）。健康 LAN WS send <100ms，5s 是
# 「客户端真卡住」的宽松上限——超时即 prune 该 socket（best-effort fan-out 丢慢
# 客户端，不让一个慢消费者阻塞整群流式推送）。流式 per-token 路径（emit_task_token
# / emit_coordinator_token）每 token 都 await emit，无超时则慢客户端会回压 LLM 流式
# 循环致整条流水线停滞；有超时则慢客户端被 prune，流式对其他客户端继续。
WS_SEND_TIMEOUT: float = 5.0


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class BusManager:
    """Maintains a set of live WebSocket connections per group_id."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    def subscribe(self, group_id: str, ws: WebSocket) -> None:
        self._connections.setdefault(group_id, set()).add(ws)

    def unsubscribe(self, group_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(group_id)
        if not conns:
            return
        conns.discard(ws)
        if not conns:
            del self._connections[group_id]

    async def emit(self, group_id: str, event_data: dict[str, Any]) -> None:
        """Push an event dict to every live socket in the group. Prunes dead ones.

        B15 错误处理 + 背压兜底（全量审计 emit_* 后的单一错误真源）：

        错误处理——``except Exception`` 不再静默吞没：``logger.debug`` + ``exc_info=True``
        捕获 send 失败（socket 关闭 / 序列化错误 / 超时），然后 prune 该 socket。为何
        debug 而非 ``logger.exception``：这是 best-effort fan-out 的 per-socket 循环，
        流式 per-token 路径（emit_task_token / emit_coordinator_token）对掉线客户端每
        token 重试都触发 except——``logger.exception``（ERROR + traceback）会在客户端
        正常断连期间按 token 频率洪水日志。``logger.debug`` 在默认日志级静默（不污染
        INFO/WARNING），排障时开 DEBUG 即见异常类型/消息/socket，定位「真 bug（序列化
        错误 prune 了健康 socket）」vs「正常断连 prune」。13 个 ``emit_*`` helper 是纯
        投影器无 try/except，错误处理全汇聚到此——单一真源，不在 13 处重复。

        背压兜底——``ws.send_json`` 包 ``asyncio.wait_for(timeout=WS_SEND_TIMEOUT)``：
        慢客户端不再无限阻塞 emit 协程。流式 per-token 路径每 token ``await emit``，
        无超时则一个慢消费者回压 LLM 流式 ``async for`` 循环致整条流水线停滞；有超时
        则慢客户端超时即 prune（best-effort 丢慢消费者），流式对其他客户端继续。
        ``wait_for`` 超时取消 ``send_json`` 协程——部分 send 可能已写入底层 buffer，
        但超时后该 socket 立即 prune（不再 send），corrupted 状态不影响（已丢弃）。
        """
        conns = self._connections.get(group_id)
        if not conns:
            return
        dead: list[WebSocket] = []
        for ws in list(conns):
            try:
                await asyncio.wait_for(
                    ws.send_json(event_data), timeout=WS_SEND_TIMEOUT
                )
            except Exception:
                # B15: 不静默吞没——debug + exc_info 捕获（非 exception 防流式洪水），
                # socket 关闭/序列化错误/超时均 prune。排障开 DEBUG 可见异常类型。
                logger.debug(
                    "[bus] send failed, pruning socket from group %s",
                    group_id,
                    exc_info=True,
                )
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(group_id, ws)


bus_manager = BusManager()


# ── event projection helpers (DomainEvent → BusEventData) ─────────────

async def emit_message_added(msg: dict[str, Any]) -> None:
    await bus_manager.emit(
        msg["group_id"],
        {
            "id": msg["id"],
            "group_id": msg["group_id"],
            "task_id": msg.get("task_id"),
            "sender_id": msg["sender_id"],
            "receiver_id": msg["receiver_id"],
            "type": msg["type"],
            "content": msg.get("content"),
            "data": msg.get("data"),
            "timestamp": msg["created_at"],
        },
    )


async def emit_task_dispatched(
    group_id: str,
    task_id: str,
    step: int,
    agent_id: str,
    agent_name: str,
    instruction: str,
) -> None:
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": "coordinator",
            "receiver_id": agent_id,
            "type": "task_dispatch",
            "content": instruction,
            "data": {
                "step": step,
                "agent_name": agent_name,
                "agent_id": agent_id,
                "instruction": instruction,
            },
            "timestamp": _ts(),
        },
    )


async def emit_task_completed(
    group_id: str,
    task_id: str,
    agent_id: str,
    success: bool,
    result: str,
    exit_code: int | None,
    artifact: dict[str, Any] | None = None,
) -> None:
    """Task completion (success → ``task_complete`` / failure → ``task_failed``).

    ``artifact`` is the optional file-artifact manifest (PL-12
    ``scan_workspace_artifacts`` output ``{"files": [{name, path, size,
    modified_at}, ...]}``) produced during this task. Only forwarded on the
    success path — a failed/cancelled/timed-out task leaves no useful artifacts
    (the engine only scans on success, see ``registry._run_worker_task``).
    Thread into ``data.artifact`` so the frontend finalized bubble can render a
    download card (task 21) without an extra round-trip to the task row. Omitted
    (absent key) when ``None`` so legacy consumers that ignore ``data`` are
    unaffected.
    """
    data: dict[str, Any] = {"exit_code": exit_code}
    if artifact:
        data["artifact"] = artifact
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "task_complete" if success else "task_failed",
            "content": result,
            "data": data,
            "timestamp": _ts(),
        },
    )


async def emit_task_log(group_id: str, task_id: str, sender_id: str, line: str) -> None:
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": sender_id,
            "receiver_id": "broadcast",
            "type": "task_log",
            "content": line,
            "data": None,
            "timestamp": _ts(),
        },
    )


# ── M11 typed event helpers (structured agent execution transparency) ────


async def emit_task_tool(
    group_id: str,
    task_id: str,
    agent_id: str,
    phase: str,
    name: str,
    content: str,
    data: dict[str, Any] | None,
) -> None:
    """Tool invocation lifecycle (on_tool_start / on_tool_end)."""
    payload: dict[str, Any] = {"phase": phase, "name": name}
    if data:
        payload.update(data)
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "task_tool",
            "content": content,
            "data": payload,
            "timestamp": _ts(),
        },
    )


async def emit_task_think(
    group_id: str,
    task_id: str,
    agent_id: str,
    phase: str,
    content: str,
) -> None:
    """Agent reasoning (intermediate thinking) or final answer."""
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "task_think",
            "content": content,
            "data": {"phase": phase},
            "timestamp": _ts(),
        },
    )


async def emit_task_token(
    group_id: str,
    task_id: str,
    agent_id: str,
    phase: str,
    delta: str,
) -> None:
    """Per-token streaming delta (PL-08 on_chat_model_stream → live rendering).

    Projects one ``on_chat_model_stream`` chunk onto a ``task_token`` WS event.
    The frontend concatenates the ``content`` deltas to render the model's
    output live (逐字流式). ``phase`` is ``"streaming"`` for every chunk —
    whether the chunk is part of reasoning-before-a-tool or the final answer
    is only known once ``on_chain_end|model`` fires, which still emits the
    complete text as ``task_think``/``task_answer`` (additive, non-breaking).
    """
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "task_token",
            "content": delta,
            "data": {"phase": phase},
            "timestamp": _ts(),
        },
    )


async def emit_agent_status(
    group_id: str,
    agent_id: str,
    agent_name: str,
    status: str,
    current_task_id: str | None,
) -> None:
    """Agent status transition (idle / executing / offline)."""
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": current_task_id,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "agent_status",
            "content": None,
            "data": {
                "status": status,
                "current_task_id": current_task_id,
                "agent_name": agent_name,
            },
            "timestamp": _ts(),
        },
    )


async def emit_coordinator_plan(
    group_id: str,
    coordinator_id: str,
    plan: list[dict[str, Any]],
) -> None:
    """Coordinator dispatch plan (steps, DAG dependencies)."""
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "coordinator_plan",
            "content": None,
            "data": {"plan": plan},
            "timestamp": _ts(),
        },
    )


async def emit_coordinator_think(
    group_id: str,
    coordinator_id: str,
    action: str,
    content: str,
) -> None:
    """Coordinator thinking step (action + reasoning)."""
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "coordinator_think",
            "content": content,
            "data": {"action": action},
            "timestamp": _ts(),
        },
    )


async def emit_coordinator_token(
    group_id: str,
    coordinator_id: str,
    reply_id: str,
    delta: str,
) -> None:
    """Per-token streaming delta for a coordinator reply (逐字流式).

    The coordinator's reply is generated by a direct streaming LLM call (not
    create_react_agent), so it doesn't go through the worker ``task_token``
    channel. This is the coordinator's analogue: each decoded content delta is
    projected as a ``coordinator_token`` WS event keyed by ``reply_id`` (one
    UUID per node_llm_decide invocation) so the frontend can accumulate the
    deltas into a single streaming bubble and reset cleanly between turns.
    """
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "coordinator_token",
            "content": delta,
            "data": {"reply_id": reply_id, "phase": "streaming"},
            "timestamp": _ts(),
        },
    )


async def emit_coordinator_reasoning(
    group_id: str,
    coordinator_id: str,
    reply_id: str,
    delta: str,
) -> None:
    """Per-token streaming delta of the model's *reasoning* chain (推理流式).

    Reasoning models (DeepSeek-R1/o1-style) stream ``reasoning_content`` — the
    model's internal chain-of-thought — *before* the visible ``content``. This
    is projected as a ``coordinator_reasoning`` WS event keyed by the same
    ``reply_id`` as the reply's ``coordinator_token`` stream, so the frontend
    can accumulate it into a collapsed-by-default "思考过程" panel on the
    streaming bubble — users who care can expand it, others see only the reply.

    Non-reasoning providers never emit reasoning_content → this is never called
    → no event → the panel simply doesn't render. Zero-cost for non-reasoning
    models, opt-in visibility for reasoning ones.
    """
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "coordinator_reasoning",
            "content": delta,
            "data": {"reply_id": reply_id, "phase": "streaming"},
            "timestamp": _ts(),
        },
    )


async def emit_coordinator_stats(
    group_id: str,
    coordinator_id: str,
    reply_id: str,
    elapsed_ms: int,
    tokens: int,
    phase: str,
    model: str = "",
    reasoning_tokens: int = 0,
) -> None:
    """Live run-statistics for a coordinator reply (elapsed time + token count).

    Emitted periodically (throttled ~200ms) while the coordinator LLM streams,
    plus a final emit with ``phase="done"`` carrying the real
    ``completion_tokens`` from the stream's usage chunk. The frontend renders
    these as a Claude-Code-style status line ("model · Ns · ↓ N tokens · thinking")
    that refreshes in real time alongside the streaming bubble.

    ``model`` is the LLM model id that produced this reply (``config["model"]``),
    surfaced so the bubble can show *which* model answered — useful when the
    user hot-switches models via the provider catalog. Empty string = unknown
    (legacy callers); the frontend falls back to omitting the model segment.

    ``reasoning_tokens`` is how many of ``tokens`` were the model's internal
    reasoning chain (from ``usage.completion_tokens_details.reasoning_tokens``),
    as opposed to visible reply text. 0 for non-reasoning models. Surfaced so
    the status line can show "↓ 148 tokens（含 133 推理）" — otherwise a 5-word
    reply showing 148 tokens looks fake when 133 were invisible reasoning.
    """
    await bus_manager.emit(
        group_id,
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "coordinator_stats",
            "content": None,
            "data": {
                "reply_id": reply_id,
                "elapsed_ms": elapsed_ms,
                "tokens": tokens,
                "phase": phase,
                "model": model,
                "reasoning_tokens": reasoning_tokens,
            },
            "timestamp": _ts(),
        },
    )
