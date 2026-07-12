"""Worker StateGraph — 4 nodes + conditional edge (Rust handle_notify worker branch).

Nodes: brain, chat, execute, ask. The brain node calls the worker LLM for a
three-state decision (chat/execute/ask). ``execute`` replies with a preview
and ``push_task`` to itself (the engine's _handle_task then runs the CLI via
``_run_worker_task``). The graph is compiled once with a MemorySaver
checkpointer and invoked per incoming notify by ``AgentEngine._handle_notify``.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from engine.inbox import push_task
from engine.state import WorkerState
from events import emit_message_added
from llm.client import chat_completion_stream, get_llm_config
from llm.extract_json import extract_json
from llm.prompts import build_brain_prompt
from store import crud

logger = logging.getLogger("multi-agent.worker")

# reply callback installed by the engine for the duration of one invoke
_REPLY_CB: Any = None


def set_reply_callback(cb: Any) -> None:
    global _REPLY_CB
    _REPLY_CB = cb


async def _unified_reply(
    group_id: str, agent_id: str, content: str, data: dict[str, Any] | None = None
) -> None:
    """Persist an agent_reply + emit + mention route (Rust engine.reply).

    ``data`` 写到持久化 message 上（存活重载/重连）。worker chat/ask 路径把
    brain 流式采集的 run-stats（``{elapsed_ms, tokens, model, reasoning_tokens,
    reasoning?}``）传进来，定稿气泡据此渲染「model · Ns · ↓ N tokens（含 N 推理）」
    状态行——与协调者 node_chat 落盘 ``_stream_stats`` 同形（前端 extractCoordStats
    不区分来源，按 data.elapsed_ms 是否存在判定渲染）。execute 路径回复是模板化
    announce（``收到，我来 {preview}...``），不匹配 brain token，不带 stats（传 None）。
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
    if _REPLY_CB is not None:
        await _REPLY_CB(content)


def _build_context(memory: list[dict], agent_name: str) -> str:
    """Render the recent memory into a context string (Rust build_context)."""
    recent = (memory or [])[-5:]
    if not recent:
        return "（无历史对话）"
    lines = []
    for m in recent:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户: {content}")
        else:
            lines.append(f"{agent_name}: {content}")
    return "\n".join(lines)


def _format_display_msg(sender: str, content: str) -> str:
    """Prefix non-user messages with the sender identity (Rust handle_notify)."""
    if sender != "user" and sender != "coordinator":
        return f"[来自智能体 {sender}] {content}"
    return content


