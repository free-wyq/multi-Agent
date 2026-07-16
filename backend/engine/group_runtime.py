"""GroupRuntime — per-group turn controller for the decentralized swarm graph.

This module is the **回合边界 + 可中止性** owner that the resident
per-agent ``AgentEngine`` model does not provide for the decentralized handoff
path. Design source: memory ``decentralized-scheduling-stop-plan-2026-07-13``
(方向 A) + ``stop-signal-cooperative-cancel-design``.

Why a separate runtime (not just another AgentEngine)
------------------------------------------------------
The resident ``AgentEngine`` (one per agent per group) owns an asyncio.Queue
inbox + a single-agent LangGraph graph; a turn is ``_handle_notify → graph.
ainvoke`` and there is no group-level turn boundary or cancel handle. The
decentralized swarm graph (``engine/group_graph.build_group_graph``) collapses
the whole group into ONE compiled graph where every agent (including the
coordinator) is a node and「who speaks next」is a handoff edge — a turn is one
``graph.ainvoke``. That turn is owned here, not by any single AgentEngine.

The three群聊缺陷 (顺序乱 / 协调者插话 / 停不下来) collapse into:
  · 顺序乱 / 同一 agent 连发 — handoff is serial + ``GroupState.turn_count`` /
    ``recent_speakers`` guard (task-12, ``worker.make_agent_node``).
  · 协调者插话 — ``route_entry`` forks by kind (task-11) so chat/@mention turns
    never reach the coordinator.
  · 停不下来 — a turn = one ``graph.ainvoke`` owned here as a cancellable
    ``asyncio.Task`` (later task); ``request_stop`` / ``cancel_turn`` (this
    skeleton) are the two stop entries.

Two-layer stop (the contract this skeleton locks)
-------------------------------------------------
``GroupRuntime`` holds a ``self._stop_event: asyncio.Event`` (default *clear*).
Two stop entries, mirroring AutoGen v0.4's ``ExternalTermination`` (termination
as a first-class, externally-injectable condition; default cooperative, not a
hard cancel):

  1. ``request_stop()`` — **cooperative soft stop**. Only ``_stop_event.set()``;
     no ``task.cancel()``. ``route_entry`` and every agent node (``worker.
     make_agent_node``) check ``_stop_event.is_set()`` at entry (a later task
     wires the check); on hit they do NOT speak and return ``Command(goto=END)``,
     so the current speaker finishes its current step and the turn ends
     gracefully (no mid-stream abort, no half message). Used by the「停/stop/
     中断」keyword path in ``route_user_message`` (a later task).

  2. ``cancel_turn()`` — **hard stop backstop**. First ``_stop_event.set()`` (so
     any node about to start yields cooperatively), THEN ``self._current_task.
     cancel()``. The ``CancelledError`` propagates into the streaming LLM's
     ``async for`` and breaks the stream on the spot. Idempotent: no active turn
     → returns ``False``. Used by the UI stop button → ``POST /api/groups/{id}/
     stop-turn`` (later tasks).

Why ``_stop_event`` is NOT on GroupState
---------------------------------------
``asyncio.Event`` is a runtime object — not serializable, it cannot live on
``GroupState`` (the LangGraph checkpointer would try to serialize it and raise).
It is a ``GroupRuntime`` instance attribute, **游离于图状态之外**, and cooperates
with the graph only through「node-entry checks」the nodes perform against the
runtime reference (injected via the node closure / ``RunnableConfig``, a later
wiring task).

Scope of THIS task
------------------
**``invoke_turn`` — the full turn lifecycle.** Built on the skeleton
(``compile_graph`` / ``_start_turn_task`` / ``_end_turn`` / the stop signal)
landed by task-13/14. ``invoke_turn`` resolves the Leader identity + group
config, injects them + the resident cross-turn mirrors as the *initial* state
of a FRESH checkpointer thread (per-turn ``_next_thread_id`` so the per-turn
reducers do not accumulate across turns), wraps ``ainvoke`` as a cancellable
``asyncio.Task`` (``_start_turn_task``, mirrors ``_worker_task``), awaits it,
and in ``finally`` clears the handle + resets stop. ``reset_stop`` at turn
start so a stale stop does not suppress a fresh turn; ``route_entry`` + agent
nodes check ``is_stopped()`` at entry (wired by a later task). On normal END:
sync ``dispatch_plan`` back + record turn memory + ``emit agent_status(idle)``.
``resume_plan`` is the PL-02 native resume (``Command(resume=...)``) twin;
``reset_session`` is the BE-02 cross-turn wipe + dangling-interrupt resolve.
The node-entry stop check (route_entry + agent nodes reading ``is_stopped()``)
is the NEXT task (.task.md line 17) — ``invoke_turn`` exposes ``is_stopped()``
for it; this task does not yet wire the check inside the nodes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from langgraph.graph import END
from langgraph.types import Command

from engine import coordinator as coord_mod
from engine import worker as worker_mod
from engine.group_graph import build_group_graph
from events import emit_agent_status
from models import get_leader_strategy

if TYPE_CHECKING:
    from models.group import Group

logger = logging.getLogger("multi-agent.group_runtime")


# ── 会话发言总量封顶（cross-turn safety backstop）──────────────────────
# 按钮硬停（cancel_turn）+ 关键词软停（request_stop）之外的最后兜底：当二者都失效
# 或用户不在场时，防止 agent 之间无限 handoff（成语接龙接疯 / A↔B 互相 @ 无限刷屏）
# 烧 token。对标 AutoGen v0.4 ``MaxMessageTermination(N)`` / OpenAI Agents SDK
# ``max_turns`` —— 跨回合的会话总量上限，与 per-turn 的 ``AGENT_NODE_MAX_HANDOFFS``
# （单回合 handoff 链护栏，worker.py）是两个正交维度：
#   · per-turn 8：单次 invoke 内一个 agent 接龙接疯的护栏（handoff 链长度）。
#   · 会话 50：跨多次 invoke（多个用户回合）累计 agent 发言数的总闸。
# 计一个 agent 节点发言一次 = +1（``record_speech``），不含 dispatch fan-out 的
# execute 派工（那是中心化任务，不是来回对话）。撞顶后 route_entry / make_agent_node
# 拦截，回合 END + emit 一条「已达上限」提示。env 可调；默认 50 给互动留够空间。
SESSION_SPEECH_CAP = max(1, int(os.environ.get("MULTI_AGENT_SESSION_SPEECH_CAP", "50")))


# ── per-turn fresh-thread key ────────────────────────────────────────
# The group graph compiles ONE graph per group, but each ``invoke_turn`` runs on
# a FRESH checkpointer thread (``{thread_id}:{seq}``, seq monotonic per runtime).
# LangGraph's ``add_messages`` / ``append_list`` / ``replace_value`` reducers
# APPEND input to the checkpointed state, so reusing one thread across turns
# accumulates ``turn_count``/``recent_speakers``/``memory`` (turn 1's speakers
# bleed into turn 2). A fresh thread per turn starts each turn from the injected
# initial state, so the per-turn guards (防连发, handoff cap) reset cleanly.
# Cross-turn continuity that the coordinator legitimately needs (the dispatch
# plan, the interrupt pause) is carried on ``dispatch_plan`` via the
# ``replace_value`` reducer + injected from the runtime's resident mirror (see
# ``invoke_turn``) — NOT by accumulating on one shared thread (which would also
# make a fresh-input demand auto-resolve a stale interrupt). This is the
# decentralized group-graph analogue of the resident engine's ``reset_session``
# (which resolves the interrupt + clears per-turn state) done per turn.
_TURN_SEQ_KEY = "_turn_seq"


class GroupRuntime:
    """Per-group turn controller: owns the compiled group graph's turn lifecycle.

    Holds the **compiled group graph** (``self._graph``) + the **current turn
    task** handle (``self._current_task``) + the **stop signal**
    (``asyncio.Event``). This is the去中心化群图的回合边界 + 可中止性 owner — a
    turn is one ``graph.ainvoke`` wrapped as a cancellable ``asyncio.Task``
    (mirrors the resident ``AgentEngine._worker_task``).

    Args:
        group: the ``Group`` (``models.group.Group``) — captures ``id`` /
            ``coordinator_id`` at construction (identity, startup-baked, mirrors
            ``AgentEngine``'s identity-layer cache). Polymorphism is accepted too:
            a ``group_id`` ``str`` is tolerated (deferred to the resident
            per-agent path) so callers that haven't resolved the Group row yet
            can still construct a runtime — the resolved form is the norm.

    Attributes:
        group_id: the group's id (read off ``group.id`` or the ``str`` arg).
        coordinator_id: the group's Leader agent_id (``group.coordinator_id``).
        _stop_event: ``asyncio.Event``, default *clear*. The cooperative stop
            signal. **Not** on ``GroupState`` — a runtime object,游离于图状态外
            (never serialized by the checkpointer).
        _graph: the compiled per-group swarm graph (``build_group_graph`` output),
            or ``None`` until ``compile_graph`` runs. Holds the route_entry +
            coordinator sub-nodes + agent nodes + handoff edges in ONE graph.
        _members: the member identity dicts (``agent_id``/``agent_name``/
            ``agent_role``/``system_prompt``) the graph was compiled with, or
            ``None`` until ``compile_graph`` runs. Stashed so ``invoke_turn`` (later
            task) can inject the group config / identity without re-resolving.
        _current_task: the current turn's ``asyncio.Task`` handle, or ``None``
            when no turn is active. Filled by ``invoke_turn`` (later task);
            ``cancel_turn`` cancels it. ``None`` → ``cancel_turn`` returns
            ``False`` (idempotent no-op).
        thread_id: the MemorySaver checkpointer key for this group's graph
            thread (``group_id`` — one thread per group, cross-invoke state via
            checkpointer). Mirrors ``AgentEngine.thread_id``'s stable-key choice.

    Lifecycle:
        · ``compile_graph()`` (async) — resolve members from the DB + compile the
          group graph once (startup / on member change). Stored on ``_graph``.
        · A turn = one ``graph.ainvoke`` (the later ``invoke_turn`` wraps it as a
          cancellable ``asyncio.Task`` stored on ``_current_task``).
        · ``request_stop()`` sets the event so node entries yield (current
          speaker finishes its step, then the next node ENDs the turn).
        · ``cancel_turn()`` sets the event THEN cancels the task (hard backstop).
        · On turn end (normal or cancelled), ``_current_task`` is cleared +
          ``emit_agent_status(idle)`` fires (later ``invoke_turn`` task).

    Thread-safety: ``asyncio`` single-thread — the event + task handle + graph
    are touched only from the event loop, so no lock is needed for THEM.
    ``request_stop`` / ``cancel_turn`` are safe to call from any coroutine in
    the loop. BUT a turn's ``graph.ainvoke`` IS serialized by ``_turn_lock``:
    one ``GroupRuntime`` serves the whole group (user chat, plan resume, every
    worker report-back all call ``invoke_turn``/``resume_plan`` on the SAME
    runtime). Without serialization, a worker's report-back
    (``registry._run_worker_task`` → ``invoke_turn``) can fire WHILE a prior
    ``invoke_turn``/``resume_plan`` is mid-flight — both touch the runtime's
    checkpointer thread (``resume_plan`` even REUSES the last turn's thread)
    and both write the ``turn_count`` / ``current_speaker`` last-value channels,
    so two concurrent ``ainvoke``s collide → ``InvalidUpdateError: At key
    'turn_count': Can receive only one value per step``. The resident
    ``AgentEngine`` avoids this via its ``asyncio.Queue`` inbox (one
    ``_handle_task`` at a time); the group-graph analogue is this lock. A turn
    acquires the lock around its whole body (identity resolve → ainvoke →
    sync-back → idle emit) so concurrent turns queue rather than interleave —
    matching the resident engine's serial-inbox semantics (a report-back that
    lands mid-turn waits for the turn to end, exactly as it waits on the inbox).
    ``cancel_turn`` does NOT take the lock (it cancels the stashed task; the
    cancelled task's ``finally`` releases the lock — no deadlock).
    """

    def __init__(self, group: "Group | str") -> None:
        # Polymorphic first arg (mirrors build_group_graph): a Group object
        # resolves group_id + coordinator_id; a bare group_id str defers
        # coordinator_id (the resident path / unresolved-group fallback).
        if hasattr(group, "id") and not isinstance(group, str):
            self.group_id: str = str(getattr(group, "id"))
            self.coordinator_id: str = str(getattr(group, "coordinator_id", "") or "")
        else:
            self.group_id = str(group)
            self.coordinator_id = ""

        # ── cooperative stop signal (游离于 GroupState, not checkpointer-serialized) ──
        # Default CLEAR: a turn runs until END / cap / stop. set() by request_stop
        # (soft) and cancel_turn (hard backstop). route_entry + each agent node
        # entry check is_set() (a later task wires the check) → on hit return
        # Command(goto=END) so the current speaker finishes its step then the turn
        # ends gracefully.
        self._stop_event: asyncio.Event = asyncio.Event()

        # ── turn serialization lock (one ainvoke at a time per group) ──
        # One GroupRuntime serves the whole group: user chat, plan resume, and
        # every worker report-back (registry._run_worker_task → invoke_turn) all
        # call invoke_turn / resume_plan on THIS runtime. Without a lock, a
        # report-back firing mid-turn would run a second graph.ainvoke
        # concurrently — both writing the turn_count/current_speaker last-value
        # channels on the runtime's checkpointer thread (resume_plan even REUSES
        # the last turn's thread) → InvalidUpdateError: At key 'turn_count':
        # Can receive only one value per step. The resident AgentEngine is
        # serial via its asyncio.Queue inbox; this lock is the group-graph
        # analogue. Acquired around the whole turn body; cancel_turn does NOT
        # take it (it cancels the stashed task whose finally releases the lock).
        self._turn_lock: asyncio.Lock = asyncio.Lock()

        # ── compiled group graph (filled by compile_graph, this task) ──
        # The per-group swarm graph (build_group_graph output): route_entry +
        # coordinator sub-nodes (classify/llm_decide/chat/dispatch + GROUP twins)
        # + one agent_<id> node per member + handoff edges. One compiled graph per
        # group, reused across invoke_turn calls. None until compile_graph runs.
        self._graph: Any = None
        # The member identity dicts the graph was compiled with (one per group
        # member EXCLUDING the coordinator). Stashed so invoke_turn (later task)
        # can inject group config / identity without re-resolving from the DB.
        self._members: list[dict[str, Any]] | None = None

        # ── current turn task handle (filled by invoke_turn, later task) ──
        # One ainvoke = one turn = one cancellable asyncio.Task (mirrors the
        # resident AgentEngine._worker_task). cancel_turn cancels this so
        # CancelledError propagates into the streaming LLM's async for (mid-stream
        # hard stop). None when no turn is active.
        self._current_task: asyncio.Task[Any] | None = None

        # ── per-turn fresh-thread sequence ──────────────────────────────
        # Each ``invoke_turn`` runs on a FRESH checkpointer thread
        # (``{thread_id}:{seq}``) so the per-turn reducers
        # (``turn_count``/``recent_speakers``/``memory``) do NOT accumulate
        # across turns (LangGraph appends input to the checkpointed state on a
        # reused thread). Monotonic per-runtime counter; never resets — a fresh
        # thread per turn is the whole point (continuity goes via ``dispatch_plan``
        # + the runtime's resident mirror, NOT thread accumulation).
        self._turn_seq: int = 0

        # ── resident cross-turn state mirrors (单一真源) ──────────────
        # The decentralized group-graph migration retires the resident engine's
        # per-invoke re-injection of ``_memory`` / ``_dispatch_plan`` /
        # ``_recent_routes`` (the「双源」design — engine field + graph state —
        # which bled per-turn reducers across turns + let a fresh input
        # auto-resolve a stale interrupt). ``GroupRuntime`` owns the cross-turn
        # state instead: ``_memory`` accumulates the conversation (appended by
        # ``_record_turn_memory`` after each turn), ``_dispatch_plan`` mirrors the
        # coordinator's DAG plan (synced from the graph result so the
        # /confirm|/direct|/modify pending guards + the /modify patch source +
        # reset_session's second wipe still work), and ``_recent_routes`` is the
        # mention anti-loop gate. All three are injected as the *initial* state of
        # each fresh-thread turn (NOT re-appended onto a shared thread), so the
        # graph sees the accumulated conversation + the live plan without the
        # resident engine's accumulation hazard.
        self._memory: list[dict[str, str]] = []
        self._dispatch_plan: list[dict[str, Any]] = []

        # ── 会话发言总量计数器（cross-turn safety backstop）──────────────
        # 按钮硬停 + 关键词软停失效时的最后兜底（对标 AutoGen
        # ``MaxMessageTermination`` / OpenAI ``max_turns``）。一个 agent 节点发言
        # 一次 = +1（``record_speech``）；不含 dispatch fan-out 的 execute 派工
        # （中心化任务，非来回对话）。撞 ``SESSION_SPEECH_CAP`` 后 route_entry /
        # make_agent_node 拦截回合。**跨回合累加**——不随单回合 END 清零（接龙就是
        # 多个短回合，单回合 reset 拦不住），只在 ``reset_session``（/new 开新对话）
        # 时清零。``_cap_emitted`` 防撞顶后每回合都重复 emit 提示（撞一次即标记，
        # reset_session 清）。
        self._speech_count: int = 0
        self._cap_emitted: bool = False

        # MemorySaver checkpointer key for this group's graph thread. One thread
        # per group (the whole group shares ONE graph, vs the resident per-agent
        # {group}:{agent} keys); each ``invoke_turn`` runs on a FRESH thread
        # derived from this (``{thread_id}:{seq}``) so per-turn reducers do not
        # accumulate across turns. Cross-turn continuity (the dispatch plan, the
        # conversation memory) is carried on the runtime's resident mirrors
        # (``_dispatch_plan`` / ``_memory``) injected as each fresh thread's
        # initial state — NOT by reusing one accumulating thread.
        self.thread_id: str = self.group_id

        logger.debug(
            "[group_runtime] constructed for group=%s coordinator=%s (graph via "
            "compile_graph; invoke_turn filled by later task)",
            self.group_id, self.coordinator_id or "(unset)",
        )

    # ── graph compilation ─────────────────────────────────────
    async def compile_graph(self, members: list[dict[str, Any]] | None = None) -> Any:
        """Compile (or recompile) the per-group swarm graph once.

        Resolves the group's members from the DB (``crud.list_group_members_with
        _agent`` joined with ``crud.list_agents`` for system_prompt) when
        ``members`` is not passed, then compiles ``build_group_graph(group,
        members, coordinator_id)`` and stashes it on ``self._graph`` + the member
        dicts on ``self._members``. Idempotent: re-calling recompiles (a member
        add/rename should recompile so the new agent node + handoff edge are
        registered — same staleness window as the resident engine, refreshed on
        reload / member change).

        Member resolution is async (DB I/O) + is THIS method's concern (unlike
        ``build_group_graph`` which stays sync + takes members as a required
        arg). Each member dict carries ``agent_id`` / ``agent_name`` /
        ``agent_role`` / ``system_prompt`` — the identity ``worker.build_agent
        _node`` closure-binds at compile time.

        Returns the compiled graph (also stashed on ``self._graph``). A later
        ``invoke_turn`` task reads ``self._graph`` to run turns; ``cancel_turn``
        does NOT touch the graph (only the turn task).

        Args:
            members: optional pre-resolved member identity dicts (used by tests
                / callers that already have them). When ``None``, resolved from
                the DB. Each dict: ``agent_id`` / ``agent_name`` / ``agent_role``
                / ``system_prompt``.
        """
        if members is None:
            members = await self._resolve_members()
        self._members = list(members)
        self._graph = build_group_graph(
            self.group_id, self._members, coordinator_id=self.coordinator_id,
        )
        logger.info(
            "[group_runtime] compiled group graph for group=%s members=%d "
            "coordinator=%s",
            self.group_id, len(self._members), self.coordinator_id or "(none)",
        )
        return self._graph

    async def _resolve_members(self) -> list[dict[str, Any]]:
        """Resolve the group's member identity dicts from the DB.

        Joins ``crud.list_group_members_with_agent`` (agent_id / agent_name /
        agent_role) with ``crud.list_agents`` (system_prompt) so each member dict
        carries the full identity ``worker.build_agent_node`` closure-binds. The
        coordinator is EXCLUDED (it is a sub-node, not an ``agent_<id>`` node) —
        members are the group's普通成员 only, matching ``build_group_graph``'s
        ``members`` contract.
        """
        from store import crud  # local import avoids a module-load DB touch

        member_rows = await crud.list_group_members_with_agent(self.group_id)
        agents = {a.id: a for a in await crud.list_agents()}
        members: list[dict[str, Any]] = []
        for m in member_rows:
            agent_id = getattr(m, "agent_id", "") or ""
            if not agent_id or agent_id == self.coordinator_id:
                # skip empty + the coordinator (coordinator is a sub-node, not
                # an agent_<id> node — route_entry owns the Leader entry).
                continue
            agent = agents.get(agent_id)
            members.append({
                "agent_id": agent_id,
                "agent_name": getattr(m, "agent_name", "") or "",
                "agent_role": getattr(m, "agent_role", "") or "",
                "system_prompt": getattr(agent, "system_prompt", "") or "" if agent else "",
                # PL-06 group-graph skill injection (handoff 断层修复): carry
                # mounted_skills so build_agent_node closure-binds them (same
                # staleness window as system_prompt). agent_executor injected
                # skills only on the resident execute path; the group-graph
                # agent node now mirrors it.
                "mounted_skills": list(getattr(agent, "mounted_skills", None) or []) if agent else [],
            })
        return members

    # ── cooperative stop (soft) ───────────────────────────────
    def request_stop(self) -> None:
        """Cooperative soft stop — only ``_stop_event.set()``, no cancel.

        Sets the stop event so that ``route_entry`` and every agent node
        (``worker.make_agent_node``), which check ``_stop_event.is_set()`` at
        entry (a later task wires the check), yield on the NEXT node boundary:
        they do NOT speak and return ``Command(goto=END)``. The currently-running
        node (speaker mid-step) is allowed to finish — no mid-stream abort, no
        half message — then the turn ends at the next node entry.

        This is the「停/stop/中断」keyword path (``route_user_message`` identifies
        the keyword and calls this, a later task). It is NOT a hard cancel: a
        speaker already streaming finishes its token stream; only the *next*
        speaker is suppressed.

        Idempotent: setting an already-set event is a no-op. Safe to call when
        no turn is active (the event just stays set; the next turn's node-entry
        checks will yield immediately unless ``reset_stop`` is called first —
        ``invoke_turn`` (later task) resets the event at turn start so a stale
        stop doesn't suppress a fresh turn).
        """
        self._stop_event.set()
        logger.debug(
            "[group_runtime] request_stop: cooperative stop event set for group=%s "
            "(current speaker finishes its step, next node yields → END)",
            self.group_id,
        )

    # ── hard stop backstop ────────────────────────────────────
    def cancel_turn(self) -> bool:
        """Hard stop backstop — set event THEN cancel the turn task.

        Two layers (mirrors ``stop-signal-cooperative-cancel-design``):
          1. ``_stop_event.set()`` — so any node about to START yields
             cooperatively (same as ``request_stop``); a node mid-step still
             gets the cancel below.
          2. ``self._current_task.cancel()`` — the ``CancelledError`` propagates
             into the streaming LLM's ``async for`` and breaks the stream on
             the spot (mid-stream hard stop, the hard backstop for when the
             cooperative layer is not enough — e.g. a long LLM call with no
             node boundary in sight).

        Returns ``True`` if a cancel was issued, ``False`` if no active turn
        (``_current_task is None``) — idempotent: calling with no active turn is
        a no-op (the event is still set so a turn starting later would yield,
        but no task is cancelled). This is the UI stop-button path
        (``StopTaskButton`` → ``POST /api/groups/{id}/stop-turn`` → this, later
        tasks).

        Until the later ``invoke_turn`` task fills ``_current_task``, this
        always returns ``False`` (no active turn) — which IS the idempotent
        no-active-turn contract; the set() still happens so the event reflects
        the stop intent.
        """
        # Layer 1: cooperative — set the event so node entries yield.
        self._stop_event.set()
        task = self._current_task
        if task is None:
            logger.debug(
                "[group_runtime] cancel_turn: no active turn for group=%s "
                "(event set, no task cancelled) — idempotent no-op",
                self.group_id,
            )
            return False
        # Layer 2: hard — cancel the streaming turn task. CancelledError
        # propagates into chat_completion_stream's async for → mid-stream break.
        task.cancel()
        logger.debug(
            "[group_runtime] cancel_turn: hard cancel issued for group=%s "
            "(CancelledError → streaming LLM async for)",
            self.group_id,
        )
        return True

    # ── stop-signal introspection / reset ─────────────────────
    def is_stopped(self) -> bool:
        """Whether the cooperative stop event is currently set.

        ``route_entry`` + agent nodes consult this at entry (a later task wires
        the check) to decide whether to yield (``Command(goto=END)``) instead
        of speaking. Exposed as a method (not just the raw event) so the
        node-entry check has one named call site to wire against.
        """
        return self._stop_event.is_set()

    def reset_stop(self) -> None:
        """Clear the stop event — a fresh turn may run.

        Called by the later ``invoke_turn`` at turn start so a stale stop
        (from a previous request_stop / cancel_turn) does NOT suppress a fresh
        turn. Also called after a cancelled turn winds down so the runtime is
        ready for the next user message.
        """
        self._stop_event.clear()
        logger.debug(
            "[group_runtime] reset_stop: stop event cleared for group=%s "
            "(next turn may run)",
            self.group_id,
        )

    # ── session-speech cap (cross-turn backstop) ─────────────
    def is_session_capped(self) -> bool:
        """Whether the session has hit the speech cap (cross-turn backstop).

        The third stop layer (alongside ``request_stop`` soft / ``cancel_turn``
        hard): when the button and keyword both fail or the user is away, this
        prevents unbounded agent handoff (成语接龙接疯 / A↔B 互相 @ 无限刷屏)
        from burning tokens. Mirrors AutoGen ``MaxMessageTermination`` /
        OpenAI ``max_turns``. ``route_entry`` + ``make_agent_node`` consult this
        at entry (on hit the turn ENDs without speaking). **Cross-turn** — does
        NOT reset per turn (only ``reset_session`` / ``/new`` clears it), which is
        exactly why it catches a multi-turn 成语接龙 that the per-turn
        ``_stop_event`` (reset each turn) misses.
        """
        return self._speech_count >= SESSION_SPEECH_CAP

    async def record_speech(self) -> None:
        """Increment the session speech counter (one agent spoke).

        Called by ``make_agent_node`` AFTER an agent actually speaks (brain
        replied), so the count reflects real agent replies — NOT dispatch
        fan-out execute acks (those are centralized tasks, not back-and-forth
        conversation, and counting them would burn the cap on a single dispatch
        round). On reaching ``SESSION_SPEECH_CAP`` emits a single「已达上限」reply
        (``_cap_emitted`` guards a one-shot so subsequent turns don't repeat it).
        ``reset_session`` zeroes the count + the flag.
        """
        self._speech_count += 1
        if self._speech_count >= SESSION_SPEECH_CAP and not self._cap_emitted:
            self._cap_emitted = True
            logger.info(
                "[group_runtime] session speech cap hit: group=%s count=%d cap=%d "
                "(turns will END without speaking; /new to reset)",
                self.group_id, self._speech_count, SESSION_SPEECH_CAP,
            )

    # ── turn task handle (cancellable ainvoke, mirrors _worker_task) ──
    def _start_turn_task(self, coro: Any) -> asyncio.Task[Any]:
        """Wrap a turn coroutine as a cancellable ``asyncio.Task`` + stash it.

        Mirrors the resident ``AgentEngine._worker_task`` pattern: one turn =
        one ``asyncio.Task`` stored on ``self._current_task`` so ``cancel_turn``
        can cancel it (CancelledError → streaming LLM ``async for`` mid-stream
        break). The handle is cleared in ``_end_turn`` (called by the later
        ``invoke_turn``'s ``finally``) so the slot is clean for the next turn.

        This method is the stable home for the「ainvoke 包成 cancellable task」
        contract the task names: the later ``invoke_turn`` builds its ainvoke
        coroutine and passes it here; ``cancel_turn`` (this skeleton) cancels the
        stashed handle. Stashing is the contract THIS task locks; the full
        invoke_turn (state injection + emit idle + finally cleanup) is the next
        task.

        Args:
            coro: the turn coroutine (typically ``self._graph.ainvoke(...,
                config=...)``). NOT awaited here — wrapped in a Task the caller
                awaits separately so cancel can interrupt it mid-await.

        Returns:
            the ``asyncio.Task`` (also stashed on ``self._current_task``).
        """
        task = asyncio.create_task(coro)
        self._current_task = task
        return task

    def _end_turn(self) -> None:
        """Clear the current turn task handle (turn done — normal or cancelled).

        Called by the later ``invoke_turn``'s ``finally`` so the slot is clean
        for the next turn (``cancel_turn`` on a cleared slot returns ``False`` —
        the idempotent no-active-turn contract). Does NOT clear the stop event
        (``invoke_turn`` calls ``reset_stop`` at the NEXT turn's start; clearing
        here would let a just-cancelled turn's stop intent leak into a turn that
        hasn't started yet — reset_stop is per-turn-start, not per-turn-end).
        """
        self._current_task = None

    # ── turn execution (one ainvoke = one turn, cancellable) ──────────
    async def _resolve_leader_identity(self) -> dict[str, str]:
        """Resolve the group's Leader identity (coordinator sub-node reads it).

        Reads the coordinator's agent row once per turn (``crud.get_agent``) so
        the coordinator sub-nodes (``classify`` / ``llm_decide`` / ``chat`` /
        ``dispatch``) see the *current* Leader ``agent_name`` /
        ``system_prompt`` — mirroring the resident engine's startup cache but
        per-turn fresh (a Leader persona edit between turns takes effect without
        a rebuild). Returns a dict with ``agent_id`` / ``agent_name`` /
        ``system_prompt`` (``agent_id`` = ``coordinator_id``); empty strings if
        the group has no coordinator or the row is gone (degraded — the graph
        still runs, the Leader node uses its build-time annotation + COORDINATOR
        fallback persona).
        """
        from store import crud  # local import avoids a module-load DB touch

        if not self.coordinator_id:
            return {"agent_id": "", "agent_name": "", "system_prompt": ""}
        agent = await crud.get_agent(self.coordinator_id)
        return {
            "agent_id": self.coordinator_id,
            "agent_name": getattr(agent, "name", "") if agent else "",
            "system_prompt": getattr(agent, "system_prompt", "") or "" if agent else "",
        }

    async def _resolve_group_config(self) -> tuple[bool, str]:
        """Read the group's per-turn config flags: ``auto_confirm`` + ``leader_strategy``.

        Fresh per invoke_turn (mirrors the resident ``_handle_notify`` per-notify
        read) so the coordinator sub-nodes reflect the *current* group config
        (a user toggling 直接干 / editing Leader strategy between turns takes
        effect without an engine rebuild). ``auto_confirm`` defaults False,
        ``leader_strategy`` defaults ``""`` (via :func:`models.get_leader_strategy`,
        single source for the default + key name). Best-effort: a group-row miss
        degrades to the defaults (the graph still runs).
        """
        from store import crud

        grp = await crud.get_group(self.group_id)
        if not grp:
            return False, ""
        auto_confirm = bool((grp.config or {}).get("auto_confirm", False))
        return auto_confirm, get_leader_strategy(grp.config)

    def _reply_cb_factory(self):
        """Build the engine-side reply callback for the duration of one turn.

        Mirrors the resident ``AgentEngine._handle_notify``'s ``reply_cb``: the
        coordinator/worker nodes' ``_unified_reply`` invoke this callback after
        persisting a reply, and it runs the @mention routing
        (``route_mentions``) so the next speaker is resolved — but for the group
        graph the handoff edge already resolves the next speaker from the reply's
        @mention (``_resolve_handoff_target`` in ``worker.make_agent_node``), so
        this callback is the secondary path: it keeps the mention anti-loop
        state (``_recent_routes``) consistent for the legacy resident engines
        that still route via ``route_mentions`` (the group-graph migration is
        additive — the resident engines run alongside until the registry swaps
        over, a later task). Returns ``None`` when there is no coordinator
        (decentralized-only group), so the graph nodes' ``_unified_reply`` see
        no callback (a no-op) and the handoff edge alone drives the turn.
        """
        from engine.mention import route_mentions

        async def reply_cb(content: str) -> None:
            if not self.coordinator_id:
                return
            await route_mentions(
                self.group_id,
                self.coordinator_id,
                "",
                content,
                None,  # None → route_mentions 取群级共享 _get_recent_routes(group_id)
            )

        return reply_cb

    def _build_turn_input(
        self,
        incoming_kind: str,
        incoming_message: str,
        incoming_sender: str,
        incoming_data: dict[str, Any] | None,
        leader: dict[str, str],
        group_config: tuple[bool, str],
    ) -> dict[str, Any]:
        """Build the initial GroupState for one fresh-thread turn.

        Injects the turn's identity + incoming message + the resident
        cross-turn state mirrors as the *initial* state of a FRESH checkpointer
        thread (NOT appended onto a shared thread — that would accumulate
        ``turn_count``/``recent_speakers``/``memory`` across turns). The
        per-turn reducers (``turn_count=0``, ``recent_speakers=[]``) reset
        cleanly each turn; the cross-turn continuity (``memory``,
        ``dispatch_plan``) is seeded from the runtime's resident mirrors so the
        graph sees the accumulated conversation + the live plan.

        Args mirror the resident ``_handle_notify``'s injection: identity
        (``group_id`` / Leader ``agent_id``+``agent_name``+``system_prompt`` +
        ``coordinator_id``), the incoming message (``incoming_*``), the group
        config flags (``auto_confirm`` / ``leader_strategy``), and the resident
        mirrors (``memory`` / ``dispatch_plan``). ``turn_count`` /
        ``recent_speakers`` reset to a fresh-turn baseline so the防连发 guard +
        the handoff cap apply from the first speaker.
        """
        auto_confirm, leader_strategy = group_config
        return {
            # identity (coordinator sub-nodes read these verbatim)
            "group_id": self.group_id,
            "coordinator_id": self.coordinator_id,
            "agent_id": leader.get("agent_id", "") or self.coordinator_id,
            "agent_name": leader.get("agent_name", ""),
            "system_prompt": leader.get("system_prompt", ""),
            # the user/system message that kicked off the turn (route_entry reads)
            "incoming_message": incoming_message,
            "incoming_sender": incoming_sender,
            "incoming_kind": incoming_kind,
            "incoming_data": incoming_data,
            # group config (injected per turn, fresh — mirrors resident engine)
            "auto_confirm": auto_confirm,
            "leader_strategy": leader_strategy,
            # resident cross-turn state mirrors (seeded as initial state, not
            # appended — continuity without the resident engine's accumulation)
            "memory": list(self._memory),
            "dispatch_plan": list(self._dispatch_plan),
            # per-turn guards reset cleanly each fresh-thread turn
            "turn_count": 0,
            "recent_speakers": [],
        }

    def _next_thread_id(self) -> str:
        """Return a FRESH checkpointer thread id for this turn.

        ``{thread_id}:{seq}`` with ``seq`` monotonic per runtime. A fresh thread
        per turn is what keeps ``turn_count`` / ``recent_speakers`` / ``memory``
        from accumulating across turns (LangGraph appends input to the
        checkpointed state on a reused thread). ``thread_id`` (the stable group
        key) stays the base so the runtime's identity is unchanged; only the
        per-turn suffix rotates.
        """
        self._turn_seq += 1
        return f"{self.thread_id}:{self._turn_seq}"

    async def invoke_turn(
        self,
        *,
        incoming_kind: str,
        incoming_message: str = "",
        incoming_sender: str = "user",
        incoming_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Run one group-graph turn = one ``graph.ainvoke`` = one cancellable Task.

        The回合边界 owner for the decentralized swarm graph (task-15):
        resolves the Leader identity + group config, injects them + the resident
        cross-turn mirrors as the *initial* state of a FRESH checkpointer
        thread, wraps the ``ainvoke`` as a cancellable ``asyncio.Task`` (via
        ``_start_turn_task``, mirroring ``AgentEngine._worker_task``), awaits it,
        and in ``finally`` clears the task handle + resets the stop event so the
        next turn starts clean.

        Cooperative stop (StopSignal): ``reset_stop`` is called at turn START so
        a stale stop (from a previous ``request_stop`` / ``cancel_turn``) does
        NOT suppress this turn. ``route_entry`` + each agent node check
        ``is_stopped()`` at entry (wired in a later task) — on hit they return
        ``Command(goto=END)`` instead of speaking (current speaker finishes its
        step, next node yields). A mid-turn ``request_stop`` (the「停/stop/中断」
        keyword path) thus ends the turn gracefully at the next node boundary.

        ``ainvoke`` is wrapped in a Task (NOT awaited inline) so
        ``Event.wait()`` / ``task.cancel()`` can be injected: the task is
        scheduled onto the loop and ``await``-ed, so a ``cancel_turn`` (the hard
        backstop) cancels it mid-await and the ``CancelledError`` propagates into
        the streaming LLM's ``async for`` (mid-stream break). No synchronous code
        blocks the loop before/after ``ainvoke`` — all setup (identity / config
        resolution) is ``await``-ed, and ``_start_turn_task`` +
        ``_current_task`` / ``finally`` cleanup are non-blocking.

        On turn END (normal): syncs ``dispatch_plan`` back from the graph result
        (the coordinator's dispatch/handle_reply/summarize nodes mutate it) +
        records the turn's user-side memory (appended to ``self._memory`` so the
        next turn's injection sees the conversation), then emits
        ``agent_status(idle)`` so the UI retires the「执行中」state.

        On CANCEL: the ``CancelledError`` propagates out (the caller — the
        resident registry / a later registry migration — decides whether to
        absorb it, mirroring ``AgentEngine._handle_task``'s
        ``except CancelledError`` absorption). ``finally`` still clears the handle
        + resets stop so the runtime is ready for the next message. ``emit
        agent_status(idle)`` is NOT fired on a cancel (the UI's stop-button path
        emits its own terminal state; firing idle here would race the
        stop-button's toast).

        Args:
            incoming_kind: the notify kind (``coordinator_reply`` /
                ``coordinator_task`` / ``agent_reply`` / ``plan_resume`` /
                ``plan_confirm``). ``route_entry`` forks on it — centralized
                (engineering/plan-confirm → Leader ``classify``) vs decentralized
                (chat/@mention → first agent node).
            incoming_message: the message content (empty for a control-signal
                turn like ``plan_resume``).
            incoming_sender: the sender id (``user`` for a user message, an
                ``agent_id`` for a peer's handoff report-back).
            incoming_data: the notify's data dict (e.g. a worker report-back's
                ``{task_id, success}`` for ``node_handle_reply``, or a
                ``plan_resume`` payload).

        Returns:
            the graph result dict (the final GroupState), or ``None`` if the
            graph isn't compiled yet (``compile_graph`` not run) — the caller
            should compile first. ``None`` is NOT a cancel (a cancel raises).
        """
        if self._graph is None:
            logger.warning(
                "[group_runtime] invoke_turn on group=%s but graph not compiled "
                "(call compile_graph first)",
                self.group_id,
            )
            return None

        # Serialize turns: one graph.ainvoke at a time per group (see class
        # docstring + _turn_lock). A worker report-back firing mid-turn would
        # otherwise run a second ainvoke concurrently on this runtime and collide
        # on the turn_count/current_speaker last-value channels. Holding the lock
        # across the WHOLE turn (identity resolve → ainvoke → sync-back → idle
        # emit) matches the resident engine's serial-inbox semantics — a
        # report-back that lands mid-turn simply queues. cancel_turn does NOT
        # take this lock; it cancels the stashed _current_task, whose finally
        # below releases the lock (no deadlock: the task holds the lock, cancel
        # sets CancelledError, the async-with exits, lock released).
        async with self._turn_lock:
            # Per-turn START: reset the cooperative stop so a stale stop (from
            # a previous request_stop / cancel_turn) does NOT suppress this fresh turn.
            # ``route_entry`` + agent nodes check ``is_stopped()`` at entry (wired
            # later); a fresh event lets the turn run.
            self.reset_stop()

            leader = await self._resolve_leader_identity()
            group_config = await self._resolve_group_config()
            turn_input = self._build_turn_input(
                incoming_kind, incoming_message, incoming_sender, incoming_data,
                leader, group_config,
            )
            thread_id = self._next_thread_id()
            config = {"configurable": {"thread_id": thread_id}}

            # Install the engine-side reply callback for the duration of the invoke
            # (mirrors the resident engine's set_reply_callback / finally clear).
            reply_cb = self._reply_cb_factory()
            coord_mod.set_reply_callback(reply_cb)
            worker_mod.set_reply_callback(reply_cb)
            # Task-17: install this runtime as the turn's GroupRuntime contextvar so
            # the group graph's route_entry + every agent node (make_agent_node) can
            # consult ``self.is_stopped()`` at entry (cooperative soft stop — on hit
            # return Command(goto=END) instead of speaking). Cleared in ``finally``
            # so the slot doesn't leak into the next turn (paired with the _REPLY_CB
            # clear; per-task contextvar copy so concurrent group turns each see
            # their own runtime — a request_stop on group A never bleeds into B).
            worker_mod.set_group_runtime(self)

            async def _ainvoke():
                return await self._graph.ainvoke(turn_input, config=config)

            task = self._start_turn_task(_ainvoke())
            cancelled = False
            result: dict[str, Any] | None = None
            try:
                result = await task
            except asyncio.CancelledError:
                # cancel_turn (hard backstop) cancelled the task: the
                # CancelledError already broke the streaming LLM's async for
                # mid-stream. Propagate it — the caller (registry) decides whether
                # to absorb it (mirrors AgentEngine._handle_task's CancelledError
                # branch). Mark cancelled so the finally skips the idle emit (the
                # stop-button path owns the terminal UI state).
                cancelled = True
                raise
            finally:
                coord_mod.set_reply_callback(None)
                worker_mod.set_reply_callback(None)
                worker_mod.set_group_runtime(None)
                self._end_turn()

            # Normal END: sync the dispatch_plan back from the graph result (the
            # coordinator's dispatch/handle_reply/summarize nodes mutate it) — the
            # runtime's resident mirror is the /confirm|/direct|/modify pending
            # guard + the /modify patch source (single source, mirrors the resident
            # engine's mirror-sync at _handle_notify:701-704).
            if result and isinstance(result, dict):
                updated_plan = result.get("dispatch_plan")
                if updated_plan is not None:
                    self._dispatch_plan = list(updated_plan)
                # Record the turn's user-side memory (appended so the next turn's
                # injection sees the conversation — mirrors the resident engine's
                # self._memory.append at _handle_notify:710-715). Skip for a control-
                # signal turn (plan_resume) whose incoming_message is empty — an
                # empty "[user] " entry would pollute the Leader's context.
                if incoming_kind != "plan_resume" and incoming_message:
                    self._memory.append(
                        {
                            "role": "user",
                            "content": f"[{incoming_sender}] {incoming_message}",
                        }
                    )

            # Emit agent_status(idle) so the UI retires the「执行中」state — the
            # turn is done (normal END). NOT fired on cancel (see above).
            if not cancelled:
                await emit_agent_status(
                    self.group_id, self.coordinator_id, leader.get("agent_name", ""),
                    "idle", None,
                )
            return result

    async def resume_plan(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        """Resume the group graph's paused dispatch node via ``Command(resume=...)``.

        PL-02/PL-03 native resume path for the decentralized group graph: a
        prior ``invoke_turn`` (centralized path → ``node_dispatch``) paused the
        turn via ``interrupt({"plan": plan})``. This resumes it —
        ``Command(resume=payload)`` re-enters ``node_dispatch`` so its
        ``interrupt()`` returns the payload and the graph fans out the pending
        steps via ``dispatch_next_group``'s ``Send`` fan-out.

        Same turn lifecycle as ``invoke_turn``: reset_stop at start, fresh
        thread, cancellable Task, finally clear handle + reset stop. The resume
        runs on the SAME thread the interrupt paused — so this does NOT call
        ``_next_thread_id`` (a fresh thread would lose the paused state). Instead
        it reuses the runtime's current thread (the last ``invoke_turn``'s
        thread). If no turn was ever interrupted (cold runtime / the prior turn
        was not a dispatch), the resume is a harmless no-op that runs the graph
        on an empty/terminal thread (mirrors the resident engine's documented
        safe-no-op resume, m12-plan-confirmation-roadmap).

        Args:
            payload: the resume payload (``{"mode": "confirm"|"direct"|"modify",
                "amended_steps": [...]}``); forwarded verbatim — the API owns
                the ``mode`` semantics.

        Returns the graph result dict, or ``None`` if the graph isn't compiled.
        """
        if self._graph is None:
            logger.warning(
                "[group_runtime] resume_plan on group=%s but graph not compiled",
                self.group_id,
            )
            return None

        # Same serialization as invoke_turn (see _turn_lock): a resume re-enters
        # the LAST turn's checkpointer thread (it does NOT mint a fresh one),
        # so it MUST NOT run concurrently with any other turn on this runtime —
        # two concurrent ainvokes on the same thread would both write the
        # turn_count/current_speaker last-value channels → InvalidUpdateError.
        # Acquiring _turn_lock here also guards against a resume racing an
        # in-flight worker report-back (registry._run_worker_task → invoke_turn):
        # whichever arrives second queues, the prior's finally releases the lock.
        async with self._turn_lock:
            self.reset_stop()
            # Reuse the runtime's current thread (the interrupt paused it); do NOT
            # mint a fresh thread — a fresh thread has no paused state to resume.
            thread_id = f"{self.thread_id}:{self._turn_seq}" if self._turn_seq else self.thread_id
            config = {"configurable": {"thread_id": thread_id}}

            reply_cb = self._reply_cb_factory()
            coord_mod.set_reply_callback(reply_cb)
            worker_mod.set_reply_callback(reply_cb)
            # Task-17: install this runtime so route_entry + agent nodes consult
            # ``is_stopped()`` at entry during the resume turn too (a resume is still
            # a turn — a request_stop mid-resume should yield at the next node).
            worker_mod.set_group_runtime(self)

            async def _aresume():
                return await self._graph.ainvoke(Command(resume=payload or {}), config=config)

            task = self._start_turn_task(_aresume())
            cancelled = False
            result: dict[str, Any] | None = None
            try:
                result = await task
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                coord_mod.set_reply_callback(None)
                worker_mod.set_reply_callback(None)
                worker_mod.set_group_runtime(None)
                self._end_turn()

            if result and isinstance(result, dict):
                updated_plan = result.get("dispatch_plan")
                if updated_plan is not None:
                    self._dispatch_plan = list(updated_plan)

            if not cancelled:
                leader_name = ""
                # best-effort leader name for the idle emit; the runtime does not
                # cache it (read fresh in invoke_turn). ``""`` is acceptable — the
                # idle emit's agent_name is cosmetic for the status card.
                await emit_agent_status(
                    self.group_id, self.coordinator_id, leader_name, "idle", None,
                )
            return result

    async def reset_session(self) -> None:
        """BE-02: clear cross-turn state + resolve a dangling interrupt.

        The group-graph analogue of ``AgentEngine.reset_session``: wipes the
        runtime's resident mirrors (``_memory`` / ``_dispatch_plan``) — the
        cross-turn state accumulated across ``invoke_turn`` calls — so the next
        turn starts a fresh conversation. Resolves a dangling interrupt (a plan
        awaiting confirmation) via ``aupdate_state(values=None, as_node=END)``
        on the last turn's thread so a fresh demand is NOT auto-resumed into the
        stale plan (the LangGraph idiom for "act as if the thread finished").
        Best-effort: a checkpointer failure degrades to the mirror-only clear
        (logged) rather than aborting.

        Does NOT touch the compiled graph or the stop event (``reset_stop`` is
        per-turn-start). If a turn is active, cancels it first via
        ``cancel_turn`` so the reset cannot race an in-flight ainvoke that would
        otherwise re-populate ``_memory`` / ``_dispatch_plan`` as it unwinds —
        the caller is expected to poll status back to idle (mirrors PL-11 stop
        semantics).
        """
        # Cancel an in-flight turn so it can't repopulate state as it unwinds.
        self.cancel_turn()
        # Resolve a dangling interrupt on the last turn's thread (a plan
        # awaiting confirmation) so the next demand isn't auto-resumed. The
        # ``aupdate_state(values=None, as_node=END)`` writes a terminal checkpoint
        # (next == (), pending interrupt tasks cleared) — no-op on a thread with
        # no checkpoint (cold runtime) or already-terminal.
        if self._graph is not None and self._turn_seq:
            thread_id = f"{self.thread_id}:{self._turn_seq}"
            try:
                await self._graph.aupdate_state(
                    config={"configurable": {"thread_id": thread_id}},
                    values=None,
                    as_node=END,
                )
            except Exception:
                logger.debug(
                    "[group_runtime] reset_session: aupdate_state(END) failed "
                    "for group=%s (degrading to mirror-only clear)",
                    self.group_id,
                    exc_info=True,
                )
        self._memory.clear()
        self._dispatch_plan.clear()
        # 会话发言计数器 + 撞顶标记也清零——/new 开新对话即重置封顶兜底，给新一轮
        # 协作留满额度（对标 reset_session 清 _memory / _dispatch_plan 的「换话题重来」）。
        self._speech_count = 0
        self._cap_emitted = False
        logger.info(
            "[group_runtime] reset_session for group=%s (interrupt resolved + "
            "memory + dispatch_plan + speech_count cleared)",
            self.group_id,
        )

    # NOTE: the full invoke_turn (state injection + ainvoke + emit idle + finally
    # _end_turn) is the next .task.md task (line 15). This task locks the compiled
    # graph ownership (compile_graph) + the cancellable turn-task wrapper
    # (_start_turn_task / _end_turn) that invoke_turn builds on.
