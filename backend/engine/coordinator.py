"""Coordinator StateGraph — 7 nodes + conditional edges (Rust handle_notify_as_coordinator).

Nodes: classify, handle_reply, llm_decide, chat, dispatch, dispatch_next, summarize.
The graph is compiled once with a MemorySaver checkpointer and invoked per
incoming notify by ``AgentEngine._handle_notify``. Cross-invoke state
(memory, dispatch_plan, recent_routes) is owned by the engine and re-injected
on each ainvoke, so the graph nodes only return partial updates (action_taken,
reply_content, dispatch_plan) rather than mutating engine state directly.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time
import uuid
from typing import Any, Optional

from langchain_core.runnables.config import RunnableConfig, var_child_runnable_config
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from engine.dispatcher import build_dispatch_sends, dispatch_ready_steps
from engine.state import CoordinatorState, GroupState
from events import (
    emit_coordinator_plan,
    emit_coordinator_reasoning,
    emit_coordinator_stats,
    emit_coordinator_think,
    emit_coordinator_token,
    emit_task_dispatched,
)
from engine.reply import persist_agent_reply
from llm.client import chat_completion, chat_completion_stream, get_llm_config
from llm.extract_json import extract_json
from llm.json_stream import ContentExtractor
from llm.prompts import (
    COORDINATOR_SYSTEM,
    build_coordinator_prompt,
    build_plan_adjust_prompt,
    build_step_recovery_prompt,
)
from store import crud

logger = logging.getLogger("multi-agent.coordinator")

# callback set by the engine on each ainvoke so nodes can persist replies +
# mention-route via the engine's unified _reply path. Nodes must not touch
# reply callback installed by the engine for the duration of one invoke.
# 用 contextvars 而非模块级全局变量：每个 agent engine 是独立 asyncio task，task 创建
# 时 copy context，各自 set 的 cb 互不覆盖。原全局单例在并发场景（协调者与 worker 同时
# ainvoke）会被后 set 的覆盖先 set 的 → _unified_reply 时 _REPLY_CB 已被清空 → @peer
# 不路由。与 worker.py 的 _REPLY_CB 同构（各自独立 ContextVar，互不串台）。
_REPLY_CB: contextvars.ContextVar = contextvars.ContextVar(
    "coordinator_reply_cb", default=None
)

# The compiled coordinator graph instance, set by ``build_coordinator_graph``
# so node helpers (``_detect_residual_interrupt``) can inspect the thread state
# via ``aget_state`` without the engine having to thread the graph object
# through every node. Set once at compile time; the graph is read-only after
# compile, so a single shared reference is safe across concurrent engine
# invokes (each invoke carries its own thread_id in ``config``).
_GRAPH_INSTANCE: contextvars.ContextVar = contextvars.ContextVar(
    "coordinator_graph_instance", default=None
)
# The state-visible pending plan at classify time, set by node_classify_incoming
# before calling ``_detect_residual_interrupt``. The guard cannot read state
# directly (``aget_state`` inside a node returns a pre-step snapshot whose
# ``next`` does not reflect a prior node's interrupt), so classify passes the
# resident pending plan through this ContextVar. Per-task (copied at engine
# run-loop creation), so concurrent engines don't cross-talk.
_PENDING_PLAN_VIEW: contextvars.ContextVar = contextvars.ContextVar(
    "coordinator_pending_plan_view", default=None
)


def set_reply_callback(cb: Any) -> None:
    """Install the engine's unified reply callable for the duration of one invoke.

    Sets the callback in the *current task's* context (contextvars), so
    concurrent engine invokes each see their own cb — not a shared global that
    the last writer wins.
    """
    _REPLY_CB.set(cb)


# Python 3.10 + langgraph 1.2.5 async-node contextvar workaround for interrupt().
# See memory langgraph-interrupt-py310-contextvar-pitfall: interrupt() reads the
# contextvar ``var_child_runnable_config`` to find its config (scratchpad/checkpointer
# keys live there). On Python < 3.11 the async node path does NOT propagate that
# contextvar into the user coroutine (langgraph gates the
# ``asyncio.create_task(coro, context=ctx)`` propagation behind
# ``ASYNCIO_ACCEPTS_CONTEXT = sys.version_info >= (3, 11)``), so a bare
# ``interrupt(...)`` inside an async node raises ``RuntimeError: Called get_config
# outside of a runnable context``. LangGraph *does* inject the runnable config as a
# ``config`` kwarg when the node declares it, so we re-set the contextvar from that
# injected config right before calling ``interrupt``. This is a no-op on 3.11+ (the
# var is already set) and a fix on 3.10. Scoped to interrupt callers only.
@contextlib.contextmanager
def _runnable_config_ctx(config: RunnableConfig | None):
    """Temporarily expose ``config`` as the runnable config contextvar.

    Used to bridge the 3.10 async-node gap so ``interrupt()`` (which reads the
    contextvar) sees the config the graph already injected via the node's
    ``config`` kwarg. ``config`` may be ``None`` when LangGraph did not inject
    it (defensive); in that case this context manager is a pure no-op.
    """
    if config is None:
        yield
        return
    token = var_child_runnable_config.set(config)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def _leader_system(state: CoordinatorState) -> list[dict[str, str]]:
    """群主 system 消息：agent.system_prompt 始终拼接 COORDINATOR_SYSTEM（用户不感知）。

    coordinator 不是智能体类型、只是路由标记；群里谁当 Leader，行为就由它自己的
    system_prompt + 群主职责（COORDINATOR_SYSTEM）共同决定。base 为空时退化为纯
    COORDINATOR_SYSTEM（与改前等价）。单聊不走 coordinator 图（registry 按 single_chat
    选 worker 图），故本 helper 只在群聊 Leader 路径生效。
    """
    base = (state.get("system_prompt") or "").strip()
    return [{"role": "system", "content": base + "\n" + COORDINATOR_SYSTEM}]


async def _unified_reply(
    group_id: str,
    agent_id: str,
    content: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Persist an agent_reply + emit + mention route (Rust engine.reply).

    Persistence + emit delegated to ``persist_agent_reply`` (engine.reply, B10)
    so the agent_reply shape is a single source shared with the registry's
    execute-path announce and the worker graph's reply. Mention routing stays
    here, performed by the engine's callback (set via ``set_reply_callback``)
    so recent_routes anti-loop state is owned by the engine.

    ``data`` is written onto the persisted message so it survives reload /
    reconnect. The coordinator chat path passes the streaming run-stats
    (``{reply_id, elapsed_ms, tokens}``) here so the finalized bubble can keep
    rendering the "Ns · ↓ N tokens" status line after the streaming bubble
    retires (stats don't vanish on completion). Other callers (announce /
    summarize / recovery) leave ``data=None`` — no behavior change.
    """
    await persist_agent_reply(group_id, agent_id, content, data)
    cb = _REPLY_CB.get()
    if cb is not None:
        await cb(content)


async def _detect_residual_interrupt(
    config: Optional[RunnableConfig], incoming_kind: str
) -> None:
    """Best-effort residual-interrupt detector for the classify non-confirm path.

    ``node_dispatch`` pauses the thread via ``interrupt()`` mid-node. On a
    fresh-input invoke (e.g. a new user demand while a plan awaits
    confirmation) LangGraph 1.2.5 auto-resolves that pause as it routes the new
    input through the graph — so the new demand is NOT swallowed. But if the
    LLM then decides ``dispatch`` again, the OLD pending plan in the
    checkpointer is silently overwritten by the NEW plan (replace_value
    reducer): the user abandoned plan A by asking for plan B, and plan A
    vanishes without a peep.

    This helper inspects the *state-visible* ``dispatch_plan`` (read directly
    from the node's ``state`` view of the checkpoint, NOT via a nested
    ``aget_state`` call — which returns a pre-step snapshot whose ``next`` does
    not reflect a prior node's interrupt). If a plan with pending steps is
    resident AND this is not a plan_confirm, the pending plan will be abandoned
    by a subsequent dispatch decision; we log that so the abandon is
    observable rather than silent. It does NOT mutate state and never raises.
    The actual interrupt resolve happens implicitly via LangGraph's fresh-input
    semantics; this is observability only (the task spec's "update_state resolve"
    is unnecessary in 1.2.5 because fresh-input already resolves — verified
    across 5+ runs).

    Outlet contract (B6): the detect result has a *structured* log outlet, not a
    free-text peep — both the info (abandon-plan surfaced) and the debug (probe
    degraded) carry an ``extra={"event": ...}`` field so a future log shipper
    (Loki/ELK/json-formatter) can aggregate by ``event`` without grepping prose.
    This is observability-only with NO downstream consumer in-process; if no
    collector is ever wired AND product confirms abandon-plan needs no trace,
    the whole helper + ``_PENDING_PLAN_VIEW`` + the classify call site (the
    ``_PENDING_PLAN_VIEW.set(...)`` / ``await _detect_residual_interrupt`` block
    in ``node_classify_incoming``) are safe to delete — routing is unchanged
    (``test_m12_boundary_new_demand`` asserts routing behavior, not logs, so it
    stays green). Kept now because abandon-plan is a real, low-frequency,
    hard-to-otherwise-see event.
    """
    try:
        # state-visible pending plan = a plan awaiting confirmation that a new
        # (non-confirm) demand would abandon if the LLM decides dispatch again.
        plan = _PENDING_PLAN_VIEW.get() or []
        pending = sum(1 for s in plan if isinstance(s, dict) and s.get("status") == "pending")
        if pending:
            logger.info(
                "[coordinator] pending plan awaiting confirmation; a new %r demand "
                "may abandon it if the LLM decides dispatch again (%d pending step(s))",
                incoming_kind, pending,
                extra={
                    "event": "plan_abandoned_by_new_demand",
                    "incoming_kind": incoming_kind,
                    "pending_steps": pending,
                },
            )
    except Exception:
        # Observability-only: never block classify routing on a state lookup.
        # ``extra.event`` tags the degradation so it is collectible (not silent)
        # when debug logging is enabled; ``exc_info`` keeps the traceback.
        # B28 错误出口统一：本块是 coordinator 全文唯一不走 exception 级日志的
        # ``except Exception``——是有意为之（observability-only state 探针，非 best-effort
        # WS 推送）。其余 best-effort emit/reply 一律 exception 级日志非静默——见
        # test_vh25 锁契约（debug 级 + exc_info 是 observability 降级的正确 level，不当 error 刷）。
        logger.debug(
            "[coordinator] residual-interrupt probe skipped",
            exc_info=True,
            extra={"event": "residual_interrupt_probe_skipped"},
        )


# ── plan-formatting helpers ────────────────────────────────────────────────

#: Maximum chars of a step's ``result``/``instruction`` carried into any
#: human-facing summary or LLM view. Bound so a single runaway worker output
#: (e.g. a verbose ``cat`` of a large file) can't blow up the chat bubble or
#: starve the prompt's context window. Single source — replaces the scattered
#: ``[:200]`` magic numbers that previously dotted ``node_summarize``,
#: ``_build_plan_adjust_state`` and ``_build_step_recovery_state`` (B7).
STEP_FIELD_LIMIT = 200


