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
**Skeleton + contract docstrings only.** ``__init__(group)`` captures the group
identity + creates ``_stop_event`` (and the ``_current_task`` slot for the
later ``invoke_turn`` to fill). ``request_stop`` / ``cancel_turn`` /
``is_stopped`` / ``reset_stop`` are implemented to their contract (set / set+cancel
/ is_set / clear) but ``cancel_turn`` operates on ``_current_task`` which is
``None`` until a later task adds ``invoke_turn`` — so today ``cancel_turn``
returns ``False`` (no active turn), which IS the idempotent contract. The
graph compilation + ``invoke_turn`` + node-entry stop checks are later tasks
(.task.md lines 14-17); this skeleton locks the API shape they target.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from engine.group_graph import build_group_graph

if TYPE_CHECKING:
    from models.group import Group

logger = logging.getLogger("multi-agent.group_runtime")


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
    are touched only from the event loop, so no lock is needed. ``request_stop``
    / ``cancel_turn`` are safe to call from any coroutine in the loop.
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

        # MemorySaver checkpointer key for this group's graph thread. One thread
        # per group (the whole group shares ONE graph + ONE thread, vs the
        # resident per-agent {group}:{agent} keys). Cross-invoke state (the
        # coordinator's interrupt + dispatch_plan, turn_count/recent_speakers)
        # persists via the checkpointer + this thread_id.
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

    # NOTE: the full invoke_turn (state injection + ainvoke + emit idle + finally
    # _end_turn) is the next .task.md task (line 15). This task locks the compiled
    # graph ownership (compile_graph) + the cancellable turn-task wrapper
    # (_start_turn_task / _end_turn) that invoke_turn builds on.
