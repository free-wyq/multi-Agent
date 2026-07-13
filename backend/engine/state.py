"""LangGraph state TypedDicts + reducers (blueprint C.2.1 / C.2.2).

``CoordinatorState`` and ``WorkerState`` are the state schemas for the two
StateGraphs. Reducers: ``append_list`` concatenates lists (memory), ``merge_dict``
right-overrides-left (dispatch_plan, recent_routes). The engine injects the
runtime values (memory, dispatch_plan, recent_routes) at each ``ainvoke`` so
the checkpointer + thread_id keep cross-invoke state.

``GroupState`` (task: 去中心化群图 handoff 迁移) is the shared state schema for
the single-graph-per-group swarm topology (engine/group_graph.py, to be wired in
later tasks). It supersedes neither ``CoordinatorState`` nor ``WorkerState`` —
those remain the schemas for the resident coordinator/worker graphs until the
group-graph migration lands and consumers switch over.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


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
    action_taken: str  # chat | dispatch | ask | continue | handle_reply | summarize | dispatch_next | confirm_dispatch
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
    # 群主 agent_id（用于把协调者消息在 DB 上下文里标成「协调者」而非裸 id）。
    # 群聊 worker brain 走 _build_context_from_db 从 messages 表拉上下文时用。
    coordinator_id: str
    # agent 基础 system_prompt：单聊 chat 路径用（brain 作为独立 system 消息注入）。
    # 空串时 LLM 以 brain prompt 内「你是一名专业的 {role}…」兜底人设作答。
    system_prompt: str

    # incoming message
    incoming_message: str
    incoming_sender: str

    # cross-invoke state（worker 侧已不再用 memory——上下文改从 messages 表真源拉，
    # 见 worker._build_context_from_db。保留字段仅为 state schema 兼容，引擎仍注入但
    # brain 不读。协调者的 memory 仍走自己的 CoordinatorState.memory。）
    memory: Annotated[list[dict], append_list]

    # decision output
    decision: dict  # {action, content, reasoning}
    # per-turn streaming run-stats carried from node_brain_decide to node_chat/ask,
    # mirror of CoordinatorState._stream_stats: {reply_id, elapsed_ms, tokens,
    # model, reasoning_tokens, reasoning?}. node_brain_decide generates one
    # ``reply_id`` (uuid4 hex) per turn — task 23 introduces it so worker
    # single-chat replies can stream live via ``task_token`` keyed by reply_id
    # (task 24 pushes token deltas in the brain's streaming loop), and node_chat/
    # ask stamp it onto the persisted agent_reply's ``data`` so the finalized
    # bubble keeps the "Ns · ↓ N tokens" status line + lets the frontend retire
    # the streaming bubble cleanly when the persisted reply lands. Without it the
    # worker reply appeared in one shot (no streaming bubble) — task 24/25 lift
    # single-chat to the same live-逐字 UX the coordinator already has.
    # execute 路径回复是模板化 announce，不匹配 brain token，不带 stats（与协调者
    # dispatch 排除同理）。None=无统计（错误兜底/execute）。
    _stream_stats: dict | None


class GroupState(TypedDict, total=False):
    """Shared state schema for the single-graph-per-group swarm topology.

    Task: 去中心化群图 handoff 迁移（engine/group_graph.py，后续任务装配）.
    Replaces the per-agent resident ``CoordinatorState``/``WorkerState`` graphs
    with ONE compiled graph per group: every agent (including the coordinator)
    is a node, and "who speaks next" is decided by LangGraph handoff edges (an
    @mention parsed from the current speaker's reply → ``goto`` the target agent
    node; no @mention → ``END`` ends the turn). See memory
    ``decentralized-scheduling-stop-plan-2026-07-13`` (方向 A) and
    ``langgraph-two-collaboration-paths`` for the design rationale.

    Field-by-field mapping to the three群聊缺陷 (顺序乱 / 协调者插话 / 停不下来):

      · ``messages`` (``add_messages`` reducer) — the turn's accumulated
        message log shared across all agent nodes. LangGraph's handoff model
        threads this through every node so each speaker sees prior context.
        Replaces the per-engine ``CoordinatorState.incoming_message`` /
        ``WorkerState.incoming_message`` pair (one shared log, not two routed
        fields). ``add_messages`` dedups by id so a resumed turn does not
        double-append.
      · ``current_speaker`` — the agent_id of the node currently driving the
        turn. Set by ``route_entry`` at turn start and by each handoff edge on
        control transfer. Nodes read it to know "who am I speaking as" without
        re-deriving from identity. (后端/前端接龙顺序乱的根因是同一 agent 一轮
        被驱动两次；current_speaker + handoff 串行只一节点在跑共同消除连发.)
      · ``dispatch_plan`` (``replace_value`` reducer) — the coordinator's DAG
        plan, last-write-wins (a node returns the FULL updated plan rather than
        a delta, mirroring ``CoordinatorState.dispatch_plan``). Carried in the
        shared state so the coordinator's dispatch/handle_reply/summarize nodes
        and any agent's report-back all read/write the same plan slot.
      · ``turn_count`` (int) — increments per turn within a single
        ``graph.ainvoke``. Bounds how many handoffs one user turn may chain
        (防连发 + 防 @mention 死循环的图内兜底, complementing LangGraph's
        ``recursion_limit``). Resets to 0 at each ``invoke_turn``.
      · ``recent_speakers`` (list[str], ``append_list`` reducer) — the ordered
        list of agent_ids that have spoken this turn. Nodes / ``route_entry``
        consult it to enforce "same agent not driven twice in one turn"
        (顺序乱根因②). Cleared at turn boundary (paired with ``turn_count`` reset).
      · ``auto_confirm`` / ``leader_strategy`` — group-config flags injected per
        ``invoke_turn`` from ``GroupEntity.config`` (same source as
        ``CoordinatorState.auto_confirm`` / ``leader_strategy``), kept verbatim
        so coordinator sub-nodes inside the group graph read the same config.
      · ``memory`` (``append_list`` reducer) — the group's shared turn memory,
        appended across handoffs within a turn (mirrors
        ``CoordinatorState.memory`` semantics; each agent node may push a
        per-turn memo). Persisted via checkpointer across ``invoke_turn``.
      · ``incoming_*`` — the user/system message that kicked off the turn,
        mirroring ``CoordinatorState.incoming_*`` (``incoming_message`` /
        ``incoming_sender`` / ``incoming_kind`` / ``incoming_data``).
        ``route_entry`` injects them from ``route_user_message``; coordinator
        sub-nodes (classify/llm_decide) read them exactly as the resident
        coordinator graph does today.

    ``total=False``: every field is optional — nodes only declare the keys
    they read/write, and ``StateGraph`` merges per-key reducers across all
    node returns. No node is forced to return keys it didn't touch.
    """

    # identity (injected at invoke_turn; the coordinator sub-node also reads these)
    group_id: str
    coordinator_id: str  # the group's Leader agent_id (handoff target for plan/engineering turns)

    # shared message log — add_messages dedups by id (resume-safe)
    messages: Annotated[list[BaseMessage], add_messages]

    # who is currently driving the turn (set by route_entry / handoff edges)
    current_speaker: str

    # coordinator DAG plan (last-write-wins; node returns the full plan)
    dispatch_plan: Annotated[list[dict], replace_value]

    # turn control (reset each invoke_turn; bound handoff chain length + anti-loop)
    turn_count: int
    recent_speakers: Annotated[list[str], append_list]

    # group config (injected per invoke_turn from GroupEntity.config)
    auto_confirm: bool
    leader_strategy: str

    # shared turn memory (appended across handoffs; checkpointer-persisted)
    memory: Annotated[list[dict], append_list]

    # the user/system message that kicked off the turn (route_entry injects)
    incoming_message: str
    incoming_sender: str
    incoming_kind: str  # agent_reply | coordinator_task | coordinator_reply | plan_confirm | ...
    incoming_data: dict | None
