"""LangGraph state TypedDicts + reducers (blueprint C.2.1 / C.2.2).

``CoordinatorState`` and ``WorkerState`` are the state schemas for the two
StateGraphs. Reducers: ``append_list`` concatenates lists (memory), ``merge_dict``
right-overrides-left (dispatch_plan, recent_routes). The engine injects the
runtime values (memory, dispatch_plan, recent_routes) at each ``ainvoke`` so
the checkpointer + thread_id keep cross-invoke state.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def append_list(left: list, right: list) -> list:
    """Reducer: concatenate lists (right appended to left)."""
    return (left or []) + (right or [])


def merge_dict(left: dict, right: dict) -> dict:
    """Reducer: merge dict (right overrides left)."""
    result = dict(left or {})
    result.update(right or {})
    return result


def replace_value(left: Any, right: Any) -> Any:
    """Reducer: last-write-wins (right replaces left). Used for dispatch_plan
    where nodes return the full updated plan rather than appending."""
    return right if right is not None else left


class CoordinatorState(TypedDict, total=False):
    """State schema for the coordinator StateGraph.

    The engine injects ``memory``, ``dispatch_plan``, and ``recent_routes`` at
    each ``ainvoke`` from the AgentEngine's in-memory fields (these are the
    cross-invoke state kept consistent by the MemorySaver checkpointer + thread_id).
    """

    # identity
    group_id: str
    agent_id: str
    agent_name: str
    # agent 基础 system_prompt（agent_def.system_prompt 缓存到引擎）。群聊 Leader
    # 由 coordinator.py 三处 system 消息拼接成 base+COORDINATOR_SYSTEM；单聊走
    # worker 图时由 brain 作为独立 system 消息注入。空串=用兜底人设。
    system_prompt: str

    # incoming message
    incoming_message: str
    incoming_sender: str
    incoming_kind: str  # agent_reply | coordinator_task | coordinator_reply
    incoming_data: dict | None

    # cross-invoke state (injected by engine)
    memory: Annotated[list[dict], append_list]
    dispatch_plan: Annotated[list[dict], replace_value]
    recent_routes: Annotated[dict, merge_dict]
    # PL-02/PL-03 plan-confirmation switch, injected per ainvoke from the
    # group config (GroupEntity.config.auto_confirm). When False (default) the
    # coordinator announces the plan then ENDS, waiting for an explicit user
    # confirmation before fan-out. When True ("直接干" mode) the graph skips
    # the wait and dispatches immediately.
    auto_confirm: bool
    # MT-03 Leader 指挥策略, injected per ainvoke from the group config
    # (GroupEntity.config.leader_strategy via models.get_leader_strategy).
    # Free-text guidance the user writes for the group's Leader (e.g.
    # "注重代码质量，每步必须自测通过再交付"). node_llm_decide passes it to
    # build_coordinator_prompt so the Leader's 拆解/派工 decisions honour it.
    # Empty string when unset (coordinator runs with no extra strategy).
    leader_strategy: str

    # decision output
    reply_content: str
    action_taken: str  # chat | dispatch | ask | continue | handle_reply | summarize | dispatch_next | wait_confirm | confirm_dispatch
    # per-turn streaming run-stats carried from node_llm_decide to node_chat:
    # {reply_id, elapsed_ms, tokens}. node_chat stamps it onto the persisted
    # agent_reply's `data` so the finalized bubble keeps rendering the
    # "Ns · ↓ N tokens" status line after the streaming bubble retires.
    # None for non-chat actions (dispatch/summarize announce their own reply).
    _stream_stats: dict | None


class WorkerState(TypedDict, total=False):
    """State schema for the worker StateGraph."""

    # identity
    group_id: str
    agent_id: str
    agent_name: str
    agent_role: str
    # agent 基础 system_prompt：单聊 chat 路径用（brain 作为独立 system 消息注入）。
    # 空串时 LLM 以 brain prompt 内「你是一名专业的 {role}…」兜底人设作答。
    system_prompt: str

    # incoming message
    incoming_message: str
    incoming_sender: str

    # cross-invoke state
    memory: Annotated[list[dict], append_list]

    # decision output
    decision: dict  # {action, content, reasoning}
