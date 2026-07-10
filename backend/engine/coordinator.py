"""Coordinator StateGraph — 7 nodes + conditional edges (Rust handle_notify_as_coordinator).

Nodes: classify, handle_reply, llm_decide, chat, dispatch, dispatch_next, summarize.
The graph is compiled once with a MemorySaver checkpointer and invoked per
incoming notify by ``AgentEngine._handle_notify``. Cross-invoke state
(memory, dispatch_plan, recent_routes) is owned by the engine and re-injected
on each ainvoke, so the graph nodes only return partial updates (action_taken,
reply_content, dispatch_plan) rather than mutating engine state directly.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from engine.dispatcher import dispatch_ready_steps
from engine.state import CoordinatorState
from events import emit_coordinator_plan, emit_coordinator_think, emit_message_added
from llm.client import chat_completion, get_llm_config
from llm.extract_json import extract_json
from llm.prompts import COORDINATOR_SYSTEM, build_coordinator_prompt
from store import crud

logger = logging.getLogger("multi-agent.coordinator")

# callback set by the engine on each ainvoke so nodes can persist replies +
# mention-route via the engine's unified _reply path. Nodes must not touch
# engine state directly; they call this to emit a reply message.
_REPLY_CB: Any = None


def set_reply_callback(cb: Any) -> None:
    """Install the engine's unified reply callable for the duration of one invoke."""
    global _REPLY_CB
    _REPLY_CB = cb


async def _unified_reply(group_id: str, agent_id: str, content: str) -> None:
    """Persist an agent_reply message + emit + mention route (Rust engine.reply).

    Delegates persistence to crud.create_message and emission to emit_message_added.
    Mention routing is performed by the engine's callback (set via
    ``set_reply_callback``) so recent_routes anti-loop state is owned by the engine.
    """
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


# ── nodes ─────────────────────────────────────────────────────────────────


async def node_classify_incoming(state: CoordinatorState) -> dict:
    """Classify the incoming notify: agent_reply with a matching dispatched step vs new demand."""
    kind = state.get("incoming_kind", "")
    sender = state.get("incoming_sender", "")
    if kind == "agent_reply" and sender != "user":
        # check if a dispatched step's task_id matches the notify data.task_id
        data = state.get("incoming_data") or {}
        task_id = data.get("task_id")
        plan = state.get("dispatch_plan") or []
        if task_id and any(s.get("task_id") == task_id and s.get("status") == "dispatched" for s in plan):
            return {"action_taken": "handle_reply"}
    return {"action_taken": "llm_decide"}


async def node_handle_reply(state: CoordinatorState) -> dict:
    """Worker reported back: mark the dispatched step completed/failed, then continue.

    Rust engine.rs 447-475. Finds the step whose task_id matches the notify
    data.task_id, sets status completed/failed + result. If all steps done ->
    summarize; otherwise dispatch_next.
    """
    data = state.get("incoming_data") or {}
    task_id = data.get("task_id")
    success = data.get("success", True)
    content = state.get("incoming_message", "")
    plan = list(state.get("dispatch_plan") or [])

    matched_idx = None
    for i, step in enumerate(plan):
        if step.get("task_id") == task_id and step.get("status") == "dispatched":
            matched_idx = i
            break

    if matched_idx is None:
        # no matching dispatched step -> fall back to LLM decision
        return {"action_taken": "llm_decide"}

    plan[matched_idx]["status"] = "completed" if success else "failed"
    plan[matched_idx]["result"] = content

    all_done = all(s.get("status") in ("completed", "failed") for s in plan)
    return {
        "dispatch_plan": plan,
        "action_taken": "summarize" if all_done else "dispatch_next",
    }


