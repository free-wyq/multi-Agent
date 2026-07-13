"""Per-group swarm StateGraph — agent nodes + handoff edges (decentralized turn).

Replaces ``mention.py``'s handwritten @mention routing (30s anti-loop +
reverse-key clear + ``_A2A_CAP``) with ONE compiled LangGraph per group where
every agent is a node and「who speaks next」is decided by a handoff edge
(an @mention parsed from the current speaker's reply → ``goto`` the target
agent node; no @mention → ``END`` ends the turn).

Design source: memory ``decentralized-scheduling-stop-plan-2026-07-13``
(方向 A — LangGraph-native handoff). The three群聊缺陷 collapse into the
graph topology:

  · 顺序乱 / 同一 agent 连发 — handoff is serial (one node runs at a time),
    and ``GroupState.turn_count`` + ``recent_speakers`` guard the same agent
    not driving twice. No inbox-queue race.
  · 协调者每轮插话 — the coordinator is NOT in the decentralized turn's
    handoff graph; ``route_entry`` routes @mention/chat turns to the first
    agent, never invoking the Leader unless the turn is engineering/plan-
    confirm (the coordinator-sub-node migration is a LATER task; until then
    ``build_group_graph`` registers agent nodes only, and the coordinator
    engine continues to handle its turns via its resident graph).
  · 停不下来 — a turn = one ``graph.ainvoke`` (cancellable task, owned by
    ``GroupRuntime`` in a later task); ``AGENT_NODE_MAX_HANDOFFS`` caps the
    handoff chain length in-graph.

Task says「agent 节点用 ``create_handoff`` 注册合法 handoff 边」. The real
``langgraph_swarm`` API is ``create_handoff_tool`` (task-name shorthand — see
memory ``langgraph-swarm-dependency-added``: ``create_handoff`` does not exist
in any released version). ``create_handoff_tool`` returns a ``BaseTool`` whose
``metadata["__handoff_destination"]`` records the合法 handoff target agent
name; calling the tool returns ``Command(goto=agent_name, update={...})``.

Our worker agents do NOT use tool-calling for handoff — the brain LLM emits a
natural-language reply (e.g.「接龙龙 @后端工程师」) and the agent node parses
the @mention itself (``worker._resolve_handoff_target``) returning
``Command(goto="agent_<peer>")`` directly. So we use ``create_handoff_tool``
as the **registry of合法 handoff edges**: each agent node is declared as a
legal handoff destination via a ``create_handoff_tool`` whose
``__handoff_destination`` metadata matches the node name
(``agent_<agent_id>``). This:

  1. Makes the set of合法 handoff targets a single declarative source (the
     list of handoff tools built at graph-compile time), so an agent can
     only ``goto`` a node that was registered — no typo-driven ``KeyError``,
     no handoff to a non-existent agent.
  2. Lets ``get_handoff_destinations`` introspect the compiled graph for the
     legal edge set (used by the contract test + future route_entry
     validation).
  3. Preserves the option to wire tool-based handoff (binding the handoff
     tools to a tool-calling agent) in a LATER task without a schema change.

The @mention → ``goto`` resolution lives in ``worker._resolve_handoff_target``
(single source for @-token scan + three-tier match + the four guards: self-
skip, coordinator-skip, none→END, first-wins). This module wires that into a
graph + validates the resolved ``goto`` target is a registered handoff edge
(else END, defensive — ``_resolve_handoff_target`` already skips unresolvable
mentions, but this is a belt-and-suspenders against a stale member list).

NOTE — coordinator migration is a LATER task. Until ``coordinator.py``'s
classify/llm_decide/chat/dispatch nodes are ported into the group graph
(later .task.md lines), ``build_group_graph`` wires ONLY the agent (member)
nodes + ``route_entry`` + handoff edges. The coordinator's
engineering/plan-confirm turns continue to run on the resident coordinator
graph (the registry routes them there until the migration lands). The
``route_entry`` node here handles the decentralized path (chat / @mention →
first agent; no agent → END, since with no coordinator node wired there is
no Leader to fall back to in this graph).
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from engine.state import GroupState
from engine.worker import (
    AGENT_NODE_MAX_HANDOFFS,
    _resolve_handoff_target,
    build_agent_node,
)
from langgraph_swarm import create_handoff_tool

logger = logging.getLogger("multi-agent.group_graph")

# Node-name convention (shared with worker.build_agent_node's goto target):
# ``agent_<agent_id>``. LangGraph forbids ':' and '|' in node names (reserved),
# so the underscore separator is the wire format between this module's
# ``route_entry``/handoff targets and worker's ``Command(goto=...)``.
AGENT_NODE_PREFIX = "agent_"


def agent_node_name(agent_id: str) -> str:
    """Canonical group-graph node name for an agent: ``agent_<agent_id>``.

    Single source for the node-name convention so ``build_group_graph`` (node
    registration), ``route_entry`` (goto target), and
    ``worker.make_agent_node`` (handoff goto) can't drift.
    """
    return f"{AGENT_NODE_PREFIX}{agent_id}"


def _build_handoff_tools(member_agent_ids: list[str]) -> list:
    """Declare each member as a合法 handoff destination via ``create_handoff_tool``.

    Returns one ``create_handoff_tool`` per member agent_id, with
    ``agent_name`` = the canonical node name (``agent_<id>``) so the tool's
    ``__handoff_destination`` metadata records the合法 goto target that
    ``worker.make_agent_node`` emits via ``Command(goto=...)``.

    These tools are NOT bound to a tool-calling agent in this first cut (our
    agents hand off by parsing @mentions from natural-language replies, not by
    emitting tool calls). They serve as the declarative registry of合法
    handoff edges — ``get_handoff_destinations`` can introspect them, and a
    later task can bind them to a tool node if we switch to tool-based
    handoff.
    """
    tools = []
    for agent_id in member_agent_ids:
        node_name = agent_node_name(agent_id)
        tools.append(
            create_handoff_tool(
                agent_name=node_name,
                # Tool name mirrors the swarm convention (transfer_to_<node>),
                # but is cosmetic here — no tool-calling agent invokes it yet.
                name=f"transfer_to_{node_name}",
                description=(
                    f"Hand off the turn to the agent node '{node_name}' "
                    f"(agent_id={agent_id}). The current speaker's reply "
                    f"@mentions this agent, so the group graph transfers "
                    f"control to them as the next speaker."
                ),
            )
        )
    return tools


def handoff_destinations(handoff_tools: list) -> set[str]:
    """Return the set of合法 goto targets declared by the handoff tools.

    Reads each tool's ``__handoff_destination`` metadata (single source: the
    ``create_handoff_tool`` call set it). Used by ``route_entry`` / the
    contract test to validate a resolved next-speaker node name is a
    registered handoff edge — a defensive guard so a stale member list can't
    make an agent ``goto`` a non-existent node (which would raise at runtime).
    """
    dests: set[str] = set()
    for tool in handoff_tools:
        meta = getattr(tool, "metadata", None) or {}
        dest = meta.get("__handoff_destination")
        if dest:
            dests.add(dest)
    return dests


async def route_entry(state: GroupState) -> Command:
    """Entry node: decide the first speaker for this turn.

    Decentralized path (this graph): parse the incoming message's @mention
    for the first speaker. If a member is @mentioned → ``goto`` that agent
    node (the turn's first speaker). If no @mention resolves → ``goto END``
    (no agent to drive the turn in the decentralized graph).

    Rationale for「no @mention → END」(NOT「→ coordinator」): until the
    coordinator-sub-node migration lands (later task), the group graph has
    no coordinator node wired. Engineering / plan-confirm turns are routed
    to the resident coordinator engine by the registry (``route_user_message``
    + ``route_after_classify``), NOT through this graph. So when this graph
    IS invoked, it is the decentralized chat/接龙 path, and a no-@mention
    user message with no resolvable target genuinely ends (话筒落地) —
    re-routing to the coordinator here would re-introduce the「协调者每轮
    插话」defect this graph exists to eliminate.

    The resolved first-speaker node name is validated against the graph's
    legal handoff destinations (declared via ``create_handoff_tool``); an
    unresolvable or unregistered target falls back to END (defensive).

    Bumps ``turn_count`` and seeds ``recent_speakers`` so the turn-count cap
    (``AGENT_NODE_MAX_HANDOFFS``) and the「same agent not driven twice」guard
    apply from the very first speaker.
    """
    group_id = state.get("group_id", "")
    coordinator_id = state.get("coordinator_id", "") or ""
    incoming_message = state.get("incoming_message", "") or ""
    incoming_sender = state.get("incoming_sender", "") or ""

    # The handoff destinations are baked into the compiled graph (set at
    # build time via _build_handoff_tools). route_entry reads them off the
    # graph's config-free closure: stash the set on the state lazily? No —
    # the graph builder passes the legal-target set into route_entry via a
    # closure (build_route_entry), so route_entry itself is closure-bound
    # (mirrors build_agent_node binding identity). The default route_entry
    # here (no closure) resolves + validates against the caller-provided
    # ``_handoff_targets`` if present, else trusts _resolve_handoff_target's
    # own guards (it already skips unresolvable + coordinator targets).
    handoff_targets: set[str] = state.get("_handoff_targets") or set()  # type: ignore[assignment]

    next_speaker = await _resolve_handoff_target(
        group_id, coordinator_id, incoming_sender, incoming_message,
    )

    turn_count = (state.get("turn_count") or 0) + 1
    # Reached the in-graph handoff cap before any agent even spoke? End the
    # turn (defensive — route_entry itself counts as one handoff step).
    if turn_count >= AGENT_NODE_MAX_HANDOFFS:
        logger.debug(
            "[group_graph] route_entry turn_count=%d reached cap=%d, end turn",
            turn_count, AGENT_NODE_MAX_HANDOFFS,
        )
        return Command(goto=END, update={"turn_count": turn_count})

    if next_speaker is None:
        # No resolvable @mention in the incoming message → decentralized
        # turn has no first speaker → END (话筒落地). The coordinator engine
        # handles no-@mention engineering turns on its own resident graph;
        # this graph is only invoked for the decentralized path.
        return Command(goto=END, update={"turn_count": turn_count})

    target_node = agent_node_name(next_speaker)
    if handoff_targets and target_node not in handoff_targets:
        # Defensive: the resolved speaker is not a registered handoff
        # destination (stale member list between build + invoke). End rather
        # than raise — the user's message still landed (route_user_message
        # persisted it before invoking the graph).
        logger.debug(
            "[group_graph] route_entry resolved %s but %s not a registered "
            "handoff target; end turn",
            next_speaker, target_node,
        )
        return Command(goto=END, update={"turn_count": turn_count})

    return Command(
        goto=target_node,
        update={
            "current_speaker": next_speaker,
            "turn_count": turn_count,
            "recent_speakers": [next_speaker],
        },
    )


def build_route_entry(handoff_targets: set[str]):
    """Closure-bind the legal handoff target set into ``route_entry``.

    The compiled graph is read-only post-build, so the handoff destinations
    (declared via ``create_handoff_tool`` at build time) are captured here.
    ``route_entry`` then validates a resolved next-speaker against this set
    without reading it off the runtime state (cleaner than stashing a private
    ``_handoff_targets`` key on GroupState, which would pollute the schema).
    """
    async def _route_entry(state: GroupState) -> Command:
        group_id = state.get("group_id", "")
        coordinator_id = state.get("coordinator_id", "") or ""
        incoming_message = state.get("incoming_message", "") or ""
        incoming_sender = state.get("incoming_sender", "") or ""

        next_speaker = await _resolve_handoff_target(
            group_id, coordinator_id, incoming_sender, incoming_message,
        )

        turn_count = (state.get("turn_count") or 0) + 1
        if turn_count >= AGENT_NODE_MAX_HANDOFFS:
            logger.debug(
                "[group_graph] route_entry turn_count=%d reached cap=%d, end turn",
                turn_count, AGENT_NODE_MAX_HANDOFFS,
            )
            return Command(goto=END, update={"turn_count": turn_count})

        if next_speaker is None:
            return Command(goto=END, update={"turn_count": turn_count})

        target_node = agent_node_name(next_speaker)
        if target_node not in handoff_targets:
            logger.debug(
                "[group_graph] route_entry resolved %s but %s not a registered "
                "handoff target; end turn",
                next_speaker, target_node,
            )
            return Command(goto=END, update={"turn_count": turn_count})

        return Command(
            goto=target_node,
            update={
                "current_speaker": next_speaker,
                "turn_count": turn_count,
                "recent_speakers": [next_speaker],
            },
        )

    _route_entry.__name__ = "route_entry"
    return _route_entry


def build_group_graph(
    group_id: str,
    members: list[dict[str, Any]],
    coordinator_id: str = "",
) -> Any:
    """Compile the per-group swarm StateGraph.

    Args:
        group_id: the group this graph is compiled for (carried in state).
        members: list of member identity dicts (``agent_id`` / ``agent_name``
            / ``agent_role`` / ``system_prompt``), one per group member EXCLUDING
            the coordinator (the coordinator sub-node migration is a later
            task; until then the coordinator engine handles its own turns).
            Each member becomes an ``agent_<id>`` node built via
            ``worker.build_agent_node``.
        coordinator_id: the group's Leader agent_id — passed into each agent
            node so ``_resolve_handoff_target`` can skip handing off back to
            the Leader (decentralized path: workers don't @mention the
            coordinator; ``route_entry`` owns Leader entry for engineering
            turns, which currently still run on the resident coordinator
            graph).

    Wires:
      · ``route_entry`` (closure-bound with the legal handoff target set) as
        the START node — parses the incoming @mention → first speaker, else
        END.
      · one ``agent_<id>`` node per member (``worker.build_agent_node``) —
        each speaks then returns ``Command(goto="agent_<peer>")`` (handoff)
        or ``Command(goto=END)`` (话筒落地). Handoff edges are DYNAMIC (the
        ``Command.goto`` target is decided at runtime from the reply's
        @mention), so no static inter-agent edges are added — LangGraph
        follows the ``Command.goto`` to whatever node name the agent emits.
      · a ``create_handoff_tool`` per member declares the legal handoff
        destinations (registry of合法 goto targets, validated by
        ``route_entry`` + introspectable via ``get_handoff_destinations``).

    Returns the compiled graph with a MemorySaver checkpointer (mirrors the
    resident coordinator/worker graphs — cross-invoke state via thread_id).

    The graph is compiled once per group and reused across ``invoke_turn``
    calls (later task). Member identity is captured at build time; a later
    member add/rename requires a recompile (same staleness window as the
    resident engine, refreshed on reload).
    """
    g: StateGraph = StateGraph(GroupState)

    member_agent_ids: list[str] = [m["agent_id"] for m in members if m.get("agent_id")]
    handoff_tools = _build_handoff_tools(member_agent_ids)
    legal_targets = handoff_destinations(handoff_tools)

    # route_entry: parse incoming @mention → first speaker (or END).
    g.add_node("route_entry", build_route_entry(legal_targets))

    # Coordinator sub-nodes (task: coordinator.py 把 classify/llm_decide/chat 节点
    # 改造为群图内 coordinator 子节点). The centralized path (engineering / plan-
    # confirm turns) runs through these nodes inside the SAME group graph, sharing
    # ``GroupState`` with the agent (member) nodes. The node functions are the
    # resident coordinator's (``coordinator.node_*``) reused unchanged — they read
    # every state key via duck-typed ``state.get(...)`` / ``state[...]``, and
    # ``GroupState`` is a schema union over ``CoordinatorState`` (this task ported
    # ``agent_id``/``agent_name``/``system_prompt``/``action_taken``/``reply_content``
    # /``_stream_stats`` onto ``GroupState``), so ``state["agent_id"]`` resolves to
    # the Leader in both graphs. Registering them here is the wiring step the task
    # names; the node bodies + the ``route_after_*`` conditional-edge semantics are
    # preserved verbatim (a later task adds the conditional edges + the route_entry
    # fan-out that routes engineering/plan-confirm turns here).
    from engine.coordinator import build_coordinator_subnodes  # local import avoids cycle
    coord_nodes = build_coordinator_subnodes(
        coordinator_id=coordinator_id,
        coordinator_name="",
        system_prompt="",
    )
    for name in ("classify", "llm_decide", "chat", "dispatch",
                 "handle_reply", "dispatch_next", "summarize"):
        g.add_node(name, coord_nodes[name])

    # one agent node per member. build_agent_node closure-binds identity so
    # the compiled node knows who it is speaking as (mirrors AgentEngine
    # caching self.agent_id / self.name / self.role / self.system_prompt).
    for m in members:
        agent_id = m["agent_id"]
        node = build_agent_node(
            agent_id=agent_id,
            agent_name=m.get("agent_name", ""),
            agent_role=m.get("agent_role", ""),
            system_prompt=m.get("system_prompt", "") or "",
            coordinator_id=coordinator_id,
        )
        g.add_node(agent_node_name(agent_id), node)

    g.add_edge(START, "route_entry")

    compiled = g.compile(checkpointer=MemorySaver())
    # Stash the handoff tools + legal-target set on the compiled graph for
    # introspection (contract tests + future route_entry validation). These
    # are read-only post-compile attributes, safe to attach.
    compiled._handoff_tools = handoff_tools  # type: ignore[attr-defined]
    compiled._legal_handoff_targets = legal_targets  # type: ignore[attr-defined]
    compiled._group_id = group_id  # type: ignore[attr-defined]
    compiled._member_agent_ids = member_agent_ids  # type: ignore[attr-defined]
    compiled._coordinator_id = coordinator_id  # type: ignore[attr-defined]
    compiled._has_coordinator_subnodes = True  # type: ignore[attr-defined]
    logger.info(
        "[group_graph] compiled graph for group=%s members=%d handoff_targets=%d "
        "coordinator_subnodes=on",
        group_id, len(member_agent_ids), len(legal_targets),
    )
    return compiled
