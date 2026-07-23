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
    ``asyncio.Task`` (later task); ``cancel_turn`` (this module) is the stop
    entry (hard stop), complemented by the ``SESSION_SPEECH_CAP`` cross-turn cap.

Stop (Option B: two entries, no cooperative soft-stop layer)
------------------------------------------------------------
Option B removed the cooperative soft-stop layer (``request_stop`` /
``is_stopped`` / ``reset_stop`` / ``_stop_event``) — its only producer was the
inbound stop-keyword path (Option B·①), now deleted, so the soft-stop trio was
dead code. Stopping now has two entries only:

  1. ``cancel_turn()`` — **hard stop**. Pure ``self._current_task.cancel()``;
     the ``CancelledError`` propagates into the streaming LLM's ``async for``
     and breaks the stream on the spot (mid-stream). Idempotent: no active turn
     → returns ``False``. Used by the UI stop button → ``POST /api/groups/{id}/
     stop-turn``. ``task.cancel`` already covers the fan-out sibling node window
     (CancelledError propagates through the Send fan-out), so the deleted event
     does not regress coverage.

  2. ``SESSION_SPEECH_CAP`` (``is_session_capped`` / ``record_speech``) — the
     cross-turn backstop (Option B kept): when the button fails or the user is
     away, this caps total agent replies per session (default 50, env-tunable)
     to prevent unbounded handoff (成语接龙接疯) burning tokens.

``_stop_event`` is gone (Option B·③): no cooperative event, no per-turn reset.
The contextvar ``worker.set_group_runtime`` / ``get_group_runtime`` is RETAINED
— ``record_speech`` / ``is_session_capped`` still consult the runtime via it.

Scope of THIS task
------------------
**``invoke_turn`` — the full turn lifecycle.** Built on the skeleton
(``compile_graph`` / ``_start_turn_task`` / ``_end_turn`` / ``cancel_turn``)
landed by task-13/14. ``invoke_turn`` resolves the Leader identity + group
config, injects them + the resident cross-turn mirrors as the *initial* state
of a FRESH checkpointer thread (per-turn ``_next_thread_id`` so the per-turn
reducers do not accumulate across turns), wraps ``ainvoke`` as a cancellable
``asyncio.Task`` (``_start_turn_task``, mirrors ``_worker_task``), awaits it,
and in ``finally`` clears the handle. On normal END: sync ``dispatch_plan``
back + record turn memory + ``emit agent_status(idle)``. ``resume_plan`` is the
PL-02 native resume (``Command(resume=...)``) twin; ``reset_session`` is the
BE-02 cross-turn wipe + dangling-interrupt resolve.
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
from models import get_collaboration_mode, get_leader_strategy

if TYPE_CHECKING:
    from models.group import Group

logger = logging.getLogger("multi-agent.group_runtime")