def _step_text(step: dict[str, Any]) -> str:
    """Return the human-facing text for a step: its result if present else its instruction.

    ``node_summarize`` renders a per-step line; both result and instruction must
    be truncated so one runaway worker output can't dominate the summary bubble.
    This helper is the single source for that truncation (B7) — previously
    ``node_summarize`` inlined ``(s.get('result') or s.get('instruction', ''))[:200]``
    with a bare magic ``200``. Kept as a private helper (not ``format_step_summary``)
    because the per-step text is also reused by ``_build_plan_adjust_state`` /
    ``_build_step_recovery_state`` for their LLM views, which truncate at the
    same limit — centralizing the truncation + the ``result or instruction``
    precedence avoids the three copies drifting.
    """
    return (step.get("result") or step.get("instruction") or "")[:STEP_FIELD_LIMIT]


def format_step_summary(plan: list[dict[str, Any]]) -> str:
    """Render a plan as a per-step summary block (✅/❌ + agent + result-or-instruction).

    Used by ``node_summarize`` to build the "🎉 全部完成！协作结果汇总" reply.
    Previously inlined in the node as a ``"\\n".join(...)`` over ``plan`` with a
    bare ``[:200]`` truncation on the result-or-instruction text (B7). Extracted
    to a shared helper so the format (status emoji + agent name + truncated text,
    newline-joined) has one definition — the dispatcher could reuse it for a
    progress announce if needed. ``STEP_FIELD_LIMIT`` is the single source for
    the per-field truncation width.

    Each step line: ``"<✅|❌> <agent_name>: <result-or-instruction, ≤200 chars>"``.
    A step with no result and no instruction renders an empty text segment
    (``"✅ bob: "``) rather than crashing — consistent with the prior inlined
    behaviour (``s.get('result') or s.get('instruction', '')`` defaulted to ``""``).
    """
    return "\n".join(
        f"{'✅' if s.get('status') == 'completed' else '❌'} {s.get('agent_name', '')}: "
        f"{_step_text(s)}"
        for s in plan
    )


# ── nodes ─────────────────────────────────────────────────────────────────


async def node_classify_incoming(
    state: CoordinatorState, config: Optional[RunnableConfig] = None
) -> dict:
    """Classify the incoming notify: worker reply vs new demand (resume bypasses classify).

    Three branches:
    - ``confirm_dispatch`` (PL-02, legacy defensive-only): reached only if a
      ``plan_confirm``-kindled notify ever lands here. Since task 11 the
      plan-confirm API endpoints resume ``node_dispatch``'s ``interrupt()``
      directly via ``Command(resume=...)`` (registry ``_handle_notify`` →
      ``route_plan_resume``), bypassing classify entirely, so this branch is no
      longer on the normal user-confirm path. It is kept as a defensive branch:
      if ``incoming_kind == "plan_confirm"`` AND the resident ``dispatch_plan``
      (sourced from the checkpointer — ``node_dispatch`` checkpointed it on the
      interrupt turn) still has at least one pending step, route straight to
      ``dispatch_next`` to fan out the pending steps WITHOUT going through the
      coordinator LLM (the plan was already LLM-decided on the dispatch turn, so
      confirming is a pure resume, not a re-decision). Falls through to
      ``llm_decide`` if no step is pending, so a stray confirm can't re-fire a
      dead plan.
    - ``handle_reply``: a worker reported back — an ``agent_reply`` notify whose
      ``data.task_id`` matches a dispatched step.
    - ``llm_decide``: everything else (new user demand) → coordinator LLM.

    Residual-interrupt guard (PL-02): ``node_dispatch``'s ``interrupt()`` leaves
    the thread paused mid-node (``get_state(config).next`` is ``("dispatch",)``).
    A fresh-input invoke on that thread auto-resumes the interrupt (LangGraph
    runs the new input through the graph from START, and a subsequent node's
    state write resolves the pause). This means a new demand while a plan
    awaits confirmation is NOT swallowed — it routes to ``llm_decide`` and the
    interrupt is resolved as a side effect. However, if the LLM then decides
    ``dispatch`` again, the OLD pending plan in the checkpointer is silently
    overwritten by the NEW plan (replace_value reducer) — i.e. a user who
    abandons a waiting plan A by asking for plan B loses plan A without a peep.
    The guard below detects a residual interrupted-state on the dispatch node
    for the *non-confirm* path and surfaces it (logs + tags the result) so the
    abandon-plan case is observable rather than silent. ``auto_confirm`` turns
    are exempt (no interrupt is created). Best-effort: a graph/config lookup
    failure degrades to the plain classify path (never blocks routing).
    """
    kind = state.get("incoming_kind", "")
    sender = state.get("incoming_sender", "")

    # PL-02: explicit user plan-confirmation — *legacy fresh-input channel*
    # (downgraded to defensive-only in task 11). The plan-confirm API endpoints
    # (/confirm | /direct | /modify) no longer push a ``plan_confirm`` notify;
    # they resume ``node_dispatch``'s ``interrupt()`` directly via
    # ``route_plan_resume`` → ``Command(resume=...)`` (the native resume path in
    # registry's ``_handle_notify``), bypassing classify entirely. So this branch
    # is no longer reached on the normal user-confirm path. It is kept as a
    # defensive branch in case a stray ``plan_confirm`` notify ever reaches
    # classify (e.g. a stale client or a future code path re-introduces the
    # marker): the plan is read from state (checkpointer is the source of truth
    # — ``node_dispatch`` checkpointed it on the interrupt turn via the
    # replace_value reducer), NOT from the notify payload, so a confirm works
    # even though no API re-sends the plan.
    if kind == "plan_confirm":
        plan = state.get("dispatch_plan") or []
        if any(s.get("status") == "pending" for s in plan):
            return {"action_taken": "confirm_dispatch"}
        # nothing pending to confirm — let the coordinator LLM respond
        return {"action_taken": "llm_decide"}

    if kind == "agent_reply" and sender != "user":
        # check if a dispatched step's task_id matches the notify data.task_id
        data = state.get("incoming_data") or {}
        task_id = data.get("task_id")
        plan = state.get("dispatch_plan") or []
        if task_id and any(s.get("task_id") == task_id and s.get("status") == "dispatched" for s in plan):
            return {"action_taken": "handle_reply"}

    # Residual-interrupt guard: if the thread is still paused on the dispatch
    # node (a plan is awaiting confirmation) and this is NOT a plan_confirm,
    # the incoming demand will overwrite/abandon that pending plan once the LLM
    # decides dispatch. We cannot stop that (the user moved on), but we surface
    # it so the abandon is observable rather than silent. auto_confirm turns
    # never create an interrupt, so they are skipped. Best-effort: any
    # graph/state lookup failure degrades to the plain llm_decide path.
    if not state.get("auto_confirm"):
        # Expose the state-visible pending plan to the guard (which cannot read
        # state directly via aget_state — that returns a pre-step snapshot whose
        # ``next`` does not reflect a prior node's interrupt).
        _PENDING_PLAN_VIEW.set(list(state.get("dispatch_plan") or []))
        await _detect_residual_interrupt(config, kind)

    return {"action_taken": "llm_decide"}


