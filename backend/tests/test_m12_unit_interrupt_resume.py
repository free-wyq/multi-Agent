"""Task 13 (验证A): 单测——auto_confirm=False 图 invoke 到 dispatch 节点 assert
get_state().next 非空，Command(resume={"mode":"confirm"}) 后 assert fan-out 发生.

Focused unit test for the M12 PL-02 interrupt/resume contract. Does NOT hit
network/DB — stubs the coordinator LLM stream + reply/emit/dispatch so the
real coordinator StateGraph (compiled with a MemorySaver checkpointer) is
driven end-to-end through interrupt() and Command(resume=).

Assertions (the task's two contracts):
  [A] auto_confirm=False: a fresh-input invoke that the LLM resolves to
      ``action == "dispatch"`` drives the graph to ``node_dispatch``, which
      calls ``interrupt({"plan": plan})`` and SUSPENDS mid-node. After the
      invoke returns, ``get_state(thread_id).next`` is NON-empty — it must be
      ``("dispatch",)`` — proving the thread is paused at the dispatch node
      awaiting human confirmation (the plan is NOT auto-fanned-out).
  [B] Feeding ``Command(resume={"mode": "confirm"})`` to the same thread wakes
      the paused dispatch node (the second ``interrupt()`` call returns the
      resume value immediately), the node returns ``action_taken="dispatch_next"``,
      ``route_after_dispatch`` routes to ``dispatch_next``, and
      ``dispatch_ready_steps`` IS called → fan-out happened. After the resume
      invoke, ``get_state(thread_id).next`` is empty (``()``) — the thread ran
      to completion.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

# tests/ -> backend/ root so `engine` / `langgraph` resolve (mirrors the other
# backend test scripts, which all run from the backend/ cwd).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.types import Command  # noqa: E402

from engine import coordinator as coord_mod  # noqa: E402
from engine.registry import AgentEngine  # noqa: E402


def _make_stream(plan: list[dict]):
    """A fake chat_completion_stream that yields one JSON decision: dispatch + plan."""
    payload = json.dumps({"action": "dispatch", "content": "", "plan": plan})

    async def fake_stream(config, messages):
        yield (payload, "", 10, 0)

    return fake_stream


async def _drive_to_dispatch(eng: AgentEngine, plan: list[dict], stubs) -> dict:
    """Invoke the coordinator graph with a fresh-input demand so the LLM
    (stubbed) decides ``dispatch`` and the graph reaches ``node_dispatch``."""
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
                "group_id": eng.group_id,
                "agent_id": eng.agent_id,
                "agent_name": eng.name,
                "system_prompt": eng.system_prompt,
                "incoming_message": "please do it",
                "incoming_sender": "user",
                "incoming_kind": "coordinator_reply",
                "incoming_data": None,
                "memory": [],
                "dispatch_plan": [],
                "recent_routes": {},
                "auto_confirm": False,
                "leader_strategy": "",
            },
            config={"configurable": {"thread_id": eng.thread_id}},
        )
        coord_mod.set_reply_callback(None)
    return result


async def main() -> int:
    errs: list[str] = []

    agent_def = {
        "id": "agent_coord_unit",
        "name": "UnitCoord",
        "role": "coordinator",
        "system_prompt": "",
    }
    eng = AgentEngine(
        agent_def, "group_unit_interrupt", coordinator_id="agent_coord_unit"
    )
    if eng.graph_kind != "coordinator":
        errs.append(f"expected coordinator graph_kind, got {eng.graph_kind!r}")
        print("\n" + "=" * 60)
        print(f"FAIL — {len(errs)} 项：")
        for e in errs:
            print(f"  - {e}")
        return 1

    # stub dispatch_ready_steps to record fan-out calls (so we can assert the
    # resume actually fanned out without needing real worker engines)
    fanout_calls: list[list[dict]] = []

    async def fake_dispatch_ready_steps(group_id, coordinator_id, plan):
        out = []
        for s in plan:
            if s.get("status") == "pending":
                s["status"] = "dispatched"
                out.append(s)
        fanout_calls.append([dict(s) for s in out])
        return out

    fake_reply = AsyncMock()
    fake_emit_plan = AsyncMock()
    fake_emit_reasoning = AsyncMock()
    fake_emit_think = AsyncMock()
    stubs = (
        fake_reply,
        fake_emit_plan,
        fake_emit_reasoning,
        fake_emit_think,
        fake_dispatch_ready_steps,
    )

    plan_a = [
        {
            "step": 1,
            "agent_id": "w1",
            "agent_name": "W1",
            "task_id": "t1",
            "status": "pending",
            "instruction": "do A",
        }
    ]
    result = await _drive_to_dispatch(eng, plan_a, stubs)
    # keep the engine mirror consistent with the graph result (the engine's
    # _handle_notify would do this sync-back; we're driving the graph directly
    # here, so replicate it for any downstream mirror readers).
    if isinstance(result, dict) and result.get("dispatch_plan") is not None:
        eng._dispatch_plan = list(result["dispatch_plan"])
    fanout_calls.clear()

    cfg = {"configurable": {"thread_id": eng.thread_id}}

    # ---- [A] the thread is paused at the dispatch node (next NON-empty) ----
    snap = await eng.graph.aget_state(cfg)
    if not snap.next:
        errs.append(
            f"[A] get_state().next is EMPTY — expected the thread paused at the "
            f"dispatch node (next=('dispatch',)) after an auto_confirm=False "
            f"invoke; got next={snap.next!r}. The plan was auto-fanned-out or "
            f"the graph did not suspend at interrupt()."
        )
    elif snap.next != ("dispatch",):
        errs.append(
            f"[A] get_state().next={snap.next!r} — expected exactly ('dispatch',) "
            f"(paused at node_dispatch's interrupt())."
        )
    else:
        # the plan must be checkpointed (source of truth) and NOT yet dispatched
        cp_plan = (snap.values or {}).get("dispatch_plan") or []
        pending = [s for s in cp_plan if s.get("status") == "pending"]
        if not cp_plan:
            errs.append("[A] dispatch_plan missing from checkpointed state at interrupt")
        elif not pending:
            errs.append(f"[A] expected pending step(s) in checkpointed plan, got {cp_plan}")
        elif fanout_calls:
            errs.append("[A] dispatch_ready_steps was called on the interrupt turn (plan fanned out before confirm — interrupt did not suspend)")
        else:
            print(
                f"[A] OK  auto_confirm=False invoke → get_state().next={snap.next} "
                f"(NON-empty, paused at dispatch node); plan checkpointed with "
                f"{len(pending)} pending step, NO fan-out yet (interrupt() suspended mid-node)"
            )

    # ---- [B] Command(resume={"mode":"confirm"}) wakes it → fan-out happens ----
    with patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch_ready_steps):
        resume_result = await eng.graph.ainvoke(
            Command(resume={"mode": "confirm"}),
            config=cfg,
        )

    if not fanout_calls:
        errs.append(
            "[B] dispatch_ready_steps was NOT called after Command(resume={'mode':'confirm'}) "
            "— the resume did not fan out the pending plan."
        )
    else:
        dispatched = fanout_calls[0]
        instr = dispatched[0].get("instruction") if dispatched else None
        if instr != "do A":
            errs.append(f"[B] fan-out dispatched the wrong step: {dispatched} (expected instruction='do A')")
        else:
            print(
                f"[B] OK  Command(resume={{'mode':'confirm'}}) → dispatch_ready_steps called "
                f"→ fan-out dispatched plan A (instruction={instr!r}); resume returned "
                f"action_taken={resume_result.get('action_taken')!r}"
                if isinstance(resume_result, dict)
                else f"[B] OK  fan-out dispatched plan A (instruction={instr!r})"
            )

    snap2 = await eng.graph.aget_state(cfg)
    if snap2.next:
        errs.append(
            f"[B] after resume get_state().next={snap2.next!r} — expected () (thread "
            f"ran to completion after fan-out)."
        )
    else:
        print(f"[B] OK  after resume get_state().next=() (thread completed, fan-out finished)")

    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(
        "PASS — auto_confirm=False interrupt + Command(resume) fan-out 单测通过：\n"
        "  · [A] fresh-input invoke 到 dispatch 节点，interrupt() 暂停线程 →\n"
        "        get_state().next=('dispatch',) 非空，plan 已 checkpointed 且未 fan-out；\n"
        "  · [B] Command(resume={'mode':'confirm'}) 唤醒 dispatch 节点 →\n"
        "        dispatch_ready_steps 被调（fan-out 发生），派发 plan A；\n"
        "        resume 后 get_state().next=()（线程跑完）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
