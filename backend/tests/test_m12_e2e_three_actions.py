"""Task 15 (验证A): 端到端——群发需求→计划卡→确认/直接干/修改三动作各验 fan-out，
改后 GET /plan 重拉仍准.

End-to-end test of the M12 plan-confirmation loop exercising the REAL plan API
endpoints (api/plan.py plan_confirm / plan_direct / plan_modify / plan_get) +
the REAL coordinator StateGraph (interrupt/resume), with only the network/DB
seams stubbed:

- the coordinator LLM stream is stubbed (a canned ``dispatch`` + plan decision),
  so no network;
- ``crud.get_group`` / ``crud.update_group`` / ``crud.list_group_members_with_agent``
  / ``crud.list_agents`` are stubbed with an in-memory fake group so no DB;
- ``registry.get_engine`` is stubbed to return a real ``AgentEngine`` built in
  the test (so the endpoints resolve the coordinator the way production does);
- ``dispatch_ready_steps`` is stubbed to record fan-out calls (so we can assert
  fan-out happened without spawning real worker engines);
- the engine run-loop is bypassed: after each endpoint pushes a ``plan_resume``
  notify onto the inbox, the test drains it and calls ``engine._handle_notify``
  directly (mirroring what the run-loop would do) to drive the resume.

Three independent scenarios (each on its own engine + thread), one per action:

  [A] 确认继续 (confirm):  demand → plan A pending → /plan/confirm → fan-out A.
  [B] 直接干 (direct):    demand → plan A pending → /plan/direct → auto_confirm
      flipped + fan-out A; GET /plan afterwards reads the (now-dispatched) plan.
  [C] 修改 (modify):      demand → plan A pending → /plan/modify (amend step
      instruction → REVISED) → fan-out REVISED; GET /plan afterwards reads the
      amended (REVISED) plan — proving the splice landed in the checkpointer.

After EACH action, GET /api/groups/{id}/plan is called via the real endpoint
and asserted to return the authoritative current plan (the checkpointer truth,
not a stale mirror) — the task's "改后 GET /plan 重拉仍准" contract.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import plan as plan_api  # noqa: E402
from api.plan import PlanModifyBody, PlanModifyStep  # noqa: E402
from engine import coordinator as coord_mod  # noqa: E402
from engine import mention  # noqa: E402
from engine.registry import AgentEngine  # noqa: E402


def _make_stream(plan: list[dict]):
    payload = json.dumps({"action": "dispatch", "content": "", "plan": plan})

    async def fake_stream(config, messages):
        yield (payload, "", 10, 0)

    return fake_stream


class _FakeGroup:
    """In-memory stand-in for the group model the API + engine read.

    ``config`` is a mutable dict so /direct's ``auto_confirm=True`` flip is
    observable on the same object the engine's _handle_notify re-reads.
    """

    def __init__(self, group_id: str, coordinator_id: str) -> None:
        self.id = group_id
        self.name = "E2ETeam"
        self.coordinator_id = coordinator_id
        self.description = ""
        self.status = "active"
        self.config: dict[str, Any] = {"auto_confirm": False}
        self.created_at = ""
        self.updated_at = ""

    def model_dump(self, **kw):
        return {
            "id": self.id,
            "name": self.name,
            "coordinator_id": self.coordinator_id,
            "description": self.description,
            "status": self.status,
            "config": dict(self.config),
        }


async def _drive_demand_to_interrupt(eng: AgentEngine, plan: list[dict], stubs) -> dict:
    """Push a fresh-input coordinator_reply demand that the (stubbed) LLM
    resolves to ``dispatch``, driving the graph to a node_dispatch interrupt."""
    fake_reply, fake_emit_plan, fake_emit_reasoning, fake_emit_think, fake_dispatch = stubs
    fake_stream = _make_stream(plan)
    with patch.object(coord_mod, "chat_completion_stream", fake_stream), \
         patch.object(coord_mod, "_unified_reply", fake_reply), \
         patch.object(coord_mod, "emit_coordinator_plan", fake_emit_plan), \
         patch.object(coord_mod, "emit_coordinator_reasoning", fake_emit_reasoning), \
         patch.object(coord_mod, "emit_coordinator_think", fake_emit_think), \
         patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch):
        coord_mod.set_reply_callback(lambda _c: asyncio.sleep(0))
        result = await eng.graph.ainvoke(
            {
                "group_id": eng.group_id, "agent_id": eng.agent_id, "agent_name": eng.name,
                "system_prompt": eng.system_prompt,
                "incoming_message": "请制定协作计划", "incoming_sender": "user",
                "incoming_kind": "coordinator_reply", "incoming_data": None,
                "memory": eng._memory, "dispatch_plan": eng._dispatch_plan,
                "recent_routes": {}, "auto_confirm": False, "leader_strategy": "",
            },
            config={"configurable": {"thread_id": eng.thread_id}},
        )
        coord_mod.set_reply_callback(None)
    return result


def _build_engine(group_id: str, coord_id: str) -> AgentEngine:
    agent_def = {
        "id": coord_id, "name": "E2ECoord", "role": "coordinator", "system_prompt": "",
    }
    return AgentEngine(agent_def, group_id, coordinator_id=coord_id, single_chat=False)


def _ctx(eng: AgentEngine, fake_group: _FakeGroup, fake_dispatch):
    """Patch the API/engine seams to route the endpoints to the test engine.

    ``crud.update_group`` is stubbed with a mutating async fake that replicates
    the production additive-merge side effect (``merged.update(payload.config)``)
    onto ``fake_group.config`` — so /direct's ``auto_confirm=True`` flip is
    observable on the same object the engine's ``_handle_notify`` re-reads.
    """

    async def fake_update_group(group_id, payload):
        data = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "config" in data:
            merged = dict(fake_group.config or {})
            merged.update(data["config"] or {})
            fake_group.config = merged
        return fake_group

    return (
        patch.object(mention.crud, "get_group", AsyncMock(return_value=fake_group)),
        patch.object(plan_api.crud, "get_group", AsyncMock(return_value=fake_group)),
        patch.object(plan_api.crud, "update_group", fake_update_group),
        patch.object(
            plan_api.registry, "get_engine",
            lambda gid, aid: eng if (gid, aid) == (eng.group_id, eng.agent_id) else None,
        ),
        patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch),
        patch.object(coord_mod, "_unified_reply", AsyncMock()),
        patch.object(coord_mod, "emit_coordinator_plan", AsyncMock()),
        patch.object(coord_mod, "emit_coordinator_reasoning", AsyncMock()),
        patch.object(coord_mod, "emit_coordinator_think", AsyncMock()),
    )


async def _drain_and_handle_resume(eng: AgentEngine, fake_dispatch) -> None:
    """The endpoint pushed a plan_resume notify onto the coordinator's inbox.
    The engine run-loop would drain + _handle_notify it; we bypass the loop and
    drive the resume directly, with dispatch_ready_steps re-patched so fan-out
    is recorded."""
    from engine.inbox import get_inbox
    pushed = await get_inbox(eng.group_id, eng.agent_id).get()
    notify = pushed["item"]
    assert notify.get("type") == "plan_resume", notify
    with patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch):
        await eng._handle_notify(notify)


async def _get_plan(group_id: str) -> dict:
    """Call the real GET /api/groups/{id}/plan endpoint."""
    return await plan_api.plan_get(group_id)


# ───────────────────────── scenario A: 确认继续 ─────────────────────────


async def scenario_confirm(errs: list[str]) -> None:
    print("\n--- 场景 A：确认继续 ---")
    gid, cid = "group_e2e_confirm", "agent_e2e_confirm"
    eng = _build_engine(gid, cid)
    fake_group = _FakeGroup(gid, cid)

    fanout: list[list[dict]] = []
    async def fake_dispatch(group_id, coordinator_id, plan):
        out = []
        for s in plan:
            if s.get("status") == "pending":
                s["status"] = "dispatched"
                out.append(s)
        fanout.append([dict(s) for s in out])
        return out

    stubs = (AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), fake_dispatch)
    plan_a = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "task_id": "t1",
               "status": "pending", "instruction": "do A"}]
    res = await _drive_demand_to_interrupt(eng, plan_a, stubs)
    if isinstance(res, dict) and res.get("dispatch_plan") is not None:
        eng._dispatch_plan = list(res["dispatch_plan"])
    fanout.clear()

    # GET /plan mid-interrupt → plan A pending (checkpointer truth, not stale)
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        mid = await _get_plan(gid)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    mid_plan = mid.get("plan") or []
    if not mid_plan or mid_plan[0].get("instruction") != "do A" or mid_plan[0].get("status") != "pending":
        errs.append(f"[A mid] GET /plan mid-interrupt should return plan A pending, got {mid_plan}")
    else:
        print(f"[A mid] GET /plan OK: plan A pending (instruction={mid_plan[0].get('instruction')!r})")

    # /plan/confirm → fan-out A
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        resp = await plan_api.plan_confirm(gid)
        await _drain_and_handle_resume(eng, fake_dispatch)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    if not resp.get("ok") or resp.get("mode") != "confirm":
        errs.append(f"[A confirm] response unexpected: {resp}")
    elif not fanout or fanout[0][0].get("instruction") != "do A":
        errs.append(f"[A confirm] fan-out wrong: {fanout}")
    else:
        print(f"[A confirm] /plan/confirm → fan-out A (instruction={fanout[0][0].get('instruction')!r})")

    # GET /plan after confirm → plan A now dispatched (checkpointer truth)
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        after = await _get_plan(gid)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    after_plan = after.get("plan") or []
    if not after_plan or after_plan[0].get("instruction") != "do A" or after_plan[0].get("status") != "dispatched":
        errs.append(f"[A after] GET /plan after confirm should show plan A dispatched, got {after_plan}")
    else:
        print(f"[A after] GET /plan OK: plan A dispatched (status={after_plan[0].get('status')!r}) — 重拉仍准")


# ───────────────────────── scenario B: 直接干 ─────────────────────────


async def scenario_direct(errs: list[str]) -> None:
    print("\n--- 场景 B：直接干 ---")
    gid, cid = "group_e2e_direct", "agent_e2e_direct"
    eng = _build_engine(gid, cid)
    fake_group = _FakeGroup(gid, cid)

    fanout: list[list[dict]] = []
    async def fake_dispatch(group_id, coordinator_id, plan):
        out = []
        for s in plan:
            if s.get("status") == "pending":
                s["status"] = "dispatched"
                out.append(s)
        fanout.append([dict(s) for s in out])
        return out

    stubs = (AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), fake_dispatch)
    plan_a = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "task_id": "t1",
               "status": "pending", "instruction": "build API"}]
    res = await _drive_demand_to_interrupt(eng, plan_a, stubs)
    if isinstance(res, dict) and res.get("dispatch_plan") is not None:
        eng._dispatch_plan = list(res["dispatch_plan"])
    fanout.clear()

    # /plan/direct → auto_confirm flipped + fan-out A
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        resp = await plan_api.plan_direct(gid)
        await _drain_and_handle_resume(eng, fake_dispatch)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    if not resp.get("ok") or resp.get("auto_confirm") is not True or resp.get("resumed_resident_plan") is not True:
        errs.append(f"[B direct] response unexpected: {resp}")
    elif fake_group.config.get("auto_confirm") is not True:
        errs.append(f"[B direct] group.config.auto_confirm not flipped: {fake_group.config}")
    elif not fanout or fanout[0][0].get("instruction") != "build API":
        errs.append(f"[B direct] fan-out wrong: {fanout}")
    else:
        print(f"[B direct] /plan/direct → auto_confirm=True + fan-out A (instruction={fanout[0][0].get('instruction')!r})")

    # GET /plan after direct → plan A dispatched (auto_confirm flipped in config too)
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        after = await _get_plan(gid)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    after_plan = after.get("plan") or []
    if not after_plan or after_plan[0].get("status") != "dispatched":
        errs.append(f"[B after] GET /plan after direct should show plan A dispatched, got {after_plan}")
    else:
        print(f"[B after] GET /plan OK: plan A dispatched (status={after_plan[0].get('status')!r}) — 重拉仍准")


# ───────────────────────── scenario C: 修改 ─────────────────────────


async def scenario_modify(errs: list[str]) -> None:
    print("\n--- 场景 C：修改 ---")
    gid, cid = "group_e2e_modify", "agent_e2e_modify"
    eng = _build_engine(gid, cid)
    fake_group = _FakeGroup(gid, cid)

    fanout: list[list[dict]] = []
    async def fake_dispatch(group_id, coordinator_id, plan):
        out = []
        for s in plan:
            if s.get("status") == "pending":
                s["status"] = "dispatched"
                out.append(s)
        fanout.append([dict(s) for s in out])
        return out

    stubs = (AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), fake_dispatch)
    plan_a = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "task_id": "t1",
               "status": "pending", "instruction": "do A"}]
    res = await _drive_demand_to_interrupt(eng, plan_a, stubs)
    if isinstance(res, dict) and res.get("dispatch_plan") is not None:
        eng._dispatch_plan = list(res["dispatch_plan"])
    fanout.clear()

    # /plan/modify (amend step 1 instruction → REVISED) → fan-out REVISED
    body = PlanModifyBody(steps=[PlanModifyStep(step=1, instruction="do A REVISED")])
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        resp = await plan_api.plan_modify(gid, body)
        await _drain_and_handle_resume(eng, fake_dispatch)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    resp_plan = resp.get("plan") or []
    if not resp_plan or resp_plan[0].get("instruction") != "do A REVISED":
        errs.append(f"[C modify] response plan should be REVISED, got {resp_plan}")
    elif not fanout or fanout[0][0].get("instruction") != "do A REVISED":
        errs.append(f"[C modify] fan-out should dispatch REVISED, got {fanout}")
    else:
        print(f"[C modify] /plan/modify → fan-out REVISED (instruction={fanout[0][0].get('instruction')!r})")

    # GET /plan after modify → plan REVISED + dispatched (splice landed in checkpointer)
    ctx = _ctx(eng, fake_group, fake_dispatch)
    for c in ctx: c.__enter__()
    try:
        after = await _get_plan(gid)
    finally:
        for c in reversed(ctx): c.__exit__(None, None, None)
    after_plan = after.get("plan") or []
    if not after_plan or after_plan[0].get("instruction") != "do A REVISED":
        errs.append(f"[C after] GET /plan after modify should show REVISED plan, got {after_plan}")
    elif after_plan[0].get("status") != "dispatched":
        errs.append(f"[C after] GET /plan after modify should show REVISED + dispatched, got {after_plan}")
    else:
        print(f"[C after] GET /plan OK: plan REVISED dispatched (instruction={after_plan[0].get('instruction')!r}) — 重拉仍准")


async def main() -> int:
    errs: list[str] = []
    await scenario_confirm(errs)
    await scenario_direct(errs)
    await scenario_modify(errs)

    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 端到端三动作各验 fan-out + GET /plan 重拉仍准：")
    print("  · [A 确认继续] /plan/confirm → fan-out plan A；改后 GET /plan 读 plan A dispatched；")
    print("  · [B 直接干]   /plan/direct → auto_confirm 翻转 + fan-out plan A；改后 GET /plan 读 plan A dispatched；")
    print("  · [C 修改]     /plan/modify(amend) → fan-out REVISED；改后 GET /plan 读 plan REVISED + dispatched（splice 落 checkpointer）。")
    print("  · 三场景均经真实 plan API 端点 + 真实 coordinator StateGraph interrupt/resume，")
    print("    仅 LLM/DB/dispatch_ready_steps stub；GET /plan 各阶段读 checkpointer 真源（非 stale mirror）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
