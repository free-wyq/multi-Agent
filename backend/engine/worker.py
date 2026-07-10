"""Worker StateGraph — 4 nodes + conditional edge (Rust handle_notify worker branch).

Nodes: brain, chat, execute, ask. The brain node calls the worker LLM for a
three-state decision (chat/execute/ask). ``execute`` replies with a preview
and ``push_task`` to itself (the engine's _handle_task then runs the CLI via
``_run_worker_task``). The graph is compiled once with a MemorySaver
checkpointer and invoked per incoming notify by ``AgentEngine._handle_notify``.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from engine.inbox import push_task
from engine.state import WorkerState
from events import emit_message_added
from llm.client import chat_completion, get_llm_config
from llm.extract_json import extract_json
from llm.prompts import build_brain_prompt
from store import crud

logger = logging.getLogger("multi-agent.worker")

# reply callback installed by the engine for the duration of one invoke
_REPLY_CB: Any = None


def set_reply_callback(cb: Any) -> None:
    global _REPLY_CB
    _REPLY_CB = cb


async def _unified_reply(group_id: str, agent_id: str, content: str) -> None:
    """Persist an agent_reply + emit + mention route (Rust engine.reply)."""
    msg = await crud.create_message(
        {
            "group_id": group_id,
            "task_id": None,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "agent_reply",
            "content": content,
            "data": None,
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
    )
    config = get_llm_config()
    try:
        raw = await chat_completion(
            config, [{"role": "user", "content": prompt}]
        )
        decision = _parse_brain_decision(raw)
    except Exception as e:
        logger.warning("[worker %s] brain decision failed: %s", state.get("agent_name"), e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "llm_error",
        }
    return {"decision": decision}


async def node_chat(state: WorkerState) -> dict:
    await _unified_reply(
        state["group_id"], state["agent_id"], state["decision"]["content"]
    )
    return {}


async def node_execute(state: WorkerState) -> dict:
    """Reply with a preview and push a task to self (Rust handle_notify 432-440).

    The pushed task wakes the engine's _handle_task -> _run_worker_task, which
    in M3 calls the mock CLI executor.
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
        state["group_id"], state["agent_id"], state["decision"]["content"]
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
