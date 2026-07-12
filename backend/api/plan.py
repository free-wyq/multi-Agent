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

The legacy ``plan_confirm`` fresh-input notify channel (``route_plan_confirm``)
remains as a compatibility fallback (task 11 will downgrade/remove it); these
endpoints now use the native resume channel.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.mention import route_plan_resume
from engine.registry import registry
from events import emit_coordinator_plan
from store import crud

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


async def _require_coordinator_engine(group_id: str):
    """Resolve the group + its coordinator engine, 404 if either is missing."""
    group = await crud.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    if not group.coordinator_id:
        raise HTTPException(status_code=409, detail="group has no coordinator")
    engine = registry.get_engine(group_id, group.coordinator_id)
    if engine is None:
        raise HTTPException(status_code=409, detail="coordinator engine not running")
    return group, engine


@router.get("/{group_id}/plan")
async def plan_get(group_id: str) -> dict[str, Any]:
    """Return the coordinator's resident plan (PL-10 重连后重拉历史).

    The plan lives in the coordinator engine's ``_dispatch_plan`` (方案 B
    内存态), never persisted to the group row. On a frontend WS reconnect the
    bus may have dropped ``coordinator_plan`` events, so this endpoint lets the
    client re-fetch the authoritative current plan. If the coordinator engine
    isn't running (group fresh, or backend just restarted and hasn't re-seeded),
    returns an empty plan — an absent engine is not an error condition here.
    """
    group = await crud.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    plan: list[dict[str, Any]] = []
    if group.coordinator_id:
        engine = registry.get_engine(group_id, group.coordinator_id)
        if engine is not None:
            plan = list(engine._dispatch_plan)
    return {
        "ok": True,
        "group_id": group_id,
        "coordinator_id": group.coordinator_id or "",
        "plan": plan,
    }


@router.post("/{group_id}/plan/confirm")
async def plan_confirm(group_id: str) -> dict[str, Any]:
    """Resume the resident plan as-is (user clicked 确认继续).

    Pushes a ``plan_resume`` notify to the coordinator carrying
    ``{"mode": "confirm"}``. The engine dispatches it to the graph as
    ``Command(resume={"mode": "confirm"})`` — ``node_dispatch``'s ``interrupt()``
    returns the payload and the graph fans out the pending steps via
    ``dispatch_next``, skipping the LLM. The plan is *not* mutated.
    """
    group, engine = await _require_coordinator_engine(group_id)
    if not any(s.get("status") == "pending" for s in engine._dispatch_plan):
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
    without waiting, then resumes the resident plan (same notify path as
    confirm). The mode persists in the group config across engine restarts.
    """
    group, engine = await _require_coordinator_engine(group_id)
    # flip the config flag (merge so we don't clobber a co-existing leader_strategy
    # — crud.update_group now merges config keys additively, but build the merged
    # dict here too so the in-memory view stays consistent before the DB write)
    config = dict(group.config or {})
    config["auto_confirm"] = True
    await crud.update_group(group_id, _GroupConfigUpdate(config=config))
    # resume the resident plan if any
    resumed = False
    if any(s.get("status") == "pending" for s in engine._dispatch_plan):
        await route_plan_confirm(group_id, {"mode": "direct"})
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
    steps absent from the body keep their existing values. After patching, the
    amended plan is written back to the engine + re-announced over the bus so
    the front-end PlanConfirmCard reflects the new plan, then a ``plan_confirm``
    notify resumes fan-out.
    """
    group, engine = await _require_coordinator_engine(group_id)
    if not engine._dispatch_plan:
        raise HTTPException(status_code=409, detail="no resident plan to modify")

    plan = [dict(s) for s in engine._dispatch_plan]
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

    # write back to engine + re-announce
    engine._dispatch_plan = plan
    await emit_coordinator_plan(group_id, group.coordinator_id, plan)
    await route_plan_confirm(group_id, {"mode": "modify"})
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