async def node_llm_decide(state: CoordinatorState) -> dict:
    """Call the coordinator LLM for a four-state decision (chat/dispatch/ask/continue).

    Rust engine.rs 497-558. Builds the prompt from members, recent memory,
    dispatch_plan status, and the incoming message. Parses the JSON response
    into action/content/plan. On LLM error or parse failure, falls back to chat
    with an apology message.
    """
    group_id = state["group_id"]
    agent_name = state["agent_name"]

    members_models = await crud.list_group_members_with_agent(group_id)
    member_list = [
        (m.agent_id, m.agent_name, m.agent_role) for m in members_models
    ]

    memory = state.get("memory") or []
    conversation = "\n".join(m.get("content", "") for m in memory[-8:])

    plan = state.get("dispatch_plan") or []
    dispatch_state = ""
    if plan:
        lines = []
        for s in plan:
            st = s.get("status", "")
            icon = (
                "✅" if st == "completed"
                else "❌" if st == "failed"
                else "🔄" if st == "dispatched"
                else "⏳"
            )
            lines.append(f"{icon} 步骤{s.get('step')}: {s.get('agent_name', '')}")
        dispatch_state = "\n".join(lines)

    prompt = build_coordinator_prompt(
        agent_name,
        member_list,
        conversation,
        dispatch_state,
        state.get("incoming_sender", ""),
        state.get("incoming_message", ""),
    )

    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            [
                {"role": "system", "content": COORDINATOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        decision = _parse_coordinator_decision(raw)
    except Exception as e:
        logger.warning("[coordinator] LLM decision failed: %s", e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边理解有点困难，能再说一次吗？",
            "plan": [],
        }

    await emit_coordinator_think(
        state["group_id"], state["agent_id"], decision["action"], decision["content"]
    )
    return {
        "action_taken": decision["action"],
        "reply_content": decision["content"],
        "dispatch_plan": decision.get("plan", []),
    }


async def node_chat(state: CoordinatorState) -> dict:
    """Persist + emit the reply_content via the unified reply path."""
    await _unified_reply(
        state["group_id"], state["agent_id"], state.get("reply_content", "")
    )
    return {}


async def node_dispatch(state: CoordinatorState) -> dict:
    """Store the plan, announce it via reply, then route to dispatch_next.

    Rust engine.rs 586-599. The LLM-returned plan replaces the engine's
    dispatch_plan (returned via the reducer). The announcement reply goes
    through the unified path so it persists + emits.
    """
    plan = state.get("dispatch_plan") or []
    plan_summary = "\n".join(
        f"{s.get('step')}. {s.get('agent_name', '')} → {s.get('instruction', '')[:40]}..."
        for s in plan
    )
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        f"📋 已制定协作计划，开始调度：\n{plan_summary}",
    )
    await emit_coordinator_plan(
        state["group_id"], state["agent_id"], state.get("dispatch_plan") or []
    )
    return {}


async def node_dispatch_next(state: CoordinatorState) -> dict:
    """Fan out ALL ready steps in parallel (DAG fan-out, MT-12).

    Dispatches every step that is pending with deps satisfied — independent
    steps go to their own worker engines which run concurrently as separate
    asyncio tasks. Returns ``action_taken="summarize"`` only if no step was
    dispatchable AND all steps are done; otherwise the graph ends (the engines
    run on, and each worker's report re-enters the coordinator via a notify).
    """
    group_id = state["group_id"]
    coordinator_id = state["agent_id"]
    plan = state.get("dispatch_plan") or []

    dispatched = await dispatch_ready_steps(group_id, coordinator_id, plan)

    if not dispatched:
        # no dispatchable step; if all done, summarize
        if plan and all(s.get("status") in ("completed", "failed") for s in plan):
            return {"action_taken": "summarize", "dispatch_plan": plan}
        return {"dispatch_plan": plan}

    return {"dispatch_plan": plan}


async def node_summarize(state: CoordinatorState) -> dict:
    """All steps done: summarize results, reply, clear plan (Rust dispatch_all_done)."""
    plan = state.get("dispatch_plan") or []
    summary = "\n".join(
        f"{'✅' if s.get('status') == 'completed' else '❌'} {s.get('agent_name', '')}: "
        f"{(s.get('result') or s.get('instruction', ''))[:200]}"
        for s in plan
    )
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        f"🎉 全部完成！协作结果汇总：\n{summary}",
    )
    # clear the plan
    return {"dispatch_plan": []}


