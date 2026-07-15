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
  · 协调者每轮插话 — ``route_entry`` forks by message kind: engineering / plan-
    confirm turns (``coordinator_reply`` / ``plan_resume`` kind, or an explicit
    plan-confirm cue) go to the Leader's ``classify`` node (centralized path);
    chat / ``@人`` turns go to the first @mentioned agent node (decentralized
    path), so the coordinator is NOT reached on a chat turn. A member @mention
    always wins over the kind — ``@前端工程师 重构登录`` hands the turn to 前端
    even though it reads like engineering work.

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
from engine import worker as worker_mod
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


# Turn kinds that route_entry fans out to the CENTRALIZED coordinator path
# (the Leader's classify→llm_decide→dispatch/handle_reply subgraph). The
# coordinator engine owns engineering-demand + plan-confirm turns; a user who
# @mentions a member opts into the decentralized path instead (handled below).
_CENTRAL_KINDS = frozenset({
    "coordinator_reply",   # user demand with no @mention → Leader
    "coordinator_task",     # synthetic demand from the execute path → Leader
    "plan_resume",         # PL-02 resume payload → dispatch node directly
    "plan_confirm",        # legacy defensive plan-confirm marker
})


def _looks_central(incoming_kind: str, message: str) -> bool:
    """Decide whether a turn enters the centralized coordinator path.

    ``incoming_kind`` is the primary signal (set by ``route_user_message`` /
    the registry before invoke): ``coordinator_reply`` / ``coordinator_task``
    / ``plan_resume`` / ``plan_confirm`` mean the turn is an engineering-demand
    or a plan-confirmation, which the Leader owns. An ``agent_reply`` kind (a
    peer's handoff) or an absent kind (a fresh user chat) is decentralized.

    NOTE — ``agent_reply`` is dual-meaning: a *peer handoff* (no ``task_id``,
    decentralized → returns ``False`` here) vs a *worker report-back* (carries
    ``incoming_data.task_id``, CENTRAL → must reach ``handle_reply_group``).
    The report-back case is handled by ``_is_report_back`` in ``route_entry``
    as an early return (this function is only consulted AFTER that check, so
    it never sees a report-back — it always returns ``False`` for the
    ``agent_reply`` kind, which is correct for the peer-handoff case that
    reaches it). The ``agent_reply`` → ``False`` contract here is locked by
    test_vh39 A3/C10 for the peer-handoff path.

    A lightweight heuristic backs up the kind: an explicit engineering/plan cue
    in the message ("确认执行" / "确认计划" / "修改计划" / "直接执行" + an
    imperative engineering verb with no @mention) routes to the Leader even when
    the kind is absent. The heuristic only ADDS central routing — it never
    overrides a member @mention (the explicit ``@人`` below wins), so a user who
    ``@前端工程师 重构登录`` still hands the turn to 前端 directly.
    """
    if incoming_kind in _CENTRAL_KINDS:
        return True
    if incoming_kind == "agent_reply":
        return False  # a peer handoff is decentralized by definition
    # heuristic on bare user chat (no kind / "user_input"): plan-confirm cues
    # + a non-@mentioned engineering imperative route to the Leader. Member
    # @mention is resolved by the caller BEFORE consulting this flag, so the
    # explicit ``@人`` always wins (decentralized).
    m = message or ""
    if any(cue in m for cue in ("确认执行", "确认计划", "修改计划", "直接执行", "直接干")):
        return True
    return False