async def node_brain_decide(state: WorkerState) -> dict:
    """Worker LLM three-state decision: chat/execute/ask (Rust handle_notify 389-422)."""
    memory = state.get("memory") or []
    context = _build_context(memory, state.get("agent_name", ""))
    display_msg = _format_display_msg(
        state.get("incoming_sender", ""), state.get("incoming_message", "")
    )
    prompt = build_brain_prompt(
        state.get("agent_role", ""),
        state.get("agent_name", ""),
        context,
        display_msg,
        state.get("system_prompt", ""),
    )
    config = get_llm_config()
    try:
        # system_prompt 作为独立 system 消息注入（空串时 LLM 以 brain prompt 内
        # 的 {role} 兜底人设作答）。单聊 agent 用自己的 system_prompt 主导行为，
        # 不再回「我可以调度团队成员来协助你」（那是 COORDINATOR_SYSTEM 的人设）。
        #
        # 改用流式 chat_completion_stream：采集 provider 返回的真实 usage
        # （completion_tokens / reasoning_tokens）+ 耗时 + model，塞进 _stream_stats，
        # 经 node_chat/ask 落盘到 agent_reply.data —— 定稿气泡据此渲染
        # 「model · Ns · ↓ N tokens（含 N 推理）」状态行（与协调者 node_chat 同形，
        # 前端 extractCoordStats 不区分来源）。原非流式 chat_completion 丢弃 usage，
        # worker 回复无状态行（只有协调者有）。worker 不推流式 token 事件（非协调者，
        # 无 reply_id 跟踪，逐字推对 worker 收 peer @notify 这种短回复无收益还增噪），
        # 只在采集完后落盘 stats——状态行在回复落地时一次性出现。
        start = time.monotonic()
        raw_parts: list[str] = []
        reasoning_parts: list[str] = []
        final_tokens = 0
        final_reasoning_tokens = 0
        async for content_delta, reasoning_delta, usage, reasoning_usage in chat_completion_stream(
            config,
            [
                {"role": "system", "content": state.get("system_prompt", "") or ""},
                {"role": "user", "content": prompt},
            ],
        ):
            if content_delta:
                raw_parts.append(content_delta)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
            if usage is not None:
                final_tokens = usage
            if reasoning_usage is not None:
                final_reasoning_tokens = reasoning_usage
        raw = "".join(raw_parts)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        model = str(config.get("model") or "")
        decision = _parse_brain_decision(raw)
        # usage 未到（provider 不回 usage 或中断）→ 退化为粗估 len//3（与协调者
        # _stream_coordinator_decision 的 live_tokens 启发式一致），保证状态行总有数。
        tokens = final_tokens if final_tokens else max(1, len(raw) // 3)
        reasoning_tokens = final_reasoning_tokens  # 0 for non-reasoning models
        reasoning_text = "".join(reasoning_parts)
        stats: dict[str, Any] = {
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "model": model,
            "reasoning_tokens": reasoning_tokens,
        }
        if reasoning_text:
            stats["reasoning"] = reasoning_text
    except Exception as e:
        logger.warning("[worker %s] brain decision failed: %s", state.get("agent_name"), e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "llm_error",
        }
        stats = None
    return {"decision": decision, "_stream_stats": stats}


async def node_chat(state: WorkerState) -> dict:
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        state["decision"]["content"],
        data=state.get("_stream_stats"),
    )
    return {}


async def node_execute(state: WorkerState) -> dict:
    """Reply with a preview and push a task to self (Rust handle_notify 432-440).

    The pushed task wakes the engine's _handle_task -> _run_worker_task, which
    in M3 calls the mock CLI executor.

    回复是模板化 announce（``收到，我来 {preview}...``），非 brain 流式文本，
    token 数不匹配 → 不带 stats（与协调者 dispatch announce 排除同理）。真正的
    execute 执行统计在 _run_worker_task / create_react_agent 那条线（task_log）。
    """
    content = state["decision"]["content"]
    preview = content[:30]
    await _unified_reply(
        state["group_id"], state["agent_id"], f"收到，我来 {preview}..."
    )
    await push_task(
        state["group_id"],
        state["agent_id"],
        state["agent_id"],
        content,
        None,
    )
    return {}


async def node_ask(state: WorkerState) -> dict:
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        state["decision"]["content"],
        data=state.get("_stream_stats"),
    )
    return {}


def route_brain(state: WorkerState) -> str:
    return state.get("decision", {}).get("action", "chat")


def build_worker_graph():
    """Compile the worker StateGraph with a MemorySaver checkpointer."""
    g: StateGraph = StateGraph(WorkerState)
    g.add_node("brain", node_brain_decide)
    g.add_node("chat", node_chat)
    g.add_node("execute", node_execute)
    g.add_node("ask", node_ask)

    g.add_edge(START, "brain")
    g.add_conditional_edges(
        "brain",
        route_brain,
        {"chat": "chat", "execute": "execute", "ask": "ask"},
    )
    g.add_edge("chat", END)
    g.add_edge("execute", END)
    g.add_edge("ask", END)

    return g.compile(checkpointer=MemorySaver())


def _parse_brain_decision(raw: str) -> dict:
    """Parse the worker LLM JSON response into action/content/reasoning (Rust parse_brain_decision)."""
    v = extract_json(raw)
    if v is None:
        return {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "parse_failed",
        }
    action = str(v.get("action", "chat"))
    if action not in ("chat", "execute", "ask"):
        action = "chat"
    content = str(v.get("content", ""))
    reasoning = str(v.get("reasoning", ""))
    return {"action": action, "content": content, "reasoning": reasoning}