# ── 会话发言总量封顶（cross-turn safety backstop）──────────────────────
# 按钮硬停（cancel_turn）之外的最后兜底：当按钮失效或用户不在场时，防止 agent 之间
# 无限 handoff（成语接龙接疯 / A↔B 互相 @ 无限刷屏）烧 token。对标 AutoGen v0.4
# ``MaxMessageTermination(N)`` / OpenAI Agents SDK ``max_turns`` —— 跨回合的会话总量
# 上限，与 per-turn 的 ``AGENT_NODE_MAX_HANDOFFS``（单回合 handoff 链护栏，worker.py）
# 是两个正交维度：
#   · per-turn 8：单次 invoke 内一个 agent 接龙接疯的护栏（handoff 链长度）。
#   · 会话 50：跨多次 invoke（多个用户回合）累计 agent 发言数的总闸。
# 计一个 agent 节点发言一次 = +1（``record_speech``），不含 dispatch fan-out 的
# execute 派工（那是中心化任务，不是来回对话）。撞顶后 route_entry / make_agent_node
# 拦截，回合 END + emit 一条「已达上限」提示。env 可调；默认 50 给互动留够空间。
# Option B 后停止只剩两入口：cancel_turn 硬切 + 本 50 封顶（软停 request_stop 已删）。
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
    task** handle (``self._current_task``). This is the去中心化群图的回合边界 +
    可中止性 owner — a turn is one ``graph.ainvoke`` wrapped as a cancellable
    ``asyncio.Task`` (mirrors the resident ``AgentEngine._worker_task``).

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
        · ``cancel_turn()`` cancels the stashed task (hard stop, mid-stream break).
        · On turn end (normal or cancelled), ``_current_task`` is cleared +
          ``emit_agent_status(idle)`` fires (later ``invoke_turn`` task).

    Thread-safety: ``asyncio`` single-thread — the task handle + graph are
    touched only from the event loop, so no lock is needed for THEM.
    ``cancel_turn`` is safe to call from any coroutine in the loop. BUT a turn's
    ``graph.ainvoke`` IS serialized by ``_turn_lock``: one ``GroupRuntime``
    serves the whole group (user chat, plan resume, every worker report-back all
    call ``invoke_turn``/``resume_plan`` on the SAME runtime). Without
    serialization, a worker's report-back (``registry._run_worker_task`` →
    ``invoke_turn``) can fire WHILE a prior ``invoke_turn``/``resume_plan`` is
    mid-flight — both touch the runtime's checkpointer thread (``resume_plan``
    even REUSES the last turn's thread) and both write the ``turn_count`` /
    ``current_speaker`` last-value channels, so two concurrent ``ainvoke``s
    collide → ``InvalidUpdateError: At key 'turn_count': Can receive only one
    value per step``. The resident ``AgentEngine`` avoids this via its
    ``asyncio.Queue`` inbox (one ``_handle_task`` at a time); the group-graph
    analogue is this lock. A turn acquires the lock around its whole body
    (identity resolve → ainvoke → sync-back → idle emit) so concurrent turns
    queue rather than interleave — matching the resident engine's serial-inbox
    semantics (a report-back that lands mid-turn waits for the turn to end,
    exactly as it waits on the inbox). ``cancel_turn`` does NOT take the lock
    (it cancels the stashed task; the cancelled task's ``finally`` releases the
    lock — no deadlock).
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
        carries the full identity ``worker.build_agent_node`` closure-binds.

        Coordinator handling is **collaboration-mode conditional** (做法 A 图级
        二选一):

          · ``centralized`` (default): coordinator EXCLUDED — it is a sub-node
            (``classify``/``llm_decide``/…), NOT an ``agent_<id>`` node. Members
            are the group's普通成员 only, matching ``build_group_graph``'s
            ``members`` contract. This is the historical behaviour (supervisor
            subgraph owns engineering-demand + plan-confirm turns).
          · ``decentralized``: coordinator INCLUDED as an普通 member with an
            ``agent_<coordinator_id>`` node (no编排权 — route_entry forks裸消息
            to END, not to classify). 纯 swarm 范式：群里无群主概念，裸消息话筒
            落地 END，@群主 合法 handoff。The coordinator still carries its
            system_prompt (resolved via ``_resolve_leader_identity`` for the
            name/prompt, then merged in here as a member dict).

        The mode is read fresh from the DB (group config) so a mode toggle
        takes effect on the next ``compile_graph`` (which is what
        ``recompile_group`` triggers — the caller owns the recompile cadence).
        """
        from store import crud  # local import avoids a module-load DB touch

        # 读取 collaboration_mode（每回合现读，与 _resolve_group_config 同源）。
        # decentralized 模式下 coordinator 纳入 members 建其 agent 节点（无编排权）；
        # centralized 维持排除（coordinator 走 supervisor 子图）。
        grp = await crud.get_group(self.group_id)
        mode = get_collaboration_mode(grp.config if grp else None)

        member_rows = await crud.list_group_members_with_agent(self.group_id)
        agents = {a.id: a for a in await crud.list_agents()}
        members: list[dict[str, Any]] = []
        for m in member_rows:
            agent_id = getattr(m, "agent_id", "") or ""
            if not agent_id:
                continue
            # centralized 模式排除 coordinator（coordinator 是子图节点不是 agent 节点，
            # route_entry owns the Leader entry）。decentralized 模式不排除——coordinator
            # 纳入 members 建其 agent 节点（@群主 合法 handoff，无编排权）。
            if mode == "centralized" and agent_id == self.coordinator_id:
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

        # decentralized 模式：coordinator 不在 member_rows 里（它是群主不是成员），
        # 但要把它纳入 members 建其 agent 节点。从 agents 表取其 identity（name/role/
        # system_prompt/mounted_skills），以「普通 member」身份加入（无编排权）。
        if mode == "decentralized" and self.coordinator_id:
            coord_agent = agents.get(self.coordinator_id)
            if coord_agent is not None and not any(
                m["agent_id"] == self.coordinator_id for m in members
            ):
                members.append({
                    "agent_id": self.coordinator_id,
                    "agent_name": getattr(coord_agent, "name", "") or "",
                    "agent_role": getattr(coord_agent, "role", "") or "",
                    "system_prompt": getattr(coord_agent, "system_prompt", "") or "",
                    "mounted_skills": list(getattr(coord_agent, "mounted_skills", None) or []),
                })
        return members

    # ── hard stop (cancel_turn) ──────────────────────────────
    def cancel_turn(self) -> bool:
        """Hard stop — cancel the active turn task (pure ``task.cancel``).

        Option B·③: the cooperative soft-stop layer (``request_stop`` /
        ``is_stopped`` / ``reset_stop`` / ``_stop_event``) was removed — its only
        producer was the inbound stop-keyword path (Option B·①, deleted), so the
        soft-stop trio was dead code. ``cancel_turn`` is now a pure hard stop:
        ``self._current_task.cancel()``. The ``CancelledError`` propagates into
        the streaming LLM's ``async for`` and breaks the stream on the spot
        (mid-stream). ``task.cancel`` already covers the fan-out sibling node
        window (CancelledError propagates through the Send fan-out), so the
        deleted event does not regress coverage.

        Returns ``True`` if a cancel was issued, ``False`` if no active turn
        (``_current_task is None``) — idempotent: calling with no active turn is
        a no-op. This is the UI stop-button path (``StopTaskButton`` → ``POST
        /api/groups/{id}/stop-turn`` → this).
        """
        task = self._current_task
        if task is None:
            logger.debug(
                "[group_runtime] cancel_turn: no active turn for group=%s "
                "(no task cancelled) — idempotent no-op",
                self.group_id,
            )
            return False
        # Hard stop: cancel the streaming turn task. CancelledError propagates
        # into chat_completion_stream's async for → mid-stream break.
        task.cancel()
        logger.debug(
            "[group_runtime] cancel_turn: hard cancel issued for group=%s "
            "(CancelledError → streaming LLM async for)",
            self.group_id,
        )
        return True

    # ── session-speech cap (cross-turn backstop) ─────────────
    def is_session_capped(self) -> bool:
        """Whether the session has hit the speech cap (cross-turn backstop).

        The stop backstop alongside ``cancel_turn`` (Option B: two stop entries
        — the button hard-stop + this cross-turn cap). When the button fails or
        the user is away, this prevents unbounded agent handoff (成语接龙接疯 /
        A↔B 互相 @ 无限刷屏) from burning tokens. Mirrors AutoGen
        ``MaxMessageTermination`` / OpenAI ``max_turns``. ``route_entry`` +
        ``make_agent_node`` consult this at entry (on hit the turn ENDs without
        speaking). **Cross-turn** — does NOT reset per turn (only
        ``reset_session`` / ``/new`` clears it), which is exactly why it catches
        a multi-turn 成语接龙.
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

        Called by ``invoke_turn``'s ``finally`` so the slot is clean for the
        next turn (``cancel_turn`` on a cleared slot returns ``False`` — the
        idempotent no-active-turn contract).
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

    async def _resolve_group_config(self) -> tuple[bool, str, str]:
        """Read the group's per-turn config flags: ``auto_confirm`` + ``leader_strategy`` + ``collaboration_mode``.

        Fresh per invoke_turn (mirrors the resident ``_handle_notify`` per-notify
        read) so the coordinator sub-nodes reflect the *current* group config
        (a user toggling 直接干 / editing Leader strategy / switching collaboration
        mode between turns takes effect without an engine rebuild).
        ``auto_confirm`` defaults False, ``leader_strategy`` defaults ``""`` (via
        :func:`models.get_leader_strategy`, single source for the default + key
        name), ``collaboration_mode`` defaults ``"centralized"`` (via
        :func:`models.get_collaboration_mode`). Best-effort: a group-row miss
        degrades to the defaults (the graph still runs).
        """
        from store import crud

        grp = await crud.get_group(self.group_id)
        if not grp:
            return False, "", "centralized"
        auto_confirm = bool((grp.config or {}).get("auto_confirm", False))
        return auto_confirm, get_leader_strategy(grp.config), get_collaboration_mode(grp.config)

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
        group_config: tuple[bool, str, str],
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
        config flags (``auto_confirm`` / ``leader_strategy`` /
        ``collaboration_mode``), and the resident mirrors (``memory`` /
        ``dispatch_plan``). ``turn_count`` / ``recent_speakers`` reset to a
        fresh-turn baseline so the防连发 guard + the handoff cap apply from the
        first speaker.
        """
        auto_confirm, leader_strategy, collaboration_mode = group_config
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
            "collaboration_mode": collaboration_mode,
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
        converge: bool = False,
    ) -> dict[str, Any] | None:
        """Run one group-graph turn = one ``graph.ainvoke`` = one cancellable Task.

        The回合边界 owner for the decentralized swarm graph (task-15):
        resolves the Leader identity + group config, injects them + the resident
        cross-turn mirrors as the *initial* state of a FRESH checkpointer
        thread, wraps the ``ainvoke`` as a cancellable ``asyncio.Task`` (via
        ``_start_turn_task``, mirroring ``AgentEngine._worker_task``), awaits it,
        and in ``finally`` clears the handle so the next turn starts clean.

        ``ainvoke`` is wrapped in a Task (NOT awaited inline) so
        ``task.cancel()`` can be injected: the task is scheduled onto the loop
        and ``await``-ed, so a ``cancel_turn`` (the hard stop) cancels it
        mid-await and the ``CancelledError`` propagates into the streaming LLM's
        ``async for`` (mid-stream break). No synchronous code blocks the loop
        before/after ``ainvoke`` — all setup (identity / config resolution) is
        ``await``-ed, and ``_start_turn_task`` + ``_current_task`` / ``finally``
        cleanup are non-blocking.

        On turn END (normal): syncs ``dispatch_plan`` back from the graph result
        (the coordinator's dispatch/handle_reply/summarize nodes mutate it) +
        records the turn's user-side memory (appended to ``self._memory`` so the
        next turn's injection sees the conversation), then emits
        ``agent_status(idle)`` so the UI retires the「执行中」state.

        On CANCEL: the ``CancelledError`` propagates out (the caller — the
        resident registry / a later registry migration — decides whether to
        absorb it, mirroring ``AgentEngine._handle_task``'s
        ``except CancelledError`` absorption). ``finally`` still clears the
        handle so the runtime is ready for the next message. ``emit
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
            converge: @收束 回合收敛 (task ``converge-turn-design``). ``True`` =
                a 收束回合——the @mentioned agent replies once then ENDs without
                handoff (``make_agent_node`` forces ``next_speaker=None`` on
                ``state["converge"]``). Only meaningful on the decentralized
                @mention path; injected into the initial state so the agent node
                sees it. Default ``False`` (normal turns handoff as usual). The
                caller (``route_user_message``) must reject a 收束 turn with no
                @mention (收束必须 @ 收口对象) before invoking.

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
            leader = await self._resolve_leader_identity()
            group_config = await self._resolve_group_config()
            turn_input = self._build_turn_input(
                incoming_kind, incoming_message, incoming_sender, incoming_data,
                leader, group_config,
            )
            # @收束 回合收敛（converge-turn-design）：注入 converge 标志到初始 state，
            # 让 make_agent_node 末端读到后强制 next_speaker=None → 回一句即 END 不 handoff。
            # 仅去中心化 @mention 路径有意义（route_user_message 在无 @ 时拒绝收束）。
            if converge:
                turn_input["converge"] = True
            thread_id = self._next_thread_id()
            config = {"configurable": {"thread_id": thread_id}}

            # Install the engine-side reply callback for the duration of the invoke
            # (mirrors the resident engine's set_reply_callback / finally clear).
            reply_cb = self._reply_cb_factory()
            coord_mod.set_reply_callback(reply_cb)
            worker_mod.set_reply_callback(reply_cb)
            # Install this runtime as the turn's GroupRuntime contextvar so the
            # group graph's route_entry + every agent node (make_agent_node) can
            # consult ``rt.is_session_capped()`` / ``record_speech()`` (the
            # cross-turn speech cap). Cleared in ``finally`` so the slot doesn't
            # leak into the next turn (per-task contextvar copy so concurrent group
            # turns each see their own runtime).
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

        Same turn lifecycle as ``invoke_turn``: fresh thread, cancellable Task,
        finally clear handle. The resume runs on the SAME thread the interrupt
        paused — so this does NOT call ``_next_thread_id`` (a fresh thread would
        lose the paused state). Instead it reuses the runtime's current thread
        (the last ``invoke_turn``'s thread). If no turn was ever interrupted
        (cold runtime / the prior turn was not a dispatch), the resume is a
        harmless no-op that runs the graph on an empty/terminal thread (mirrors
        the resident engine's documented safe-no-op resume,
        m12-plan-confirmation-roadmap).

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
            # Reuse the runtime's current thread (the interrupt paused it); do NOT
            # mint a fresh thread — a fresh thread has no paused state to resume.
            thread_id = f"{self.thread_id}:{self._turn_seq}" if self._turn_seq else self.thread_id
            config = {"configurable": {"thread_id": thread_id}}

            reply_cb = self._reply_cb_factory()
            coord_mod.set_reply_callback(reply_cb)
            worker_mod.set_reply_callback(reply_cb)
            # Install this runtime so route_entry + agent nodes consult
            # ``is_session_capped()`` / ``record_speech()`` during the resume
            # turn too (a resume is still a turn).
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

        Does NOT touch the compiled graph. If a turn is active, cancels it first
        via ``cancel_turn`` so the reset cannot race an in-flight ainvoke that
        would otherwise re-populate ``_memory`` / ``_dispatch_plan`` as it
        unwinds — the caller is expected to poll status back to idle (mirrors
        PL-11 stop semantics).
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