def _is_report_back(state: GroupState) -> bool:
    """An execute-path worker report-back: ``agent_reply`` kind carrying a ``task_id``.

    ``agent_reply`` is dual-meaning on the inbound path:

      · **report-back** (has ``incoming_data.task_id``) — a worker finished a
        dispatched step and is reporting completion/failure to the coordinator.
        This is unambiguously CENTRAL: the coordinator owns the dispatch plan +
        MT-15 failure recovery + MT-14 step adjustment, so the turn must reach
        ``classify`` → ``handle_reply_group``. Routed here, NOT to a peer agent
        node.
      · **peer handoff** (no ``task_id``) — an A2A chat handoff (成语接龙 /
        discussion) where one member @mentions another. Decentralized → the
        @mentioned agent node. ``_looks_central`` keeps this ``False``.

    The ``task_id`` presence is the sole discriminator — a report-back message
    ("步骤完成：…\\n结果：…") typically carries no @mention, so without this
    check route_entry would END the turn at the no-@mention fall-through and
    the dispatched step would stay ``dispatched`` forever (split-brain: the
    group-graph Send fan-out mutated ``rt._dispatch_plan`` but no turn ever
    marks the step completed). ``_looks_central`` itself is NOT changed (its
    ``agent_reply`` → ``False`` contract is locked by test_vh39 A3/C10 for the
    peer-handoff case); this check lives in route_entry as an early return that
    fires only when a ``task_id`` is present.
    """
    if state.get("incoming_kind") != "agent_reply":
        return False
    data = state.get("incoming_data") or {}
    return bool(data.get("task_id"))


