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
    """Classify the incoming notify: agent_reply with a matching dispatched step vs new demand.

    Three branches:
    - ``confirm_dispatch`` (PL-02): the user confirmed a pending plan. Detected
      when ``incoming_kind == "plan_confirm"`` (an explicit marker pushed by
      the plan-confirm API) AND the resident ``dispatch_plan`` still has at
      least one pending step. Falls through to ``llm_decide`` if the plan was
      already dispatched/cleared, so a stray confirm can't re-fire a dead plan.
    - ``handle_reply``: a worker reported back — an ``agent_reply`` notify whose
      ``data.task_id`` matches a dispatched step.
    - ``llm_decide``: everything else (new user demand) → coordinator LLM.
    """
    kind = state.get("incoming_kind", "")
    sender = state.get("incoming_sender", "")

    # PL-02: explicit user plan-confirmation
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
            [{"role": "system", "content": COORDINATOR_SYSTEM},
             {"role": "user", "content": prompt}],
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


def _parse_plan_adjust_decision(raw: str) -> dict | None:
    """Parse the plan-adjustment LLM response into {adjust, reason, announce, revised_steps}.

    Lenient: a missing/invalid ``adjust`` defaults to False (no change), and a
    non-list ``revised_steps`` is treated as empty. Returns None on JSON parse
    failure so the caller keeps the plan unchanged.
    """
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
            [{"role": "system", "content": COORDINATOR_SYSTEM},
             {"role": "user", "content": prompt}],
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
    """Store the plan, announce it, then either wait for confirm or fan out.

    Rust engine.rs 586-599. The LLM-returned plan replaces the engine's
    dispatch_plan (returned via the reducer). The announcement reply goes
    through the unified path so it persists + emits.

    PL-02/PL-03: by default the plan is *announced but not dispatched* — the
    node returns ``action_taken="wait_confirm"`` and the graph edges to END,
    leaving the plan resident in the engine's ``_dispatch_plan`` so a later
    confirm message can resume via ``dispatch_next`` (方案 B 引擎内存态等待).
    When ``auto_confirm`` is True ("直接干" mode, PL-03) the node tags the plan
    ``confirm_mode="auto"`` and returns ``action_taken="direct_run"`` so
    ``route_after_dispatch`` routes straight to fan-out, preserving the old
    zero-confirmation behaviour.
    """
    plan = state.get("dispatch_plan") or []
    plan_summary = "\n".join(
        f"{s.get('step')}. {s.get('agent_name', '')} → {s.get('instruction', '')[:40]}..."
        for s in plan
    )
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
        state["group_id"], state["agent_id"], state.get("dispatch_plan") or []
    )
    # PL-03 直接干: auto_confirm routes the conditional edge straight to
    # dispatch_next. We tag the plan as a direct_run so route_after_dispatch
    # distinguishes "auto fan-out" from a later user confirmation
    # (confirm_dispatch) — both land on dispatch_next, but the provenance is
    # explicit and the plan card on the front-end can reflect it.
    plan = state.get("dispatch_plan") or []
    if state.get("auto_confirm"):
        for s in plan:
            s.setdefault("confirm_mode", "auto")
    return {
        "action_taken": "direct_run" if state.get("auto_confirm") else "wait_confirm"
    }


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
    """PL-02/PL-03: after announcing the plan, either wait for confirm or fan out.

    node_dispatch sets ``action_taken`` to ``dispatch_next`` (auto_confirm /
    "直接干" mode, PL-03) or ``wait_confirm`` (default, needs user confirmation).
    In the wait case the graph ends here — the plan stays resident in the
    engine's ``_dispatch_plan`` and is resumed by a later confirm message that
    routes through classify → dispatch_next (方案 B 引擎内存态等待).

    PL-03 "直接干" is realized entirely here: when ``auto_confirm`` is True,
    node_dispatch marks the plan ``direct_run`` so this router sends the graph
    straight to fan-out, skipping the confirmation step the user would otherwise
    take manually.
    """
    action = state.get("action_taken", "")
    if action in ("dispatch_next", "confirm_dispatch", "direct_run"):
        return "dispatch_next"
    return END


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