async def node_handle_reply(state: CoordinatorState) -> dict:
    """Worker reported back: mark the dispatched step completed/failed, then continue.

    Rust engine.rs 447-475. Finds the step whose task_id matches the notify
    data.task_id, sets status completed/failed + result. Then — MT-14 — asks
    the coordinator LLM whether the *remaining pending steps* need adjusting in
    light of this intermediate result, splices any revised pending steps back
    into the plan (preserving completed/failed/dispatched steps), and announces
    the adjustment. If all steps are done -> summarize; otherwise dispatch_next
    (which fans out the now possibly-revised ready steps).

    MT-14 dynamic adjustment: a worker's intermediate result may change what
    the remaining workers should do — e.g. the backend's API shape determines
    how the frontend should call it. Rather than blindly fan out the original
    pending steps, the Leader re-considers them against the fresh result. The
    adjustment only touches steps that have NOT started (status == "pending"):
    completed/failed (history) and dispatched (in-flight) steps are preserved,
    so an adjustment can never rewrite work that already happened or is in
    flight. The LLM may return ``adjust=false`` to keep the plan as-is (the
    common case for independent steps), and any LLM/parse error falls back to
    the unchanged plan — so dynamic adjustment is purely additive, never
    blocks the deterministic dispatch path.
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

    # MT-15: on a worker failure, ask the LLM whether to retry or degrade
    # BEFORE the all-done check — a single-step plan where the only step just
    # failed would otherwise short-circuit straight to summarize, skipping the
    # recovery decision entirely (retry/skip could still salvage it). The
    # recovery may mutate the step back to ``pending`` (retry/reassign) or
    # ``completed`` (skip/degrade), so all_done must be re-evaluated *after*
    # it runs. Bounded by MAX_RETRY_ATTEMPTS so a step can't retry forever.
    if not success:
        plan = await _maybe_handle_step_failure(state, plan, matched_idx)
        # after retry/reassign the step may be pending again (re-dispatched);
        # skip may have marked it completed (degraded). Re-evaluate all_done.
        if all(s.get("status") in ("completed", "failed") for s in plan):
            return {"dispatch_plan": plan, "action_taken": "summarize"}
        # if the failed step was reset to pending (retry/reassign), skip the
        # MT-14 success-side adjustment — there are no fresh results to
        # adjust on. dispatch_next will fan out the ready (re-dispatched) step.
        if plan[matched_idx].get("status") == "pending":
            return {"dispatch_plan": plan, "action_taken": "dispatch_next"}

    all_done = all(s.get("status") in ("completed", "failed") for s in plan)
    if all_done:
        return {"dispatch_plan": plan, "action_taken": "summarize"}

    # MT-14: only ask the LLM to revise the remaining pending steps when this
    # report completed successfully — a failed step is handled by the DAG
    # fail-fast path in dispatch_ready_steps (apply_fail_fast cascades the
    # failure to dependent pending steps), and revising a plan around a failure
    # is the MT-15 (retry/degrade) concern, not MT-14 (adjust on success).
    # Also skip the LLM call when there are no pending steps left to revise
    # (all remaining steps are dispatched — in flight — so nothing to adjust).
    pending_steps = [s for s in plan if s.get("status") == "pending"]
    if success and pending_steps:
        plan = await _maybe_adjust_remaining_steps(state, plan)

    return {"dispatch_plan": plan, "action_taken": "dispatch_next"}


async def _maybe_adjust_remaining_steps(
    state: CoordinatorState, plan: list[dict]
) -> list[dict]:
    """MT-14: ask the LLM whether the remaining pending steps need revising.

    Builds a ``plan_state`` summary (each step's status + result for completed
    ones, instruction for pending ones) and the worker's report, then calls the
    plan-adjustment LLM. If it returns ``adjust=true`` with a ``revised_steps``
    list, those steps replace the pending steps in-place (preserving step order
    by re-keying them to their original step numbers), the revised plan is
    re-announced via ``emit_coordinator_plan``, and the ``announce`` text is
    posted as a Leader reply so the user sees the adjustment.

    On any LLM error, parse failure, or ``adjust=false``, the plan is returned
    unchanged — so this never blocks dispatch. The returned ``plan`` is always
    a complete list (history + in-flight + possibly-revised pending steps).
    """
    group_id = state["group_id"]
    coordinator_id = state["agent_id"]

    # Render a compact, status-aware view of the plan for the LLM: completed
    # steps include their result (the intermediate result that should inform
    # the adjustment), pending steps show their current instruction.
    state_lines = []
    for s in plan:
        st = s.get("status", "pending")
        label = {
            "completed": "已完成", "failed": "已失败",
            "dispatched": "执行中", "pending": "待执行",
        }.get(st, st)
        extra = ""
        if st == "completed":
            extra = f"，结果：{(s.get('result') or '')[:300]}"
        elif st in ("pending", "dispatched"):
            extra = f"，指令：{(s.get('instruction') or '')[:200]}"
        state_lines.append(
            f"步骤{s.get('step')}（{s.get('agent_name', '')}）[{label}]{extra}"
        )
    plan_state = "\n".join(state_lines)

    # The worker that just reported is the most-recently-completed step.
    just_done = next(
        (s for s in plan if s.get("status") == "completed" and s.get("result")),
        None,
    )
    worker_name = (just_done or {}).get("agent_name", "成员")
    worker_report = state.get("incoming_message", "")

    prompt = build_plan_adjust_prompt(plan_state, worker_report, worker_name)
    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            _leader_system(state) + [{"role": "user", "content": prompt}],
        )
        decision = _parse_plan_adjust_decision(raw)
    except Exception as e:
        logger.warning("[coordinator] plan-adjust LLM failed: %s", e)
        decision = None

    if not decision or not decision.get("adjust"):
        return plan

    revised = decision.get("revised_steps") or []
    if not isinstance(revised, list) or not revised:
        return plan

    # Splice revised steps in-place: preserve completed/failed/dispatched
    # steps, replace each pending step with its revision (matched by step
    # number when the LLM kept it; appends new steps otherwise). Re-key the
    # remaining pending slots to the original step numbers so the DAG deps
    # (which reference step numbers) still resolve.
    revised_by_step: dict[Any, dict] = {}
    appended: list[dict] = []
    for r in revised:
        if not isinstance(r, dict):
            continue
        rstep = r.get("step")
        norm = _normalize_revised_step(r, plan)
        if rstep is not None and any(
            s.get("status") == "pending" and s.get("step") == rstep for s in plan
        ):
            revised_by_step[rstep] = norm
        else:
            appended.append(norm)

    # Merge: keep non-pending steps verbatim; replace pending steps whose
    # number is in revised_by_step with the revision; drop pending steps the
    # LLM omitted (cancelled); append brand-new steps at the end.
    new_plan: list[dict] = []
    used_revised: set[Any] = set()
    for s in plan:
        st = s.get("status", "pending")
        if st != "pending":
            new_plan.append(s)
            continue
        num = s.get("step")
        if num in revised_by_step:
            new_plan.append(revised_by_step[num])
            used_revised.add(num)
        # else: pending step omitted by the LLM -> dropped (cancelled)
    for norm in appended:
        new_plan.append(norm)

    # Re-announce the adjusted plan so the frontend PlanConfirmCard /
    # WorkerTrace reflects the revised remaining steps, and post the announce
    # text as a Leader reply so the user sees what changed and why.
    try:
        await emit_coordinator_plan(group_id, coordinator_id, new_plan)
    except Exception:
        logger.exception("[coordinator] failed to re-announce adjusted plan")
    announce = (decision.get("announce") or "").strip()
    if announce:
        try:
            await _unified_reply(group_id, coordinator_id, announce)
        except Exception:
            logger.exception("[coordinator] failed to post plan-adjust announce")
    logger.info(
        "[coordinator] plan adjusted after worker report: %d -> %d steps (reason: %s)",
        len(plan), len(new_plan), (decision.get("reason") or "")[:120],
    )
    return new_plan


def _normalize_revised_step(raw: dict, plan: list[dict]) -> dict:
    """Normalize one LLM-revised step into the plan's step shape.

    Ensures ``status == "pending"``, ``task_id`` is cleared (so it re-dispatches
    fresh), and ``depends_on`` is a list. The step number is preserved from the
    LLM output (the caller splices by number); a missing/invalid number gets
    a fresh number above the plan's current max so appended steps sort after.
    """
    existing = {s.get("step") for s in plan if s.get("step") is not None}
    step_num = raw.get("step")
    if step_num is None or step_num in existing and any(
        s.get("status") != "pending" and s.get("step") == step_num for s in plan
    ):
        # fall back to a fresh number above the current max
        step_num = max([0] + [s.get("step") or 0 for s in plan]) + 1
    return {
        "step": step_num,
        "agent_id": raw.get("agent_id", ""),
        "agent_name": raw.get("agent_name", ""),
        "instruction": raw.get("instruction", ""),
        "depends_on": raw.get("depends_on", []) or [],
        "status": "pending",
        "result": None,
        "task_id": None,
    }


async def _normalize_plan_agent_ids(
    group_id: str, plan: list[dict]
) -> list[dict]:
    """Resolve each step's ``agent_id`` to a real member id before fan-out.

    The coordinator prompt exposes each member as ``- {name}（{role}）id=
    {agent_id}`` and asks the LLM for strict JSON ``{"agent_id": "xxx"}``, but
    the LLM frequently echoes the **role label** (``backend_engineer``) or the
    **member name** (``后端工程师``) into ``agent_id`` instead of the real id
    (``agent_backend_1``). On the resident dispatch path a bogus id is harmless
    (``push_task(receiver_id=...)`` queues a task no engine claims). On the
    GROUP path ``build_dispatch_sends`` mints ``Send(agent_node_target(id))`` =
    ``agent_<bogus>``, a node the compiled graph never registered → LangGraph
    silently drops it (``Ignoring unknown node name ... in pending sends``) →
    the agent node never runs → no worker execute → no report-back → dependent
    steps never satisfy ``depends_on`` → plan deadlock (mt14 step2 break).

    This resolves a step's ``agent_id`` against the live roster by:
      1. exact id match (already correct → unchanged),
      2. exact role match (``backend_engineer`` → the member with that role),
      3. exact name match (``后端工程师`` → the member with that name),
      4. case-insensitive substring of the role/id as a last resort,
    preferring non-coordinator members (the coordinator is a sub-node, not an
    ``agent_<id>`` dispatch target). ``agent_name`` is re-synced from the
    resolved member so the dispatch emit + summary stay consistent. Steps that
    resolve to no member keep their original ``agent_id`` (the resident path's
    tolerance is preserved — a bogus id degrades rather than raising, so a
    cold/compile-failed group still dispatches to an inbox).

    Single source: BOTH ``node_dispatch_next`` (resident, ``push_task``) and
    ``node_dispatch_next_group`` (group, ``Send``) call this before fan-out, so
    the step id + the ``emit_task_dispatched`` event's ``agent_id`` field are a
    real member id on both paths. Never raises — a roster read failure returns
    the plan unchanged (best-effort, degrades to the pre-fix tolerant behaviour).
    """
    if not plan:
        return plan
    try:
        members = await crud.list_group_members_with_agent(group_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[coordinator] roster read failed for agent_id normalization "
            "(group=%s); leaving plan agent_ids as-is (best-effort)",
            group_id,
        )
        return plan
    # non-coordinator members first (the coordinator is a sub-node, not a
    # dispatch target); fall back to the full roster if a group has only the
    # coordinator somehow.
    workers = [m for m in members if m.agent_id != _coord_id_of(members, plan, group_id)]
    pool = workers if workers else members
    by_id = {m.agent_id: m for m in pool}
    by_role = {m.agent_role: m for m in pool if m.agent_role}
    by_name = {m.agent_name: m for m in pool if m.agent_name}

    def _resolve(raw: str) -> str:
        if not raw:
            return raw
        if raw in by_id:
            return raw
        if raw in by_role:
            return by_role[raw].agent_id
        if raw in by_name:
            return by_name[raw].agent_id
        low = raw.lower()
        for m in pool:
            if m.agent_role and m.agent_role.lower() == low:
                return m.agent_id
            if m.agent_name and m.agent_name == raw:
                return m.agent_id
        return raw

    changed = False
    for s in plan:
        if not isinstance(s, dict):
            continue
        resolved = _resolve(str(s.get("agent_id", "") or ""))
        if resolved and resolved != s.get("agent_id"):
            s["agent_id"] = resolved
            member = by_id.get(resolved)
            if member and member.agent_name:
                s["agent_name"] = member.agent_name
            changed = True
    if changed:
        logger.info(
            "[coordinator] normalized plan step agent_ids against roster "
            "(group=%s, %d steps) — LLM had echoed role/name instead of id",
            group_id, len(plan),
        )
    return plan


def _coord_id_of(members: list, plan: list[dict], group_id: str) -> str:
    """Best-effort coordinator id for a group (to exclude it from dispatch targets)."""
    # a member whose role is "coordinator" is the Leader
    for m in members:
        if getattr(m, "agent_role", "") == "coordinator":
            return m.agent_id
    return ""


def _splice_amended_steps(plan: list[dict], amended: list[dict]) -> list[dict]:
    """Splice user-amended steps (from a /plan/modify resume payload) into the plan.

    Each amended step is keyed by its ``step`` number. Steps whose number
    matches an existing pending step replace it (re-deriving pending status +
    clearing task_id so it re-dispatches); steps with a fresh/unknown number
    are appended at the end. Steps absent from ``amended`` are left untouched
    (the user only edited the ones they returned). Completed/failed/dispatched
    steps are never rewritten by an amend — the modify card only sends the
    still-pending ones.

    Mirrors the in-place splice ``_maybe_adjust_remaining_steps`` does for the
    MT-14 LLM-revision path, but driven by the user's edited payload rather
    than an LLM decision. Returns a new plan list (does not mutate the input).
    """
    new_plan: list[dict] = [dict(s) for s in plan]
    by_step = {s.get("step"): s for s in new_plan}
    appended: list[dict] = []
    for raw in amended:
        if not isinstance(raw, dict):
            continue
        num = raw.get("step")
        target = by_step.get(num) if num is not None else None
        if target is not None and target.get("status") == "pending":
            target.update(
                {
                    "agent_id": raw.get("agent_id", target.get("agent_id", "")),
                    "agent_name": raw.get("agent_name", target.get("agent_name", "")),
                    "instruction": raw.get("instruction", target.get("instruction", "")),
                    "depends_on": raw.get("depends_on", target.get("depends_on", [])) or [],
                    "status": "pending",
                    "task_id": None,
                    "result": None,
                }
            )
        else:
            fresh = max([0] + [s.get("step") or 0 for s in new_plan]) + 1
            new_plan.append(
                {
                    "step": num if num is not None else fresh,
                    "agent_id": raw.get("agent_id", ""),
                    "agent_name": raw.get("agent_name", ""),
                    "instruction": raw.get("instruction", ""),
                    "depends_on": raw.get("depends_on", []) or [],
                    "status": "pending",
                    "result": None,
                    "task_id": None,
                }
            )
    return new_plan


def _parse_plan_adjust_decision(raw: str) -> dict | None:
    v = extract_json(raw)
    if v is None:
        return None
    adjust = bool(v.get("adjust", False))
    revised = v.get("revised_steps")
    if not isinstance(revised, list):
        revised = []
    return {
        "adjust": adjust,
        "reason": str(v.get("reason", "")),
        "announce": str(v.get("announce", "")),
        "revised_steps": revised,
    }


# MT-15: maximum retry attempts for a single step before hard-failing.
# Caps retry loops so a persistently-failing step can't re-dispatch forever
# (each retry resets the step to ``pending`` → re-dispatch → re-fail → another
# recovery decision). After this many attempts the recovery decision is
# forced to ``keep_failed`` regardless of what the LLM says.
MAX_RETRY_ATTEMPTS = 2


async def _maybe_handle_step_failure(
    state: CoordinatorState, plan: list[dict], failed_idx: int
) -> list[dict]:
    """MT-15: on a worker failure, decide retry / reassign / skip / keep_failed.

    Asks the coordinator LLM how to handle the failed step before the DAG
    fail-fast cascade runs. The decision mutates the failed step in place:

    - ``retry``   → step reset to ``pending`` (task_id cleared, attempt++),
                    so ``dispatch_ready_steps`` re-dispatches it to the same
                    worker. Capped by ``MAX_RETRY_ATTEMPTS`` — once exhausted the
                    step stays ``failed`` (forced keep_failed).
    - ``reassign``→ step reset to ``pending`` with a new ``agent_id`` /
                    ``agent_name`` (the LLM's chosen target), task_id cleared,
                    attempt++ (counts as a retry). Re-dispatched to the new
                    worker. Also capped by ``MAX_RETRY_ATTEMPTS``.
    - ``skip``    → step marked ``completed`` with a degraded result noting the
                    failure was tolerated, so dependents (whose deps are now
                    ``completed``) can proceed. Graceful degradation — the plan
                    continues despite a non-critical step failing.
    - ``keep_failed`` (or any LLM error / parse failure / unknown strategy)
                    → step stays ``failed``; the existing ``apply_fail_fast``
                    cascade runs in ``dispatch_ready_steps`` as before. This is
                    the deterministic default — recovery is purely additive.

    The ``attempt`` counter is stored on the step as ``_attempts`` so it
    survives across the worker's re-dispatch → re-fail → next recovery decision
    (the step dict is carried in the engine's ``_dispatch_plan`` between
    invokes). On ``skip``/``keep_failed`` the counter is left as-is (terminal).

    Returns the (possibly mutated) plan. Never raises — any exception keeps the
    step ``failed`` (default cascade).
    """
    group_id = state["group_id"]
    coordinator_id = state["agent_id"]
    step = plan[failed_idx]

    # attempt counter: increments on each retry/reassign, persisted on the step
    attempts = int(step.get("_attempts") or 0)

    # Hard cap: if we've already retried MAX_RETRY_ATTEMPTS times, force
    # keep_failed without an LLM call (avoid infinite retry loops + save tokens).
    if attempts >= MAX_RETRY_ATTEMPTS:
        logger.info(
            "[coordinator] step %s failed after %d attempts -> keep_failed (cap reached)",
            step.get("step"), attempts,
        )
        return plan

    # Build the LLM view: plan state (so the LLM sees what dependents are at
    # risk), the failed step, the failure reason, and the roster of members
    # available for reassign.
    state_lines = []
    for s in plan:
        st = s.get("status", "pending")
        label = {
            "completed": "已完成", "failed": "已失败",
            "dispatched": "执行中", "pending": "待执行",
        }.get(st, st)
        extra = ""
        if st == "completed":
            extra = f"，结果：{(s.get('result') or '')[:200]}"
        elif st in ("pending", "dispatched"):
            extra = f"，指令：{(s.get('instruction') or '')[:150]}"
        state_lines.append(
            f"步骤{s.get('step')}（{s.get('agent_name', '')}）[{label}]{extra}"
        )
    plan_state = "\n".join(state_lines)
    failed_desc = (
        f"步骤{step.get('step')}（{step.get('agent_name', '')}）："
        f"{(step.get('instruction') or '')[:200]}"
    )
    failure_reason = (state.get("incoming_message") or "")[:500]
    members_models = await crud.list_group_members_with_agent(group_id)
    roster_lines = [
        f"- {m.agent_name}（{m.agent_role}）id={m.agent_id}" for m in members_models
    ]
    roster = "\n".join(roster_lines) if roster_lines else "（无其他成员）"

    prompt = build_step_recovery_prompt(
        plan_state, failed_desc, failure_reason, roster, attempts
    )
    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            _leader_system(state) + [{"role": "user", "content": prompt}],
        )
        decision = _parse_step_recovery_decision(raw)
    except Exception as e:
        logger.warning("[coordinator] step-recovery LLM failed: %s", e)
        decision = None

    if not decision:
        return plan  # default: keep_failed (step stays failed)

    strategy = decision.get("strategy", "keep_failed")
    announce = (decision.get("announce") or "").strip()

    if strategy == "skip":
        # graceful degradation: mark the step completed with a degraded result
        # so dependents (whose deps are now completed) can proceed.
        step["status"] = "completed"
        step["result"] = (
            f"⚠️ 步骤失败已降级跳过：{(step.get('result') or '')[:200] or '执行失败'}"
        )
        logger.info("[coordinator] step %s failed -> skip (degraded)", step.get("step"))
        if announce:
            try:
                await _unified_reply(group_id, coordinator_id, announce)
            except Exception:
                logger.exception("[coordinator] failed to post skip announce")
        return plan

    if strategy in ("retry", "reassign") and attempts < MAX_RETRY_ATTEMPTS:
        step["status"] = "pending"
        step["task_id"] = None
        step["result"] = None
        step["_attempts"] = attempts + 1
        if strategy == "reassign":
            target_id = decision.get("reassign_to") or ""
            # resolve the target member name + validate it's a real member
            target = next(
                (m for m in members_models if m.agent_id == target_id), None
            )
            if target and target_id != step.get("agent_id"):
                step["agent_id"] = target_id
                step["agent_name"] = target.agent_name
            # if reassign target invalid/same, fall back to retry semantics
            # (re-dispatch to original) — still a valid recovery attempt.
        logger.info(
            "[coordinator] step %s failed -> %s (attempt %d -> %d)",
            step.get("step"), strategy, attempts, attempts + 1,
        )
        if announce:
            try:
                await _unified_reply(group_id, coordinator_id, announce)
            except Exception:
                logger.exception("[coordinator] failed to post recovery announce")
        return plan

    # keep_failed or retry-cap-reached: step stays failed (default cascade)
    logger.info(
        "[coordinator] step %s failed -> keep_failed (strategy=%s attempts=%d)",
        step.get("step"), strategy, attempts,
    )
    return plan


def _parse_step_recovery_decision(raw: str) -> dict | None:
    """Parse the step-recovery LLM response into {strategy, reason, announce, reassign_to}.

    Lenient: a missing/invalid ``strategy`` defaults to ``keep_failed`` (no
    change, default cascade). Returns None on JSON parse failure so the caller
    keeps the step ``failed`` (deterministic fallback).
    """
    v = extract_json(raw)
    if v is None:
        return None
    strategy = str(v.get("strategy", "keep_failed"))
    if strategy not in ("retry", "reassign", "skip", "keep_failed"):
        strategy = "keep_failed"
    return {
        "strategy": strategy,
        "reason": str(v.get("reason", "")),
        "announce": str(v.get("announce", "")),
        "reassign_to": str(v.get("reassign_to", "") or ""),
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
        # MT-03: inject the user-written Leader 指挥策略 into the coordinator
        # prompt so the Leader's 拆解/派工 decisions honour it. Read from state
        # (the engine injects group.config.leader_strategy per ainvoke via
        # models.get_leader_strategy). Empty string → no strategy section.
        state.get("leader_strategy", ""),
    )

    config = get_llm_config()
    try:
        reply_id, raw, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text = await _stream_coordinator_decision(
            config,
            _leader_system(state) + [{"role": "user", "content": prompt}],
            state["group_id"],
            state["agent_id"],
        )
        decision = _parse_coordinator_decision(raw)
    except Exception as e:
        logger.warning("[coordinator] LLM decision failed: %s", e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边理解有点困难，能再说一次吗？",
            "plan": [],
        }
        reply_id, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text = "", 0, 0, "", 0, ""

    # Stamp the streaming run-stats onto the chat/ask/continue action so node_chat
    # persists them onto the agent_reply's data — the finalized bubble then keeps
    # rendering "model · Ns · ↓ N tokens（含 N 推理）" after the streaming bubble
    # retires (stats stay visible, don't vanish on completion).
    #
    # Which actions carry stats: any action whose reply_content IS the streamed
    # LLM text and routes to node_chat. chat/ask/continue all reuse decision
    # ["content"] verbatim through node_chat (route_after_llm_decide sends ask/
    # continue to "chat" too), so they all consumed real tokens and deserve the
    # status line. dispatch is excluded — its reply is a templated "📋 已制定
    # 协作计划..." announce built in node_dispatch, NOT the streamed decision
    # text, so the stream's tokens/elapsed wouldn't match the persisted content.
    #
    # reasoning_tokens is persisted so the finalized bubble can keep showing
    # "含 N 推理" after the streaming bubble retires. reasoning_text (the full
    # reasoning_content) is also persisted onto data["reasoning"] so the
    # finalized bubble's collapsible panel can expand the reasoning — otherwise
    # phase="done" clears the live coordReasoning buffer and the user could
    # never expand reasoning on a historical/just-finalized bubble.
    #
    # Pre-fix this only stamped "chat", so ask/continue replies (clarifying
    # questions, continuations) lost their status line on finalization — the
    # streaming bubble showed "model · Ns · ↓ N tokens · 思考中" live, then the
    # stats vanished when the finalized bubble took over. Symptom: some
    # coordinator bubbles showed the model line, others didn't.
    if decision["action"] in ("chat", "ask", "continue"):
        decision["_stream_stats"] = {
            "reply_id": reply_id,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "model": model,
            "reasoning_tokens": reasoning_tokens,
            "reasoning": reasoning_text,
        }

    await emit_coordinator_think(
        state["group_id"], state["agent_id"], decision["action"], decision["content"]
    )
    result: dict[str, Any] = {
        "action_taken": decision["action"],
        "reply_content": decision["content"],
        # carry the per-turn streaming stats through the graph to node_chat
        "_stream_stats": decision.get("_stream_stats"),
    }
    # Only replace the resident dispatch_plan on a dispatch action. A chat/ask/
    # continue turn must NOT clobber a resident pending plan — the user may ask
    # a side question while a plan awaits confirmation (PL-02). The dispatch_plan
    # reducer is ``replace_value`` (last-write-wins, state.py), so returning
    # ``dispatch_plan: []`` here (decision["plan"] is [] for non-dispatch
    # actions) would wipe the pending plan: the PlanConfirmCard vanishes, the
    # 确认/修改/直接干 buttons disappear, and a later /plan/confirm 409s with
    # "no pending plan to confirm". Omitting the key leaves the resident plan
    # untouched (LangGraph only runs the reducer when the node returns the key).
    if decision["action"] == "dispatch":
        result["dispatch_plan"] = decision.get("plan", [])
    return result


async def node_chat(state: CoordinatorState) -> dict:
    """Persist + emit the reply_content via the unified reply path.

    Carries the streaming run-stats (``state['_stream_stats']``) onto the
    persisted agent_reply's ``data`` so the finalized bubble keeps rendering
    the "Ns · ↓ N tokens" status line after the streaming bubble retires —
    the stats stay visible, they don't vanish on completion.
    """
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        state.get("reply_content", ""),
        data=state.get("_stream_stats"),
    )
    return {}


async def node_dispatch(state: CoordinatorState, config: Optional[RunnableConfig] = None) -> dict:
    """Store the plan, announce it, then either interrupt for confirm or fan out.

    Rust engine.rs 586-599. The LLM-returned plan replaces the engine's
    dispatch_plan (returned via the reducer). The announcement reply goes
    through the unified path so it persists + emits.

    PL-02/PL-03 + LangGraph native ``interrupt()``: by default the plan is
    *announced but not dispatched* — the node calls ``interrupt({"plan": plan})``
    which pauses the graph (the checkpointer is the single source of truth for
    the resident plan, replacing the old engine ``_dispatch_plan`` mirror as
    truth). A later ``Command(resume={"mode": ...})`` wakes it: on resume the
    node re-runs from the top (LangGraph interrupt semantics — see
    ``interrupt()`` docstring "re-executing all logic"), the second
    ``interrupt`` call returns the resume value immediately, and the node
    returns ``action_taken="dispatch_next"`` so ``route_after_dispatch`` fans
    out. When ``auto_confirm`` is True ("直接干" mode, PL-03) the node tags the
    plan ``confirm_mode="auto"`` and returns
    ``action_taken="dispatch_next"`` straight away — no interrupt, immediate
    fan-out, preserving the old zero-confirmation behaviour.

    The ``plan`` returned here (and thus written to ``state.dispatch_plan``
    via the ``replace_value`` reducer) is the source the resume path and
    ``node_dispatch_next`` read. On the confirm-await interrupt turn the node
    calls ``interrupt({"plan": plan})`` *before* returning, which suspends
    the graph mid-node — so ``route_after_dispatch`` is not evaluated and no
    ``action_taken`` is set that turn; the plan is checkpointed via the
    interrupt's own checkpointing + the ``replace_value`` reducer on the
    auto-confirm partial return, and a later ``Command(resume=...)`` re-enters
    this node to return ``dispatch_next`` (the plan is unchanged, so the
    reducer no-ops).

    Note (B8): the confirm-await pause is realized entirely by ``interrupt()``
    suspending inside this node — the graph never reaches the conditional edge
    on the interrupt turn, so there is no ``wait_confirm`` action to route on.
    The earlier ``action_taken="wait_confirm"`` sentinel was removed (7564caf):
    it was inert (never routed, ``route_after_dispatch`` had no branch for it)
    and only existed for state-inspection readability. ``node_dispatch`` now
    returns ``dispatch_next`` on both the auto-confirm and resume turns, and
    nothing on the interrupt turn.

    config / 3.10 workaround: the ``config`` kwarg is injected by LangGraph
    (declared as ``Optional[RunnableConfig]``) and fed to
    ``_runnable_config_ctx`` around ``interrupt`` so the contextvar
    ``var_child_runnable_config`` is visible inside ``interrupt`` on Python
    3.10 (see ``_runnable_config_ctx``).
    """
    plan = list(state.get("dispatch_plan") or [])
    plan_summary = "\n".join(
        f"{s.get('step')}. {s.get('agent_name', '')} → {s.get('instruction', '')[:40]}..."
        for s in plan
    )
    # B14 announce 类回复不带 stats：此处「📋 已制定协作计划...」是模板文本（由
    # plan_summary 拼接，非 brain 流式 LLM 输出），故 _unified_reply 不传 data →
    # persist_agent_reply 落 data=None。与 dispatcher._dispatch_one 的「🚀 步骤 N 派发」
    # announce 同判定（模板文本不带 stats），与 A8/vg2 的 stats 契约对齐——dispatch
    # announce 被排除在 stats 透传外（stats 不匹配模板 content，前端 extractCoordStats
    # 对 data.elapsed_ms 缺失返 null 不渲染状态行，正确）。只有 chat/ask/continue
    # （reply_content 即流式 LLM 文本）才透传 _stream_stats（见 node_llm_decide 盖 stats 注释）。
    if state.get("auto_confirm"):
        await _unified_reply(
            state["group_id"],
            state["agent_id"],
            f"📋 已制定协作计划（直接干模式），开始调度：\n{plan_summary}",
        )
    else:
        await _unified_reply(
            state["group_id"],
            state["agent_id"],
            f"📋 已制定协作计划，请确认后执行：\n{plan_summary}",
        )
    await emit_coordinator_plan(
        state["group_id"], state["agent_id"], plan
    )
    # PL-03 直接干: auto_confirm skips the confirmation interrupt and routes
    # straight to dispatch_next. We tag the plan confirm_mode="auto" so the
    # front-end plan card can reflect direct-run provenance.
    if state.get("auto_confirm"):
        for s in plan:
            s.setdefault("confirm_mode", "auto")
        return {"action_taken": "dispatch_next", "dispatch_plan": plan}

    # PL-02 default: pause for human confirmation. interrupt() surfaces the
    # plan to the client and suspends; the plan is checkpointed via the
    # interrupt's own checkpointing + the replace_value reducer (interrupt
    # itself does not write state, but the graph checkpoints the partial
    # turn). A later ``Command(resume=...)`` re-runs this node — the second
    # interrupt() call returns the resume value immediately, and we fall
    # through to fan-out. No ``wait_confirm`` sentinel is set: the interrupt
    # suspends before this node returns, so the conditional edge after it is
    # never evaluated on the interrupt turn (B8: the inert sentinel was
    # removed — the pause is owned by interrupt() mid-node, not the router).
    with _runnable_config_ctx(config):
        resume = interrupt({"plan": plan})
    # On resume: the plan is already checkpointed (returned on the interrupt
    # turn); honour a modify-mode resume by splicing amended steps if present.
    if isinstance(resume, dict):
        amended = resume.get("amended_steps")
        if isinstance(amended, list) and amended:
            plan = _splice_amended_steps(plan, amended)
            await emit_coordinator_plan(
                state["group_id"], state["agent_id"], plan
            )
    return {"action_taken": "dispatch_next", "dispatch_plan": plan}


async def node_dispatch_next(state: CoordinatorState) -> dict:
    """Fan out ALL ready steps in parallel (DAG fan-out, MT-12).

    Dispatches every step that is pending with deps satisfied — independent
    steps go to their own worker engines which run concurrently as separate
    asyncio tasks. Returns ``action_taken="summarize"`` only if no step was
    dispatchable AND all steps are done; otherwise the graph ends (the engines
    run on, and each worker's report re-enters the coordinator via a notify).

    This is the **resident coordinator engine's** dispatch_next: it calls
    ``dispatch_ready_steps`` (``push_task`` to worker inboxes, band-out via the
    engine run loop). The group-graph twin is ``node_dispatch_next_group`` below,
    which fans out via LangGraph ``Send`` to the agent nodes in-graph.
    """
    group_id = state["group_id"]
    coordinator_id = state["agent_id"]
    plan = state.get("dispatch_plan") or []

    # Normalize step.agent_id against the live roster before fan-out (same
    # single-source fix as the group ``node_dispatch_next_group`` — see its
    # comment for the LLM-echoes-role-as-id defect). The resident path does
    # not strictly need this (``push_task`` tolerates a bogus receiver_id) but
    # applying it here keeps the resident + group dispatchers reading the same
    # resolved id, so ``emit_task_dispatched``'s ``agent_id`` field is a real
    # member id on BOTH paths (the frontend's task card + the MT-14/MT-15 e2e
    # probes key off it).
    plan = await _normalize_plan_agent_ids(group_id, plan)

    dispatched = await dispatch_ready_steps(group_id, coordinator_id, plan)

    if not dispatched:
        # no dispatchable step; if all done, summarize
        if plan and all(s.get("status") in ("completed", "failed") for s in plan):
            return {"action_taken": "summarize", "dispatch_plan": plan}
        return {"dispatch_plan": plan}

    # Fan-out mutated step status to "dispatched" — re-emit the plan so the
    # frontend's resident PlanStep[] (driven only by coordinator_plan WS
    # events) reflects the new statuses. Without this emit the plan card
    # stays on the first announce (all steps "pending"), so the user still
    # sees a confirmable plan and a second /plan/confirm hits 409
    # "no pending plan to confirm". This is purely an emit inside an existing
    # LangGraph node — no topology/routing change.
    try:
        await emit_coordinator_plan(group_id, coordinator_id, plan)
    except Exception:
        logger.exception("[coordinator] failed to emit plan after dispatch_next")
    return {"dispatch_plan": plan}


async def node_dispatch_next_group(state: GroupState) -> Command:
    """Group-graph dispatch_next: fan out ready steps via LangGraph ``Send``.

    Task: coordinator dispatch_next 节点 — dispatcher.dispatch_ready_steps 输出从
    ``push_task`` 改为 LangGraph ``Send``/并行 fan-out 到各 agent 节点（保 DAG
    fail-fast 与 ready_steps 逻辑）.

    The group-graph twin of the resident ``node_dispatch_next``: instead of
    ``dispatch_ready_steps`` (``push_task`` to worker inboxes, band-out via the
    engine run loop), it calls ``build_dispatch_sends`` (same fail-fast +
    ready-query, single source via ``apply_fail_fast`` + ``find_ready_steps``)
    and returns ``Command(goto=sends, update={"dispatch_plan": plan})``.
    LangGraph drives the ``Send``s in parallel within one ``ainvoke`` — each
    ``Send`` invokes the target ``agent_<agent_id>`` node with its own state
    copy seeded by the ``Send``'s payload (the step's instruction +
    ``incoming_sender=coordinator_id``), so independent ready steps run
    concurrently as in-graph agent-node invocations, exactly as the resident
    path runs them as separate asyncio tasks.

    The DAG fail-fast + ready_steps logic is byte-for-byte identical to the
    resident path (single source: ``build_dispatch_sends`` calls the same
    ``apply_fail_fast(plan)`` + ``find_ready_steps(plan)``). Step mutation
    (``pending`` → ``dispatched`` + ``task_id``) is identical too, so the
    coordinator's downstream ``handle_reply`` (MT-15 recovery + MT-14 adjust)
    matches the report-back by ``task_id`` regardless of dispatch transport.

    Routing contract (preserved verbatim from the resident
    ``route_after_dispatch_next``):

    - no dispatchable step + all done → ``action_taken="summarize"``,
      ``route_after_dispatch_next`` routes to ``summarize`` (Command goto the
      summarize sub-node).
    - no dispatchable step + not all done → END (in-flight steps still running;
      their report-back re-enters via the next turn — mirrors the resident
      ``return {"dispatch_plan": plan}`` → ``route_after_dispatch_next`` END).
    - dispatched steps → the ``Send``s in ``goto`` drive the fan-out;
      ``route_after_dispatch_next`` is NOT consulted (LangGraph follows
      ``Command.goto=[Send(...), ...]`` to the agent nodes, then each agent
      node's own ``Command(goto=...)`` continues/ends the turn).

    Re-emits the plan (mutated statuses ``dispatched``) so the frontend
    PlanStep[] reflects the fan-out — mirrors the resident ``node_dispatch_next``
    emit. The plan is carried onto ``dispatch_plan`` via the ``replace_value``
    reducer (single source: ``GroupState.dispatch_plan``).

    NOTE — this node is the group-graph dispatch_next; the resident
    ``node_dispatch_next`` (above) stays for the live coordinator engine until
    the group-graph migration swaps consumers over. Both call the SAME
    fail-fast + ready-query (single source), so the DAG semantics cannot drift
    between the two transports.
    """
    from langgraph.types import Command as _Command, Send as _Send  # noqa: F401
    from langgraph.graph import END as _END

    group_id = state["group_id"]
    coordinator_id = state.get("coordinator_id") or state.get("agent_id") or ""
    plan = state.get("dispatch_plan") or []

    # Normalize step.agent_id against the live roster BEFORE fan-out. The
    # coordinator prompt exposes each member as ``- {name}（{role}）id={agent_id}``
    # and asks for strict JSON ``{"agent_id": "xxx"}``, but the LLM frequently
    # echoes the role label (``backend_engineer`` / ``frontend_engineer``) or
    # the member name (``后端工程师``) into ``agent_id`` instead of the real id
    # (``agent_backend_1``). On the resident path this is harmless —
    # ``_dispatch_one`` does ``push_task(receiver_id=step.agent_id)`` and a
    # bogus receiver_id simply queues a task no engine claims (the resident
    # ``dispatch_ready_steps`` has no node-name lookup). On the GROUP path
    # ``build_dispatch_sends`` mints ``Send(agent_node_target(step.agent_id))``
    # = ``agent_<bogus>``, a node the compiled graph never registered →
    # LangGraph drops it (``Ignoring unknown node name agent_frontend_engineer
    # in pending sends``) → the step's agent node never runs → no worker
    # execute → no report-back → dependent steps never satisfy their
    # ``depends_on`` → plan deadlock (mt14 step2 re-dispatch break). Resolving
    # the step's ``agent_id`` to a real member id here (single source, before
    # BOTH the resident ``dispatch_ready_steps`` and the group
    # ``build_dispatch_sends`` consume it) fixes the group path without
    # disturbing the resident path (a correct id round-trips unchanged).
    plan = await _normalize_plan_agent_ids(group_id, plan)

    sends, dispatched = build_dispatch_sends(group_id, coordinator_id, plan)

    if not dispatched:
        # no dispatchable step; if all done, summarize
        if plan and all(s.get("status") in ("completed", "failed") for s in plan):
            # GROUP twin: goto must name the registered group node
            # ``summarize_group`` (group_graph.py registers it as
            # ``summarize_group``, NOT ``summarize`` — that bare name only
            # exists in the resident coordinator graph). A bare ``summarize``
            # goto here targets an unregistered node → LangGraph logs
            # "wrote to unknown channel branch:to:summarize, ignoring it" →
            # node_summarize_group never runs → no "all done" summary reply →
            # mt14 checks 8/9 fail (task #37 root cause #3).
            return _Command(
                goto="summarize_group",
                update={"action_taken": "summarize", "dispatch_plan": plan},
            )
        # not all done + nothing dispatchable → END (in-flight steps running)
        return _Command(goto=_END, update={"dispatch_plan": plan})

    # Fan-out mutated step status to "dispatched" — re-emit the plan so the
    # frontend's resident PlanStep[] reflects the new statuses (same emit as the
    # resident node_dispatch_next — without it the plan card stays on the first
    # announce and a stray /plan/confirm 409s).
    try:
        await emit_coordinator_plan(group_id, coordinator_id, plan)
    except Exception:
        logger.exception("[coordinator] failed to emit plan after dispatch_next_group")

    # Emit a ``task_dispatch`` bus event per dispatched step — mirrors the resident
    # ``_dispatch_one`` (dispatcher.py:145 ``emit_task_dispatched``), so the group
    # path's fan-out produces the SAME WS event the resident path produces. Without
    # this the frontend's task card + the MT-14/MT-15 e2e probes catch 0
    # ``task_dispatch`` events even though the ``Send``s fanned out (the chain
    # looked dead at the dispatch step — ``dispatch=0`` / 「两步不全完成」), and
    # ``emit_task_completed`` (from the worker execute path) had no matching
    # ``task_dispatch`` to pair with. ``build_dispatch_sends`` already minted the
    # per-step ``task_id`` (stored on the step) so the event carries the same id
    # the later ``task_complete`` will carry — the e2e serial-order assertion
    # (步骤1 task_complete 早于步骤2 task_dispatch) keys off these ids.
    for step in dispatched:
        try:
            await emit_task_dispatched(
                group_id,
                step.get("task_id") or "",
                step.get("step"),
                step.get("agent_id") or "",
                step.get("agent_name") or "",
                step.get("instruction") or "",
            )
        except Exception:
            logger.exception(
                "[coordinator] failed to emit task_dispatch for step %s in dispatch_next_group",
                step.get("step"),
            )
    # LangGraph drives the Send[] in parallel — each agent node gets its own
    # state copy seeded by the Send payload. The plan carries the dispatched
    # statuses for downstream handle_reply matching.
    return _Command(goto=sends, update={"dispatch_plan": plan})


def route_after_dispatch_next(state: GroupState) -> str:
    """Group-graph router after ``dispatch_next`` (preserved verbatim from resident).

    ``node_dispatch_next_group`` returns ``Command(goto=...)`` directly when it
    fans out (the ``Send``s drive the agent nodes), so this router is only
    consulted on the no-dispatchable-step branches:

    - ``action_taken == "summarize"`` → ``summarize`` (all done, summarize).
    - else → END (nothing dispatchable, in-flight steps running — their
      report-back re-enters the next turn).

    Same routing as the resident ``lambda s: "summarize" if s.get("action_taken")
    == "summarize" else END``. Kept as a named function (not a lambda) so it is
    introspectable + contract-testable (vh5-style dead-branch audit).
    """
    if state.get("action_taken") == "summarize":
        return "summarize"
    return END


async def node_summarize(state: CoordinatorState) -> dict:
    """All steps done: summarize results, reply, clear plan (Rust dispatch_all_done)."""
    plan = state.get("dispatch_plan") or []
    summary = format_step_summary(plan)
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        f"🎉 全部完成！协作结果汇总：\n{summary}",
    )
    # Emit an empty plan so the frontend drops the resident plan card before
    # the engine clears its in-memory _dispatch_plan. Mirrors reset_session
    # (backend/api/groups.py) which emits emit_coordinator_plan(g, c, []) on
    # wipe — without this emit the card lingers with stale pending steps and
    # a stray confirm could 409.
    try:
        await emit_coordinator_plan(state["group_id"], state["agent_id"], [])
    except Exception:
        logger.exception("[coordinator] failed to emit empty plan on summarize")
    # clear the plan
    return {"dispatch_plan": []}


async def node_handle_reply_group(state: GroupState) -> Command:
    """Group-graph handle_reply: receive an agent node's report in-graph.

    Task: coordinator handle_reply + summarize 节点迁移到群图 — handle_reply 接收
    agent 节点报告（MT-15 失败恢复 + MT-14 步骤调整），不再走 inbox notify 回路.

    The group-graph twin of the resident ``node_handle_reply``. In the resident
    coordinator engine, a worker's report-back arrives as an ``agent_reply``
    inbox notify (``registry._run_worker_task`` → ``push_notify("agent_reply",
    ...)`` → the coordinator engine's run loop → ``_handle_notify`` → fresh-input
    ainvoke with ``incoming_kind="agent_reply"`` + ``incoming_data={task_id,
    success}`` → ``classify`` → ``route_after_classify`` → ``handle_reply``).
    That is the **inbox notify loop** the task retires for the group graph.

    In the group graph, the dispatched step's agent node (``worker.make_agent_node``,
    fanned out via ``node_dispatch_next_group``'s ``Send``s) speaks its reply and
    ends its turn with ``Command(goto=END)`` — but before ENDing it can route the
    report back to this coordinator sub-node by emitting a ``Command(goto=
    "handle_reply_group", update={...})`` instead of ``Command(goto=END)``. This
    node then runs the SAME MT-15 (``_maybe_handle_step_failure``) + MT-14
    (``_maybe_adjust_remaining_steps``) recovery logic as the resident
    ``node_handle_reply`` — the only difference is the transport (in-graph
    ``Command(goto=...)`` vs out-of-band ``push_notify``), not the recovery logic.

    **Why a Command return, not a dict**: the resident ``node_handle_reply``
    returns a dict (``{dispatch_plan, action_taken}``) and the resident
    ``route_after_handle_reply`` (conditional edge) routes to summarize /
    dispatch_next / llm_decide. The group-graph twin returns a ``Command(goto=...)``
    so it can directly fan out to the next agent nodes (via
    ``node_dispatch_next_group``'s ``Send``s) OR goto summarize, without going
    through a conditional-edge router — the routing decision is made in-node
    (the same decision the resident ``route_after_handle_reply`` makes, just
    expressed as a ``Command.goto`` target rather than a router return string).
    This keeps the MT-15/MT-14 branching identical (matched task_id → mark
    completed/failed → MT-15 recovery on failure / MT-14 adjust on success →
    all_done? summarize : dispatch_next) while letting the group graph fan out
    in-graph via ``Send``.

    The resident ``node_handle_reply`` + ``route_after_handle_reply`` are kept
    verbatim for the resident coordinator engine (its consumers — mt13/mt14/mt15/
    mt16/mt17 — keep patching + asserting on them unchanged). This twin is
    additive.

    Routing contract (preserved verbatim from ``route_after_handle_reply``):

    - matched_idx is None (no dispatched step for this task_id — e.g. a stray
      report) → ``llm_decide`` (fall back to the Leader LLM, same as resident).
    - all steps done after marking + recovery → ``summarize``.
    - failed step reset to pending (retry/reassign) → ``dispatch_next_group``
      (re-fan-out the ready step).
    - otherwise (more pending steps, possibly MT-14-adjusted) →
      ``dispatch_next_group`` (fan out the now-ready steps).
    """
    from langgraph.types import Command as _Command
    from langgraph.graph import END as _END

    # Reuse the resident handle_reply body for the mark + MT-15 + MT-14 logic:
    # it reads state.get(...) (duck-typed), and GroupState has every key it
    # touches (incoming_data/incoming_message/dispatch_plan/agent_id/group_id/
    # system_prompt/memory/leader_strategy — the task-6 schema union). We inline
    # the same steps rather than calling node_handle_reply (which returns a dict
    # + sets action_taken for a conditional-edge router) so we can translate the
    # dict result into a Command.goto target here.
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
        # no matching dispatched step -> fall back to LLM decision (same as
        # resident route_after_handle_reply's default → llm_decide).
        return _Command(goto="llm_decide", update={"dispatch_plan": plan})

    plan[matched_idx]["status"] = "completed" if success else "failed"
    plan[matched_idx]["result"] = content

    # MT-15: on a worker failure, ask the LLM whether to retry or degrade
    # BEFORE the all-done check (same ordering + cap as the resident path).
    if not success:
        plan = await _maybe_handle_step_failure(state, plan, matched_idx)
        # after retry/reassign the step may be pending again (re-dispatched);
        # skip may have marked it completed (degraded). Re-evaluate all_done.
        if all(s.get("status") in ("completed", "failed") for s in plan):
            # GROUP twin: goto the registered ``summarize_group`` node, NOT the
            # resident-only ``summarize`` (unregistered in the group graph →
            # "wrote to unknown channel branch:to:summarize, ignoring it" → no
            # summary reply → mt14 checks 8/9 fail). See task #37 root cause #3.
            return _Command(goto="summarize_group", update={"dispatch_plan": plan,
                                                      "action_taken": "summarize"})
        # if the failed step was reset to pending (retry/reassign), skip the
        # MT-14 success-side adjustment — there are no fresh results to adjust
        # on. dispatch_next_group will fan out the ready (re-dispatched) step.
        if plan[matched_idx].get("status") == "pending":
            return _Command(goto="dispatch_next_group", update={"dispatch_plan": plan})

    all_done = all(s.get("status") in ("completed", "failed") for s in plan)
    if all_done:
        # GROUP twin: goto the registered ``summarize_group`` node, NOT the
        # resident-only ``summarize`` (unregistered in the group graph → goto
        # ignored → no summary reply → mt14 checks 8/9 fail).
        # See task #37 root cause #3.
        return _Command(goto="summarize_group", update={"dispatch_plan": plan,
                                                  "action_taken": "summarize"})

    # MT-14: only ask the LLM to revise the remaining pending steps when this
    # report completed successfully (same gate as the resident path).
    pending_steps = [s for s in plan if s.get("status") == "pending"]
    if success and pending_steps:
        plan = await _maybe_adjust_remaining_steps(state, plan)

    # Fan out the now-possibly-revised ready steps via the group-graph
    # dispatch_next (Send fan-out). The plan carries the completed/failed +
    # possibly-adjusted pending steps.
    return _Command(goto="dispatch_next_group", update={"dispatch_plan": plan})


async def node_summarize_group(state: GroupState) -> Command:
    """Group-graph summarize: all steps done → reply + clear plan, in-graph end.

    The group-graph twin of the resident ``node_summarize``. Runs the SAME
    summary reply (``format_step_summary``) + emit empty plan + clear plan logic,
    then returns ``Command(goto=END)`` to end the turn in-graph (the resident
    ``node_summarize`` returns ``{"dispatch_plan": []}`` and the resident graph's
    ``summarize → END`` edge ends it — the group graph expresses the same as a
    ``Command.goto=END``).

    The summary reply goes through ``_unified_reply`` (persist + emit + the reply
    callback) exactly as the resident path — single source for the agent_reply
    shape + the Leader's summary bubble.
    """
    from langgraph.types import Command as _Command
    from langgraph.graph import END as _END

    plan = state.get("dispatch_plan") or []
    summary = format_step_summary(plan)
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        f"🎉 全部完成！协作结果汇总：\n{summary}",
    )
    try:
        await emit_coordinator_plan(state["group_id"], state["agent_id"], [])
    except Exception:
        logger.exception("[coordinator] failed to emit empty plan on summarize_group")
    # clear the plan + end the turn in-graph.
    return _Command(goto=_END, update={"dispatch_plan": []})


# ── routing ───────────────────────────────────────────────────────────────


def route_after_classify(state: CoordinatorState) -> str:
    """Route after the classify node.

    Three branches:
    - ``dispatch_next`` (PL-02): a user confirmed a pending plan — classify set
      ``action_taken="confirm_dispatch"``. The graph jumps straight to fan-out,
      resuming the resident plan that was left waiting in the engine's
      ``_dispatch_plan`` after ``node_dispatch`` announced it and ENDED
      (方案 B 引擎内存态等待). This closes the confirm-resume loop without
      going through the coordinator LLM.
    - ``handle_reply``: a worker reported back on a dispatched step.
    - ``llm_decide``: everything else (new user demand) → coordinator LLM.
    """
    action = state.get("action_taken", "")
    if action == "confirm_dispatch":
        return "dispatch_next"
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


def route_after_dispatch(state: CoordinatorState) -> str:
    """PL-02/PL-03: fan out after the plan is announced, else end.

    ``node_dispatch`` returns ``action_taken="dispatch_next"`` in two cases:
    - ``auto_confirm`` / direct-run mode (PL-03): the node skipped the
      interrupt and wants immediate fan-out.
    - resume turn: ``Command(resume=...)`` re-entered ``node_dispatch``, the
      second ``interrupt()`` call returned the resume value immediately, and
      the node fell through to fan out the (possibly amended) plan.

    On the confirm-await interrupt turn ``node_dispatch`` calls ``interrupt()``
    which suspends the graph *inside* the node before it returns — so this
    router is not even called that turn (no ``action_taken`` is set; the plan
    is checkpointed via the ``replace_value`` reducer on the auto-confirm
    partial return / the interrupt's own checkpointing, and a later
    ``Command(resume=...)`` completes the loop). Any ``action_taken`` other
    than ``dispatch_next`` falls through to ``END`` — a defensive default that
    is unreachable on the normal path, since ``node_dispatch`` only ever
    returns ``dispatch_next`` (B8: the earlier ``("dispatch_next",
    "confirm_dispatch", "direct_run")`` tuple carried two dead members —
    ``confirm_dispatch`` is produced only by ``node_classify_incoming`` and
    routed by ``route_after_classify`` straight to ``dispatch_next``, never
    reaching the dispatch node; ``direct_run`` was never produced by any node).
    """
    action = state.get("action_taken", "")
    if action == "dispatch_next":
        return "dispatch_next"
    return END


# ── graph builder ─────────────────────────────────────────────────────────


def build_coordinator_subnodes(coordinator_id: str = "", coordinator_name: str = "",
                               system_prompt: str = "") -> dict:
    """Build the coordinator sub-nodes for wiring into the group graph.

    Task: coordinator.py 把 classify/llm_decide/chat 节点改造为群图内 coordinator
    子节点，状态读写改用 GroupState，保 route_after_* 条件边语义.

    The resident ``build_coordinator_graph`` keeps a self-contained 7-node graph
    (``CoordinatorState`` schema) for the live coordinator engine until the full
    group-graph migration swaps consumers over. This helper packages the same
    node functions (``node_classify_incoming`` / ``node_llm_decide`` / ``node_chat``
    / ``node_dispatch`` / ``node_handle_reply`` / ``node_dispatch_next`` /
    ``node_summarize``) + the four ``route_after_*`` routers as a dict so
    ``group_graph.build_group_graph`` can register them as the centralized-path
    sub-nodes of the single-graph-per-group topology — the coordinator sits
    alongside the agent (member) ``agent_<id>`` nodes in ONE compiled graph.

    **Why the node code is reused unchanged**: the node functions already read
    every state key via ``state.get(...)`` / ``state[...]`` (duck-typed dict
    access), NOT via TypedDict attribute access. ``CoordinatorState`` and
    ``GroupState`` share all the keys the coordinator nodes touch — ``group_id``,
    ``agent_id``, ``agent_name``, ``system_prompt``, ``incoming_*``,
    ``dispatch_plan``, ``memory``, ``auto_confirm``, ``leader_strategy``,
    ``action_taken``, ``reply_content``, ``_stream_stats``. The resident graph
    injects ``agent_id`` = the group's Leader agent_id (the engine is the
    coordinator's AgentEngine); in the group graph the same field is injected
    at ``invoke_turn`` time. So the node code that does ``state["agent_id"]``
    resolves to the Leader in BOTH graphs without a code change — the
    state-read/write migration is a schema union, not a code rewrite.

    **The three sub-nodes named in the task** (classify / llm_decide / chat) plus
    the four the centralized path needs (dispatch / dispatch_next / handle_reply
    / summarize) are all returned — wiring only classify+llm_decide+chat would
    leave dispatch's ``interrupt()`` (PL-02) and the DAG fan-out (dispatch_next)
    without a home, breaking the resident coordinator's plan-confirmation +
    parallel-dispatch semantics the group graph is meant to preserve. All seven
    are returned together; ``build_group_graph`` wires whichever it needs (a
    later task wires the full centralized path; this task packages the nodes).

    Args:
        coordinator_id: the group's Leader agent_id. Stamped onto the returned
            node specs (``agent_id``) so a later wiring task can register the
            sub-nodes with identity bound, mirroring ``worker.build_agent_node``'s
            closure-binding of member identity. The node code reads
            ``state["agent_id"]`` at runtime — the value injected there at
            ``invoke_turn`` is the Leader (single source), so this is only the
            build-time annotation, NOT a hard-wired override.
        coordinator_name: the Leader's display name (build-time annotation).
        system_prompt: the Leader's persona (build-time annotation; ``_leader_system``
            reads ``state["system_prompt"]`` at runtime, so this is only an
            annotation for symmetry with the agent-node factory).

    Returns a dict mapping node name → node callable, plus the routers, all
    importable from this module. The keys mirror the resident graph's node names
    verbatim so ``add_conditional_edges`` path maps stay identical. The
    group-graph dispatch_next twin (``node_dispatch_next_group`` + ``route_after_dispatch_next``)
    is included so a later wiring task can swap the resident ``dispatch_next``
    sub-node for the ``Send``-fan-out variant without touching this factory's
    callers:

        {"classify": node_classify_incoming, "llm_decide": node_llm_decide,
         "chat": node_chat, "dispatch": node_dispatch,
         "handle_reply": node_handle_reply, "dispatch_next": node_dispatch_next,
         "summarize": node_summarize,
         # group-graph dispatch_next twins (LangGraph Send fan-out):
         "dispatch_next_group": node_dispatch_next_group,
         "route_after_dispatch_next": route_after_dispatch_next,
         "route_after_classify": route_after_classify,
         "route_after_handle_reply": route_after_handle_reply,
         "route_after_llm_decide": route_after_llm_decide,
         "route_after_dispatch": route_after_dispatch,
         "_coordinator_id": coordinator_id,
         "_coordinator_name": coordinator_name,
         "_system_prompt": system_prompt}

    The routers are returned verbatim too — they read ``state.get("action_taken")``
    only, which exists on BOTH ``CoordinatorState`` and ``GroupState`` (the union
    landed in this task), so ``route_after_*`` conditional-edge semantics are
    preserved byte-for-byte without modification. The group-graph
    ``dispatch_next_group`` node returns ``Command(goto=...)`` (not a dict), so
    when it is wired in place of ``dispatch_next`` the conditional-edge router
    after it is bypassed on the fan-out branch (LangGraph follows the ``Send``s
    directly) — only consulted on the no-dispatchable-step branches (summarize /
    END), which ``route_after_dispatch_next`` handles.
    """
    return {
        "classify": node_classify_incoming,
        "llm_decide": node_llm_decide,
        "chat": node_chat,
        "dispatch": node_dispatch,
        "handle_reply": node_handle_reply,
        "dispatch_next": node_dispatch_next,
        "summarize": node_summarize,
        # group-graph dispatch_next twin (LangGraph Send fan-out, task-8):
        # a later wiring task swaps ``dispatch_next``→``dispatch_next_group`` +
        # adds ``route_after_dispatch_next`` for the no-dispatch branches.
        "dispatch_next_group": node_dispatch_next_group,
        "route_after_dispatch_next": route_after_dispatch_next,
        # group-graph handle_reply + summarize twins (task-9): receive an agent
        # node's report in-graph (no inbox notify loop), run the same MT-15 +
        # MT-14 recovery, then Command(goto=...) to dispatch_next_group /
        # summarize / llm_decide.
        "handle_reply_group": node_handle_reply_group,
        "summarize_group": node_summarize_group,
        "route_after_classify": route_after_classify,
        "route_after_handle_reply": route_after_handle_reply,
        "route_after_llm_decide": route_after_llm_decide,
        "route_after_dispatch": route_after_dispatch,
        "_coordinator_id": coordinator_id,
        "_coordinator_name": coordinator_name,
        "_system_prompt": system_prompt,
    }


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
        {
            "dispatch_next": "dispatch_next",
            "handle_reply": "handle_reply",
            "llm_decide": "llm_decide",
        },
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
    g.add_conditional_edges(
        "dispatch",
        route_after_dispatch,
        {"dispatch_next": "dispatch_next", END: END},
    )
    g.add_conditional_edges(
        "dispatch_next",
        lambda s: "summarize" if s.get("action_taken") == "summarize" else END,
        {"summarize": "summarize", END: END},
    )
    g.add_edge("chat", END)
    g.add_edge("summarize", END)

    compiled = g.compile(checkpointer=MemorySaver())
    # Publish the compiled graph so node helpers (``_detect_residual_interrupt``)
    # can call ``aget_state(config)`` without the engine threading the graph
    # object through every node. Set on the default context: each engine's
    # run-loop task copies this context at creation, so concurrent engines each
    # see the same graph (read-only post-compile). The graph is a singleton —
    # every coordinator engine compiles its own via this function, so the last
    # compile wins; that is fine because all coordinator graphs are structurally
    # identical and only the per-thread ``config`` distinguishes them.
    _GRAPH_INSTANCE.set(compiled)
    return compiled


# ── decision parser (Rust parse_coordinator_decision) ─────────────────────


# ── coordinator streaming helpers ─────────────────────────────────────────


# Coordinator LLM output is a JSON envelope ({"action","content","plan"}). The
# user-visible text lives in the ``content`` string field. While the LLM
# streams, we receive raw JSON fragment deltas (tokens) — not the decoded
# content value. To render the reply token-by-token we must *decode on the fly*:
# feed the deltas to a small JSON-aware state machine that, once it enters the
# ``content`` field's string value, emits each decoded character as soon as it
# arrives (honouring ``\"``/``\\``/``\n`` escapes), and stays silent on the JSON
# skeleton (keys, braces, the ``action``/``plan`` fields).
class _ContentExtractor(ContentExtractor):
    """Backward-compat alias for the in-coordinator name (split to llm.json_stream, B9).

    Kept so any external/test reference to ``engine.coordinator._ContentExtractor``
    still resolves after the class moved to ``llm.json_stream.ContentExtractor``.
    The coordinator's own call sites use the public ``ContentExtractor`` directly
    (see ``_stream_coordinator_decision``).

    B32 死代码重巡航核实：全仓 grep ``_ContentExtractor`` 的非 tests/ 引用为零——
    coordinator.py:1356 + worker.py:172 都用 ``ContentExtractor()``（公共名），
    无源码消费者经此别名导入。但 test_vh6 [B6] 显式锁定此别名存在（锁「向后兼容
    别名保留」契约），故保留不删——删了破 vh6 回归。保留理由：别名是 B9 抽出时的
    有意过渡保留（防外部插件/未来 import 断），vh6 把「保留」本身锁成契约；真要
    删需先同步删 vh6 [B6] 断言 + 评估外部 import 面，非本轮「逐个核实」范围。
    """


async def _stream_coordinator_decision(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    group_id: str,
    coordinator_id: str,
) -> tuple[str, str, int, int, str, int]:
    """Stream the coordinator LLM, emitting per-token + live-stats events.

    Consumes ``chat_completion_stream``: each ``(content_delta, reasoning_delta,
    completion_tokens, reasoning_tokens)`` chunk feeds the ``ContentExtractor``
    (only the decoded ``content`` field value is pushed to the frontend via
    ``emit_coordinator_token`` — the JSON skeleton/keys are never rendered as
    reply text). Reasoning-model ``reasoning_content`` deltas are pushed
    separately via ``emit_coordinator_reasoning`` so the frontend can render a
    collapsed "思考过程" panel. Live statistics (``emit_coordinator_stats``) are
    emitted ~every 200ms during the stream and once more at the end with
    ``phase="done"`` and the real ``completion_tokens`` / ``reasoning_tokens``.

    Returns ``(reply_id, raw_full, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text)``:

    - ``reply_id`` — the UUID per-turn streaming key (so the caller can stamp
      it onto the persisted agent_reply's ``data`` and the frontend can keep the
      stats line alive after the streaming bubble retires).
    - ``raw_full`` — the assembled raw LLM output (visible ``content`` only) for
      ``extract_json`` to parse action/plan (unchanged from the non-streaming
      path; reasoning_content is NOT part of raw_full — it's not the reply).
    - ``tokens`` — the final token count (real ``completion_tokens`` if the
      provider sent usage, else the coarse char-based estimate).
    - ``elapsed_ms`` — total wall-clock from stream start to finish.
    - ``model`` — the LLM model id that produced this reply (``config["model"]``),
      surfaced through stats + persisted data so the bubble can show *which*
      model answered (the user can hot-switch models via the provider catalog).
    - ``reasoning_tokens`` — how many of ``tokens`` were the model's internal
      reasoning chain (0 for non-reasoning models). Surfaced through stats +
      persisted data so the status line can show "含 N 推理" and the bubble
      can render a reasoning panel — otherwise a 5-word reply showing 148
      tokens looks fake when 133 were invisible reasoning.
    - ``reasoning_text`` — the full assembled ``reasoning_content`` (empty for
      non-reasoning models). Persisted onto ``agent_reply.data["reasoning"]``
      so the finalized bubble's collapsible panel can expand the reasoning even
      after the live ``coordReasoning`` buffer is cleared on ``phase="done"``.
    """
    # 命名口径（见 docs/naming-conventions.md §2.2）：reply_id 是裸 uuid hex（无 `task_`
    # 前缀），作为单轮流式归并键，与 worker._stream_brain_decision 的 reply_id 同构。
    reply_id = uuid.uuid4().hex
    model = str(config.get("model") or "")
    extractor = ContentExtractor()
    raw_parts: list[str] = []
    # reasoning_content 全文累积——落盘到 agent_reply.data.reasoning，定稿气泡的
    # 折叠区据此展开（流式期靠 coordinator_reasoning 事件，定稿后靠持久化文本，
    # 否则 phase=done 清空 coordReasoning 后用户无法再展开历史气泡的推理）。
    reasoning_parts: list[str] = []
    final_tokens = 0
    final_reasoning_tokens = 0
    # throttle stats emits to ~200ms; Date.now()/time is fine here (engine side)
    start = time.monotonic()
    last_stats_ts = 0.0
    # running estimate of emitted content chars → a coarse token estimate for
    # the live counter before the authoritative usage chunk arrives
    live_tokens = 0
    # running estimate of emitted reasoning chars → a coarse token estimate for
    # the live reasoning counter (reasoning_tokens only lands on the final chunk)
    live_reasoning_tokens = 0

    async for content_delta, reasoning_delta, usage, reasoning_usage in chat_completion_stream(config, messages):
        if reasoning_delta:
            live_reasoning_tokens += max(1, len(reasoning_delta) // 3)
            reasoning_parts.append(reasoning_delta)
            try:
                await emit_coordinator_reasoning(
                    group_id, coordinator_id, reply_id, reasoning_delta
                )
            except Exception:
                logger.exception("[coordinator] failed to emit reasoning delta")
        if content_delta:
            raw_parts.append(content_delta)
            extractor.feed(content_delta)
            piece = extractor.take()
            if piece:
                live_tokens += max(1, len(piece) // 3)
                # B28 错误出口统一：与 emit_coordinator_reasoning(1383) /
                # emit_coordinator_stats(1411/1433) 同款 best-effort + logger.exception
                # ——WS 推送失败只跳过当前 token delta 不中断流式。原裸 await 会把单次
                # emit 异常冒泡出整个 _stream_coordinator_decision，被 node_llm_decide
                # 粗兜底成 chat 兜底回复，丢失后续 token + stats + reasoning（一次 WS
                # 抖动整条回复报废，与 reasoning/stats 的容忍度不对称）。
                try:
                    await emit_coordinator_token(group_id, coordinator_id, reply_id, piece)
                except Exception:
                    logger.exception("[coordinator] failed to emit token delta")
        if usage is not None:
            final_tokens = usage
        if reasoning_usage is not None:
            final_reasoning_tokens = reasoning_usage
        # throttled stats: at most every 200ms, + a final emit after the loop
        now = time.monotonic()
        if now - last_stats_ts >= 0.2:
            elapsed_ms = int((now - start) * 1000)
            try:
                await emit_coordinator_stats(
                    group_id,
                    coordinator_id,
                    reply_id,
                    elapsed_ms,
                    live_tokens,
                    "streaming",
                    model,
                    live_reasoning_tokens,
                )
            except Exception:
                logger.exception("[coordinator] failed to emit streaming stats")
            last_stats_ts = now

    raw_full = "".join(raw_parts)
    reasoning_text = "".join(reasoning_parts)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    real_tokens = final_tokens if final_tokens else live_tokens
    real_reasoning_tokens = (
        final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens
    )
    try:
        await emit_coordinator_stats(
            group_id,
            coordinator_id,
            reply_id,
            elapsed_ms,
            real_tokens,
            "done",
            model,
            real_reasoning_tokens,
        )
    except Exception:
        logger.exception("[coordinator] failed to emit final stats")
    return reply_id, raw_full, real_tokens, elapsed_ms, model, real_reasoning_tokens, reasoning_text


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
