"""Plan-confirmation routes — M12 PL-02/PL-03 (LangGraph native interrupt resume).

Routes map to frontend `planApi`:
  POST /api/groups/{groupId}/plan/confirm   → plan_resume (resume waiting plan)
  POST /api/groups/{groupId}/plan/direct    → plan_direct (switch 直接干 mode + resume)
  POST /api/groups/{groupId}/plan/modify    → plan_modify (amend steps, then confirm)

The coordinator announces a plan via ``node_dispatch`` then pauses the thread
via LangGraph ``interrupt({"plan": plan})`` (default, ``auto_confirm=False``),
leaving the plan checkpointed in the graph's MemorySaver checkpointer (the
source of truth). These endpoints are the user-facing wake-up that resumes the
paused dispatch node: they push a ``plan_resume`` notify onto the coordinator's
inbox via ``route_plan_resume``; the coordinator engine's ``_handle_notify``
sees ``type == "plan_resume"`` and dispatches it to the graph as
``Command(resume=<payload>)`` — ``node_dispatch``'s ``interrupt()`` returns the
payload and the graph fans out the pending steps, skipping the LLM (the plan was
already LLM-decided on the dispatch turn, so confirming is a pure resume).

``/direct`` flips the group's ``config.auto_confirm=True`` so future plans in the
same group auto-dispatch; ``/modify`` splices the user's amended steps into the
resident plan before confirming. ``/confirm`` is the plain "continue as planned"
resume carrying ``{"mode": "confirm"}``.

The legacy ``plan_confirm`` fresh-input notify channel (the removed
``route_plan_confirm`` pusher) was retired in task 11 — plan-confirm no longer
goes through the notify-as-fresh-input channel at all. These endpoints are the
sole plan-confirm inbound path, all via the native resume channel
(``route_plan_resume``).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.mention import route_plan_resume
from engine.registry import registry
from events import emit_coordinator_plan
from store import crud

logger = logging.getLogger("multi-agent.plan")

router = APIRouter(prefix="/api/groups", tags=["plan"])


class PlanModifyStep(BaseModel):
    """One amended step in a plan-modify request (all fields optional bar step)."""

    model_config = {"extra": "allow"}

    step: int
    agent_id: str | None = None
    agent_name: str | None = None
    instruction: str | None = None
    depends_on: list[int] | None = None


class PlanModifyBody(BaseModel):
    """Body for POST /plan/modify — amended steps replace the resident plan."""

    model_config = {"extra": "allow"}

    steps: list[PlanModifyStep] = Field(default_factory=list)


async def _require_coordinator_runtime(group_id: str):
    """Resolve the group + its per-group ``GroupRuntime`` (the plan host).

    task-19③: the plan endpoints now read/resume against the per-group
    ``GroupRuntime`` (the decentralized swarm graph's turn controller) instead
    of the resident per-agent ``AgentEngine``. The plan's source of truth — the
    paused ``dispatch`` interrupt + the ``dispatch_plan`` mirror — lives on the
    runtime (``rt._dispatch_plan`` + the runtime's checkpointer thread), so the
    pending guards + the modify patch source read ``rt``. 404 if the group row
    is gone; 409 if it has no coordinator or the runtime won't resolve/build
    (cold race that ``ensure_runtime`` couldn't satisfy, or a compile failure).
    """
    group = await crud.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    if not group.coordinator_id:
        raise HTTPException(status_code=409, detail="group has no coordinator")
    try:
        rt = await registry.ensure_runtime(group_id)
    except Exception:
        # ensure_runtime only returns None when the group row is gone (already
        # 404'd above); a real failure here is a compile error — degrade to 409
        # rather than 500-ing a plan-confirm click (B31 错误处理重巡航：降级语义
        # 保留 + debug 可观测，与 _read_resident_plan 的 checkpointer 降级同款).
        logger.debug(
            "[plan] ensure_runtime failed for group %s — degrading to 409",
            group_id, exc_info=True,
        )
        rt = None
    if rt is None:
        raise HTTPException(status_code=409, detail="coordinator runtime not running")
    return group, rt


@router.get("/{group_id}/plan")
async def plan_get(group_id: str) -> dict[str, Any]:
    """Return the coordinator's resident plan (PL-10 重连后重拉历史).

    task-19③: the plan lives on the per-group ``GroupRuntime`` (the
    decentralized group graph's turn controller) — the source of truth since
    the handoff migration. The runtime's checkpointer thread holds the
    ``dispatch`` interrupt + the ``dispatch_plan`` state (the same thread a
    prior ``invoke_turn`` paused at ``node_dispatch``'s ``interrupt()``). On a
    frontend WS reconnect the bus may have dropped ``coordinator_plan``
    events, so this endpoint lets the client re-fetch the authoritative current
    plan by reading the runtime's checkpointer thread state
    (``rt._graph.get_state(thread_id).values.get("dispatch_plan")``). If the
    runtime isn't built (group fresh / backend just restarted and hasn't
    re-seeded via ``load_from_store``) — or its thread has no checkpoint yet
    (never reached ``node_dispatch``) — returns an empty plan; an absent
    runtime or empty thread is not an error condition here.
    """
    group = await crud.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    plan: list[dict[str, Any]] = []
    if group.coordinator_id:
        rt = registry.get_runtime(group_id)
        if rt is not None:
            plan = await _read_resident_plan(rt)
    return {
        "ok": True,
        "group_id": group_id,
        "coordinator_id": group.coordinator_id or "",
        "plan": plan,
    }


async def _read_resident_plan(rt) -> list[dict[str, Any]]:
    """Read the resident plan from the runtime's checkpointer (source of truth).

    Prefers the group graph's checkpointer thread state
    (``rt._graph.get_state(thread_id).values.get("dispatch_plan")``) — the
    single source of truth since the handoff migration — falling back to the
    runtime's ``_dispatch_plan`` mirror only if the checkpointer read fails
    (best-effort: a checkpointer error degrades to the mirror rather than
    500-ing the read).

    Thread-id key (the坑 task-19③ carries over from ``resume_plan``):
    ``GroupRuntime`` mints a FRESH thread per ``invoke_turn``
    (``{thread_id}:{seq}``); the thread a dispatch paused on is the runtime's
    last turn's thread — read with the same key ``resume_plan`` uses
    (``group_runtime.py:811``): ``f"{rt.thread_id}:{rt._turn_seq}"`` if a turn
    has run, else ``rt.thread_id`` (a cold runtime with ``_turn_seq==0`` that
    never invoked → no checkpoint → returns ``[]``, matching a cold
    coordinator's behavior).
    """
    try:
        thread_id = (
            f"{rt.thread_id}:{rt._turn_seq}" if getattr(rt, "_turn_seq", 0) else rt.thread_id
        )
        graph = getattr(rt, "_graph", None) or getattr(rt, "graph", None)
        snapshot = graph.get_state(
            config={"configurable": {"thread_id": thread_id}}
        )
        cp_plan = (snapshot.values or {}).get("dispatch_plan")
        if cp_plan:
            return [dict(s) for s in cp_plan]
        # No ``dispatch_plan`` in the checkpointed thread state — fall through to
        # the mirror (a runtime that reached dispatch syncs it; a cold/idle
        # runtime has [] there too). Returning [] here would mask a populated
        # mirror on a thread that checkpointed no dispatch_plan (e.g. a fresh
        # thread for an agent_reply peer-handoff turn), so prefer the mirror.
        mirror = getattr(rt, "_dispatch_plan", None)
        if mirror:
            return [dict(s) for s in mirror]
        return []
    except Exception:
        # best-effort: degrade to the mirror rather than 500-ing the read.
        # Logged at debug (not exception): a checkpointer read miss on a
        # cold/idle/auto_confirm-only runtime is the documented normal path
        # (no dispatch_plan checkpointed), so exception-level logging would flag
        # normal traffic as errors (B31 错误处理重巡航——原裸 `return` 吞掉异常
        # 不可观测；降级语义保留，补 debug + exc_info 让真 checkpointer 故障可查).
        logger.debug(
            "[plan] checkpointer state read failed for group %s — "
            "degrading to in-memory mirror", getattr(rt, "group_id", "?"),
            exc_info=True,
        )
        return [dict(s) for s in rt._dispatch_plan]


@router.post("/{group_id}/plan/confirm")
async def plan_confirm(group_id: str) -> dict[str, Any]:
    """Resume the resident plan as-is (user clicked 确认继续).

    Pushes a ``plan_resume`` notify to the coordinator carrying
    ``{"mode": "confirm"}``. The engine dispatches it to the graph as
    ``Command(resume={"mode": "confirm"})`` — ``node_dispatch``'s ``interrupt()``
    returns the payload and the graph fans out the pending steps via
    ``dispatch_next``, skipping the LLM. The plan is *not* mutated.
    """
    group, rt = await _require_coordinator_runtime(group_id)
    if not any(s.get("status") == "pending" for s in rt._dispatch_plan):
        raise HTTPException(status_code=409, detail="no pending plan to confirm")
    await route_plan_resume(group_id, {"mode": "confirm"})
    return {
        "ok": True,
        "group_id": group_id,
        "coordinator_id": group.coordinator_id,
        "mode": "confirm",
    }


@router.post("/{group_id}/plan/direct")
async def plan_direct(group_id: str) -> dict[str, Any]:
    """Switch the group to 直接干 (auto_confirm=True) and resume.

    Sets ``group.config.auto_confirm = True`` so future plans auto-dispatch
    without waiting, then resumes the resident plan via the native resume
    channel (``Command(resume={"mode": "direct"})``) — ``node_dispatch``'s
    ``interrupt()`` returns the payload and the graph fans out the pending
    steps. The mode persists in the group config across engine restarts.
    """
    group, rt = await _require_coordinator_runtime(group_id)
    # flip the config flag (merge so we don't clobber a co-existing leader_strategy
    # — crud.update_group now merges config keys additively, but build the merged
    # dict here too so the in-memory view stays consistent before the DB write)
    config = dict(group.config or {})
    config["auto_confirm"] = True
    await crud.update_group(group_id, _GroupConfigUpdate(config=config))
    # resume the resident plan if any
    resumed = False
    if any(s.get("status") == "pending" for s in rt._dispatch_plan):
        await route_plan_resume(group_id, {"mode": "direct"})
        resumed = True
    return {
        "ok": True,
        "group_id": group_id,
        "coordinator_id": group.coordinator_id,
        "mode": "direct",
        "auto_confirm": True,
        "resumed_resident_plan": resumed,
    }


@router.post("/{group_id}/plan/modify")
async def plan_modify(group_id: str, body: PlanModifyBody) -> dict[str, Any]:
    """Amend the resident plan's steps then confirm (user edited the plan).

    Merges each provided field into the matching step (by ``step`` number);
    steps absent from the body keep their existing values. The patched
    ``amended_steps`` are carried in the resume payload (``{"mode": "modify",
    "amended_steps": [...]}``) rather than written directly to the engine's
    mirror — ``node_dispatch``'s ``interrupt()`` returns the payload and
    ``_splice_amended_steps`` rewrites the plan in the checkpointer (the source
    of truth) before fan-out. The front-end PlanConfirmCard is pre-emptively
    re-announced with the patched plan so the card reflects the edit before
    the resume fans out.
    """
    group, rt = await _require_coordinator_runtime(group_id)
    if not rt._dispatch_plan:
        raise HTTPException(status_code=409, detail="no resident plan to modify")

    plan = [dict(s) for s in rt._dispatch_plan]
    by_step = {s.get("step"): s for s in plan}
    for patch in body.steps:
        target = by_step.get(patch.step)
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=f"step {patch.step} not found in resident plan",
            )
        patch_data = patch.model_dump(exclude_unset=True, exclude_none=True)
        patch_data.pop("step", None)  # step number is the key, not a field to set
        target.update(patch_data)
        # any amendment resets a completed/dispatched step back to pending so
        # the modified step (and its downstream deps) re-dispatch
        if patch_data:
            target["status"] = "pending"
            target["task_id"] = None
            target["result"] = None

    # re-announce the patched plan so the front-end card reflects the edit
    # immediately (the checkpointer is updated by node_dispatch on resume).
    await emit_coordinator_plan(group_id, group.coordinator_id, plan)
    # carry the patched steps in the resume payload — node_dispatch's interrupt
    # returns this and _splice_amended_steps rewrites the checkpointer's plan
    # (the source of truth) before fan-out. We no longer write engine._dispatch
    # _plan directly: the mirror is synced back from the graph result by
    # _handle_notify after the resume completes.
    await route_plan_resume(
        group_id, {"mode": "modify", "amended_steps": plan}
    )
    return {
        "ok": True,
        "group_id": group_id,
        "coordinator_id": group.coordinator_id,
        "mode": "modify",
        "plan": plan,
    }


class _GroupConfigUpdate(BaseModel):
    """Minimal partial-update carrier so update_group's payload.model_dump works."""

    model_config = {"extra": "allow"}

    config: dict[str, Any] | None = None
    name: str | None = None
    coordinator_id: str | None = None
    description: str | None = None
    status: str | None = None