async def route_entry(state: GroupState) -> Command:
    """Entry node: fork the turn by message kind — centralized vs decentralized.

    Two paths from START (task-11: route_entry 按消息类型分叉):

      · **Centralized path** (engineering / plan-confirm): ``goto="classify"``
        — the Leader's coordinator subgraph (classify→llm_decide→dispatch /
        handle_reply / summarize) owns engineering-demand and plan-confirmation
        turns. Entered when ``incoming_kind`` is ``coordinator_reply`` /
        ``coordinator_task`` / ``plan_resume`` / ``plan_confirm``, or a bare
        user chat carrying a plan-confirm cue. The coordinator sub-nodes read
        ``GroupState`` (task-6 schema union) exactly as the resident coordinator
        graph reads ``CoordinatorState``, so the centralized path runs in-graph
        sharing the same state as the agent nodes.
      · **Decentralized path** (chat / @mention): ``goto="agent_<id>"`` — a
        member is @mentioned (or a peer's ``agent_reply`` hands control over),
        so the first agent node drives the turn and hands off via ``@mention``
        in its reply. The Leader is NOT reached on this path — exactly the
        「协调者每轮插话」defect this graph exists to eliminate. No resolvable
        ``@mention`` on this path → ``goto END`` (话筒落地).

    **Member @mention wins over the kind**: a user who ``@前端工程师 重构登录``
    explicitly hands the turn to 前端 — the ``@人`` opt-in is honored even when
    the message reads like an engineering demand (mirrors ``route_user_message``
    first-mention-wins). Only a *bare* (no @mention) engineering/plan cue routes
    to the Leader. This keeps the coordinator off the decentralized chat path
    while still funneling real engineering work to it.

    Bumps ``turn_count`` on both paths so the per-turn cap
    (``AGENT_NODE_MAX_HANDOFFS``) + the「same agent not driven twice」guard
    apply from the very first speaker.

    NOTE: the standalone ``route_entry`` here mirrors ``build_route_entry``'s
    closure-bound node (used by ``build_group_graph``). It resolves the member
    @mention itself (via ``_resolve_handoff_target``) and trusts the kind. The
    closure-bound variant (registered in the compiled graph) additionally
    validates the resolved goto target against the build-time legal-handoff set.
    """
    group_id = state.get("group_id", "")
    coordinator_id = state.get("coordinator_id", "") or ""
    incoming_message = state.get("incoming_message", "") or ""
    incoming_sender = state.get("incoming_sender", "") or ""
    incoming_kind = state.get("incoming_kind", "") or ""
    # The handoff destinations are baked into the compiled graph at build time
    # via _build_handoff_tools. The standalone route_entry reads them off a
    # caller-provided ``_handoff_targets`` state key if present; the
    # closure-bound build_route_entry captures the set directly (cleaner —
    # avoids polluting GroupState with a private key).
    handoff_targets: set[str] = state.get("_handoff_targets") or set()  # type: ignore[assignment]

    # ── 协作式停止守卫（StopSignal·task-17）────────────────
    # Same cooperative-stop check as the closure-bound ``build_route_entry``:
    # ``GroupRuntime.request_stop()`` set the event (soft stop), so this entry
    # node does NOT pick a speaker + returns ``Command(goto=END)`` (current
    # speaker already finished, the turn ends gracefully). ``None`` runtime →
    # skip (backward compatible — the standalone route_entry is used by tests
    # that don't install a runtime). Kept in sync with the closure-bound twin.
    rt = worker_mod.get_group_runtime()
    if rt is not None and rt.is_stopped():
        logger.debug(
            "[group_graph] route_entry 协作式停止守卫命中（standalone）：stop_event 已 set，"
            "不选发言者，回合 END",
        )
        return Command(goto=END, update={"turn_count": state.get("turn_count") or 0})

    # ── execute-path report-back 中心化分叉（item④前置·修 split-brain）──
    # ``agent_reply`` 携带 ``incoming_data.task_id`` = worker 跑完派工步骤的回报，
    # 必须走中心化路径 → classify → handle_reply_group（MT-15 失败恢复 + MT-14
    # 步骤调整 + 把该 step 标 completed/failed）。这里早返回，不解析 @mention——
    # 回报消息（"步骤完成：…\n结果：…"）通常无 @mention，否则会落到下方的
    # no-@mention→END 分支，派工步骤永远停在 dispatched，rt._dispatch_plan 死锁
    # （split-brain：群图 Send 扇出改了 rt._dispatch_plan 但无回合标完成）。
    # 区分键：有 task_id = 中心化回报；无 task_id = 去中心化 peer handoff（走下方
    # _resolve_handoff_target）。``_looks_central`` 的 agent_reply→False 契约不动
    # （test_vh39 A3/C10 锁 peer-handoff 路径），report-back 由本早返回接管。
    if _is_report_back(state):
        logger.debug(
            "[group_graph] route_entry execute-path report-back 命中（standalone）："
            "agent_reply + task_id=%s → 中心化 classify（handle_reply_group）",
            (state.get("incoming_data") or {}).get("task_id"),
        )
        turn_count = (state.get("turn_count") or 0) + 1
        return Command(goto="classify", update={"turn_count": turn_count})

    turn_count = (state.get("turn_count") or 0) + 1
    # Reached the in-graph handoff cap before any agent even spoke? End the
    # turn (defensive — route_entry itself counts as one handoff step).
    if turn_count >= AGENT_NODE_MAX_HANDOFFS:
        logger.debug(
            "[group_graph] route_entry turn_count=%d reached cap=%d, end turn",
            turn_count, AGENT_NODE_MAX_HANDOFFS,
        )
        return Command(goto=END, update={"turn_count": turn_count})

    # Member @mention FIRST — an explicit ``@人`` opts into the decentralized
    # path even when the message reads like engineering work. ``_resolve_handoff
    # _target`` already skips self-mentions + the coordinator (workers do not
    # hand off back to the Leader via @mention on the decentralized path).
    next_speaker = await _resolve_handoff_target(
        group_id, coordinator_id, incoming_sender, incoming_message,
    )
    if next_speaker is not None:
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
        # route_entry picks the first speaker but does NOT seed recent_speakers:
        # the agent node appends itself to recent_speakers when it speaks (its own
        # update), so the防连发守卫 sees an empty list on the first speaker's
        # FIRST invocation (allows speech) and the speaker's id only on a SECOND
        # invocation (suppresses). Seeding recent_speakers here would make the
        # guard fire on the first speaker's very first call (false positive).
        return Command(
            goto=target_node,
            update={
                "current_speaker": next_speaker,
                "turn_count": turn_count,
            },
        )

    # No member @mention → route by kind. Centralized kinds + plan-confirm cues
    # go to the Leader's classify node; everything else (bare chat / agent
    # handoff with no further @mention) ends the turn (话筒落地).
    if _looks_central(incoming_kind, incoming_message):
        return Command(goto="classify", update={"turn_count": turn_count})

    # No @mention + not central → decentralized turn has no first speaker → END.
    # The coordinator is NOT reached here: a bare chat message with no @mention
    # and no engineering cue genuinely ends (话筒落地), and re-routing to the
    # coordinator would re-introduce the「协调者每轮插话」defect.
    return Command(goto=END, update={"turn_count": turn_count})


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
        incoming_kind = state.get("incoming_kind", "") or ""

        # ── 协作式停止守卫（StopSignal·task-17）────────────────
        # ``GroupRuntime.request_stop()``（用户喊「停/stop/中断」）只 set 一个
        # ``asyncio.Event``（软停·不强切）。route_entry 是回合入口节点，命中 stop
        # 即不选发言者、直接 ``Command(goto=END)`` 结束回合——上一发言者已把当前
        # step 跑完，本回合话筒落地，不 mid-stream 强切、不留半截消息。
        # 这是双层停止的软停层；硬停层 ``cancel_turn``（先 set 再 task.cancel）由 UI
        # 停止按钮走，与本守卫正交。runtime 经 ``worker.get_group_runtime()`` 从
        # contextvar 取（``GroupRuntime.invoke_turn`` 在 ainvoke 前 set，finally 清）。
        # ``None``（群图在 GroupRuntime 之外被 invoke / 测试直调）→ 守卫跳过，
        # route_entry 按原逻辑分叉（向后兼容）。
        rt = worker_mod.get_group_runtime()
        if rt is not None and rt.is_stopped():
            logger.debug(
                "[group_graph] route_entry 协作式停止守卫命中：stop_event 已 set"
                "（用户喊停），不选发言者，回合 END（上一发言者已说完，不 mid-stream 强切）",
            )
            return Command(goto=END, update={"turn_count": state.get("turn_count") or 0})

        # ── execute-path report-back 中心化分叉（item④前置·修 split-brain）──
        # 与 standalone route_entry 同款：agent_reply + incoming_data.task_id =
        # worker 跑完派工步骤的回报 → 中心化 classify → handle_reply_group。不解析
        # @mention（回报消息无 @）。无 task_id 的 agent_reply = peer handoff（去中心化），
        # 走下方 _resolve_handoff_target。两份 route_entry 须同步（vh40 锁）。
        if _is_report_back(state):
            logger.debug(
                "[group_graph] route_entry execute-path report-back 命中："
                "agent_reply + task_id=%s → 中心化 classify（handle_reply_group）",
                (state.get("incoming_data") or {}).get("task_id"),
            )
            turn_count = (state.get("turn_count") or 0) + 1
            return Command(goto="classify", update={"turn_count": turn_count})

        turn_count = (state.get("turn_count") or 0) + 1
        if turn_count >= AGENT_NODE_MAX_HANDOFFS:
            logger.debug(
                "[group_graph] route_entry turn_count=%d reached cap=%d, end turn",
                turn_count, AGENT_NODE_MAX_HANDOFFS,
            )
            return Command(goto=END, update={"turn_count": turn_count})

        # Member @mention FIRST — explicit ``@人`` opts into the decentralized
        # path even when the message reads like engineering work.
        next_speaker = await _resolve_handoff_target(
            group_id, coordinator_id, incoming_sender, incoming_message,
        )
        if next_speaker is not None:
            target_node = agent_node_name(next_speaker)
            if target_node not in handoff_targets:
                logger.debug(
                    "[group_graph] route_entry resolved %s but %s not a registered "
                    "handoff target; end turn",
                    next_speaker, target_node,
                )
                return Command(goto=END, update={"turn_count": turn_count})
            # route_entry picks the first speaker but does NOT seed recent_speakers:
            # the agent node appends itself when it speaks, so the防连发守卫 sees an
            # empty list on the first speaker's FIRST invocation (allows speech) and
            # the speaker's id only on a SECOND invocation (suppresses). Seeding here
            # would make the guard fire on the first speaker's very first call.
            return Command(
                goto=target_node,
                update={
                    "current_speaker": next_speaker,
                    "turn_count": turn_count,
                },
            )

        # No member @mention → route by kind (centralized vs decentralized end).
        if _looks_central(incoming_kind, incoming_message):
            return Command(goto="classify", update={"turn_count": turn_count})
        return Command(goto=END, update={"turn_count": turn_count})

    _route_entry.__name__ = "route_entry"
    return _route_entry