# ── routing ───────────────────────────────────────────────────────────────


def route_after_classify(state: CoordinatorState) -> str:
    action = state.get("action_taken", "")
    if action == "handle_reply":
        return "handle_reply"
    return "llm_decide"


def route_after_handle_reply(state: CoordinatorState) -> str:
    action = state.get("action_taken", "")
    if action == "summarize":
        return "summarize"
    if action == "dispatch_next":
        return "dispatch_next"
    return "llm_decide"


def route_after_llm_decide(state: CoordinatorState) -> str:
    action = state.get("action_taken", "")
    if action == "chat":
        return "chat"
    if action == "dispatch":
        return "dispatch"
    # ask falls through to chat (same reply path)
    return "chat"


# ── graph builder ─────────────────────────────────────────────────────────


def build_coordinator_graph():
    """Compile the coordinator StateGraph with a MemorySaver checkpointer."""
    g: StateGraph = StateGraph(CoordinatorState)
    g.add_node("classify", node_classify_incoming)
    g.add_node("handle_reply", node_handle_reply)
    g.add_node("llm_decide", node_llm_decide)
    g.add_node("chat", node_chat)
    g.add_node("dispatch", node_dispatch)
    g.add_node("dispatch_next", node_dispatch_next)
    g.add_node("summarize", node_summarize)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify",
        route_after_classify,
        {"handle_reply": "handle_reply", "llm_decide": "llm_decide"},
    )
    g.add_conditional_edges(
        "handle_reply",
        route_after_handle_reply,
        {
            "summarize": "summarize",
            "dispatch_next": "dispatch_next",
            "llm_decide": "llm_decide",
        },
    )
    g.add_conditional_edges(
        "llm_decide",
        route_after_llm_decide,
        {"chat": "chat", "dispatch": "dispatch"},
    )
    g.add_edge("dispatch", "dispatch_next")
    g.add_conditional_edges(
        "dispatch_next",
        lambda s: "summarize" if s.get("action_taken") == "summarize" else END,
        {"summarize": "summarize", END: END},
    )
    g.add_edge("chat", END)
    g.add_edge("summarize", END)

    return g.compile(checkpointer=MemorySaver())


# ── decision parser (Rust parse_coordinator_decision) ─────────────────────


def _parse_coordinator_decision(raw: str) -> dict:
    """Parse the LLM JSON response into action/content/plan.

    Validates action against {chat, dispatch, ask, continue}. Falls back to
    chat with an apology if JSON parsing fails or action is unknown.
    """
    v = extract_json(raw)
    if v is None:
        return {
            "action": "chat",
            "content": "抱歉，我这边理解有点困难，能再说一次吗？",
            "plan": [],
        }
    action = str(v.get("action", "chat"))
    if action not in ("chat", "dispatch", "ask", "continue"):
        action = "chat"
    content = str(v.get("content", ""))
    plan_raw = v.get("plan")
    plan: list[dict] = []
    if isinstance(plan_raw, list):
        for p in plan_raw:
            if isinstance(p, dict):
                plan.append(
                    {
                        "step": p.get("step", 0),
                        "agent_id": p.get("agent_id", ""),
                        "agent_name": p.get("agent_name", ""),
                        "instruction": p.get("instruction", ""),
                        "depends_on": p.get("depends_on", []) or [],
                        "status": p.get("status", "pending"),
                        "result": p.get("result"),
                        "task_id": p.get("task_id"),
                    }
                )
    return {"action": action, "content": content, "plan": plan}