def build_group_graph(
    group,
    members: list[dict[str, Any]] | None = None,
    coordinator_id: str = "",
) -> Any:
    """Compile the per-group swarm StateGraph.

    Task: ``group_graph.py build_group_graph(group) 装配 START→route_entry→
    {coordinator|agent}+handoff 边，编译通过``. Assembles the complete group
    topology in ONE compiled graph:

      START → route_entry → { coordinator subgraph (classify→…)
                            | agent_<id> nodes (handoff) }
                            → END

    Args (polymorphic first arg — task name is ``build_group_graph(group)``):
        group: either a group_id ``str`` (the original 3-arg form, used by the
            contract tests) OR a ``Group`` object (``models.group.Group``) —
            when a Group object is passed, ``group_id`` is read off ``group.id``
            and ``coordinator_id`` off ``group.coordinator_id`` (unless
            explicitly overridden by the 3rd arg). This lets a future
            ``GroupRuntime`` call ``build_group_graph(group, members)`` after
            resolving members from the DB (member resolution stays async + is
            the caller's job — the builder itself stays sync, mirroring
            ``build_coordinator_graph`` / ``build_worker_graph``).
        members: list of member identity dicts (``agent_id`` / ``agent_name``
            / ``agent_role`` / ``system_prompt``), one per group member EXCLUDING
            the coordinator. Each becomes an ``agent_<id>`` node built via
            ``worker.build_agent_node``. Required (the builder does not resolve
            members from the DB — that is async + the caller's concern).
        coordinator_id: the group's Leader agent_id. When ``group`` is a Group
            object this defaults to ``group.coordinator_id``; pass an explicit
            value to override. Passed into each agent node so
            ``_resolve_handoff_target`` can skip handing off back to the Leader
            (decentralized path: workers don't @mention the coordinator).

    Wires:
      · ``START → route_entry`` (static edge).
      · ``route_entry`` (closure-bound with the legal handoff target set) —
        parses the incoming message → first speaker. Returns ``Command(goto=...)``
        so it can dynamically reach EITHER an ``agent_<id>`` node (decentralized
        chat/接龙 path, @mention-resolved) OR the coordinator entry ``classify``
        (centralized engineering/plan-confirm path). The *when* (which message
        kind → which path) is the route_entry fan-out logic (a later task); this
        task wires the topology so BOTH targets are reachable. No-@mention → END.
      · coordinator subgraph — the centralized path runs IN this graph, sharing
        ``GroupState`` with the agent nodes. Uses the GROUP twins
        (``dispatch_next_group`` / ``handle_reply_group`` / ``summarize_group``)
        for the fan-out / report-back / summary nodes so the centralized path
        fans out via LangGraph ``Send`` to the agent nodes (not ``push_task`` to
        worker inboxes — the group-graph topology has no separate worker
        engines, agents ARE nodes) + receives agent reports in-graph via
        ``Command(goto="handle_reply_group")`` (not the inbox notify loop). The
        shared resident nodes (``classify`` / ``llm_decide`` / ``chat`` /
        ``dispatch``) return dicts and route via the ``route_after_*``
        conditional edges (semantics preserved verbatim):
          classify   → route_after_classify → {dispatch_next_group, handle_reply_group, llm_decide}
          llm_decide → route_after_llm_decide → {chat, dispatch}
          dispatch   → route_after_dispatch   → {dispatch_next_group, END}
          chat       → END
        The twin nodes return ``Command(goto=...)`` so no conditional edge is
        wired after them (LangGraph follows the ``Command.goto``).
      · one ``agent_<id>`` node per member (``worker.build_agent_node``) — each
        speaks then returns ``Command(goto="agent_<peer>")`` (handoff) or
        ``Command(goto=END)``. Handoff edges are DYNAMIC (``Command.goto`` target
        decided at runtime from the reply's @mention), so no static inter-agent
        edges are added.
      · a ``create_handoff_tool`` per member declares the legal handoff
        destinations (registry of合法 goto targets, validated by ``route_entry``).

    Returns the compiled graph with a MemorySaver checkpointer (mirrors the
    resident coordinator/worker graphs — cross-invoke state via thread_id).

    The graph is compiled once per group and reused across ``invoke_turn``
    calls (later task). Member identity is captured at build time; a later
    member add/rename requires a recompile (same staleness window as the
    resident engine, refreshed on reload).
    """
    # Polymorphic first arg: Group object vs group_id str (task: build_group_graph(group)).
    if hasattr(group, "id") and not isinstance(group, str):
        group_id: str = str(getattr(group, "id"))
        if not coordinator_id:
            coordinator_id = str(getattr(group, "coordinator_id", "") or "")
    else:
        group_id = str(group)
    if members is None:
        members = []

    g: StateGraph = StateGraph(GroupState)

    member_agent_ids: list[str] = [m["agent_id"] for m in members if m.get("agent_id")]
    handoff_tools = _build_handoff_tools(member_agent_ids)
    legal_targets = handoff_destinations(handoff_tools)

    # route_entry: parse incoming @mention → first speaker (or END). Closure-bound
    # with the legal handoff target set so a resolved goto target is validated.
    g.add_node("route_entry", build_route_entry(legal_targets))

    # Coordinator sub-graph (centralized path). Uses the GROUP twins
    # (dispatch_next_group / handle_reply_group / summarize_group) so the
    # centralized path fans out via LangGraph Send to the agent nodes (not
    # push_task to inboxes) + receives agent reports in-graph. The shared
    # resident nodes (classify / llm_decide / chat / dispatch) return dicts and
    # route via the route_after_* conditional edges (semantics preserved).
    from engine.coordinator import build_coordinator_subnodes  # local import avoids cycle
    coord_nodes = build_coordinator_subnodes(
        coordinator_id=coordinator_id,
        coordinator_name="",
        system_prompt="",
    )
    # shared resident nodes (dict-returning, conditional-edge routed)
    g.add_node("classify", coord_nodes["classify"])
    g.add_node("llm_decide", coord_nodes["llm_decide"])
    g.add_node("chat", coord_nodes["chat"])
    g.add_node("dispatch", coord_nodes["dispatch"])
    # group twins (Command-returning — drive the next hop via Command.goto,
    # no conditional edge after them)
    g.add_node("dispatch_next_group", coord_nodes["dispatch_next_group"])
    g.add_node("handle_reply_group", coord_nodes["handle_reply_group"])
    g.add_node("summarize_group", coord_nodes["summarize_group"])

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
            mounted_skills=m.get("mounted_skills") or None,
        )
        g.add_node(agent_node_name(agent_id), node)

    # ── edges ────────────────────────────────────────────────
    # START → route_entry (the turn entry — decides coordinator vs agent path).
    g.add_edge(START, "route_entry")

    # Coordinator sub-graph conditional edges (route_after_* semantics preserved
    # verbatim — the routers read state["action_taken"] only, which exists on
    # GroupState). The path map routes the resident routers' return strings to
    # the GROUP twin nodes (dispatch_next→dispatch_next_group, etc.) so the
    # centralized path uses Send fan-out + in-graph report-back.
    g.add_conditional_edges(
        "classify",
        coord_nodes["route_after_classify"],
        {
            "dispatch_next": "dispatch_next_group",
            "handle_reply": "handle_reply_group",
            "llm_decide": "llm_decide",
        },
    )
    g.add_conditional_edges(
        "llm_decide",
        coord_nodes["route_after_llm_decide"],
        {"chat": "chat", "dispatch": "dispatch"},
    )
    g.add_conditional_edges(
        "dispatch",
        coord_nodes["route_after_dispatch"],
        {"dispatch_next": "dispatch_next_group", END: END},
    )
    # chat (resident, dict-returning) → END. The twin nodes
    # (dispatch_next_group / handle_reply_group / summarize_group) return
    # Command(goto=...) so they need no outgoing edge — LangGraph follows the
    # Command.goto to the agent nodes (Send fan-out) / summarize_group / END.
    g.add_edge("chat", END)

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
