"""Task 14 (验证A): 边界测——interrupted 态发新需求（非 confirm）assert 不被吞、走 llm_decide.

M12 PL-02 residual-interrupt boundary contract: while a plan awaits
confirmation (the thread is paused mid-``node_dispatch`` via ``interrupt()``),
a NEW user demand arriving as a fresh-input invoke (NOT a ``Command(resume=)``)
must NOT be swallowed — it routes through ``classify → llm_decide`` so the
coordinator LLM sees the new message and can answer it. LangGraph 1.2.5
auto-resolves the dangling interrupt as a side effect of routing the fresh
input through the graph from START.

This test drives the real coordinator StateGraph (MemorySaver checkpointer):
  [1] turn-1 fresh demand → LLM decides dispatch → node_dispatch interrupt()
      → thread paused at dispatch (next=('dispatch',)), plan A pending;
  [2] turn-2 a NEW non-confirm demand (incoming_kind != "plan_confirm", e.g.
      a side question "顺便问下天气") → assert:
      (a) the invoke completes WITHOUT the new demand being swallowed into the
          pending plan — i.e. dispatch_ready_steps is NOT called (the waiting
          plan A is not auto-fanned-out by the new demand);
      (b) the LLM-decide path ran: the coordinator LLM stub was invoked a
          second time (turn-2) and its chat reply was emitted — the new demand
          reached llm_decide → chat, NOT silently dropped;
      (c) the resident pending plan A is still intact in the checkpointer
          (the new chat turn did not clobber it — node_llm_decide omits the
          dispatch_plan key on non-dispatch actions so replace_value no-ops);
      (d) the new demand was NOT turned into a confirm-resume (next after
          turn-2 is () because chat → END, and the dangling interrupt was
          resolved implicitly by the fresh-input routing).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import coordinator as coord_mod  # noqa: E402
from engine.registry import AgentEngine  # noqa: E402


def _make_stream(payload_str: str):
    """A fake chat_completion_stream yielding one JSON decision chunk."""
    async def fake_stream(config, messages):
        yield (payload_str, "", 10, 0)
    return fake_stream


async def _invoke_fresh(eng: AgentEngine, payload_str: str, message: str, stubs) -> dict:
    """Fresh-input ainvoke with the LLM stubbed to a given JSON decision.

    Faithfully replicates ``AgentEngine._handle_notify``'s fresh-input payload:
    it passes the engine's *resident* ``_dispatch_plan`` mirror as the input
    ``dispatch_plan`` (NOT a bare ``[]``). This matters on a turn-2 invoke while
    the thread is interrupted at ``node_dispatch``: the input is written to
    state via the ``replace_value`` reducer at START, so passing ``[]`` would
    clobber the waiting plan A, while passing the mirror (plan A, synced back
    after turn-1's interrupt) no-ops the reducer and preserves the resident
    plan — which is the production behaviour the test must assert against.
    """
    fake_reply, fake_emit_plan, fake_emit_reasoning, fake_emit_think, fake_dispatch = stubs
    fake_stream = _make_stream(payload_str)
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
                "incoming_message": message,
                "incoming_sender": "user",
                "incoming_kind": "coordinator_reply",
                "incoming_data": None,
                "memory": eng._memory,
                "dispatch_plan": eng._dispatch_plan,
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
        "id": "agent_coord_boundary",
        "name": "BoundaryCoord",
        "role": "coordinator",
        "system_prompt": "",
    }
    eng = AgentEngine(
        agent_def, "group_boundary_interrupt", coordinator_id="agent_coord_boundary", single_chat=False
    )
    assert eng.graph_kind == "coordinator"

    fanout_calls: list[list[dict]] = []
    async def fake_dispatch_ready_steps(group_id, coordinator_id, plan):
        out = []
        for s in plan:
            if s.get("status") == "pending":
                s["status"] = "dispatched"
                out.append(s)
        fanout_calls.append([dict(s) for s in out])
        return out

    # unified_reply records each call's content so we can assert the chat reply
    # for the new demand was actually produced (llm_decide → chat ran).
    reply_contents: list[str] = []
    async def fake_reply(group_id, agent_id, content, data=None):
        reply_contents.append(content)
        return None

    fake_emit_plan = AsyncMock()
    fake_emit_reasoning = AsyncMock()
    fake_emit_think = AsyncMock()
    stubs = (fake_reply, fake_emit_plan, fake_emit_reasoning, fake_emit_think, fake_dispatch_ready_steps)

    cfg = {"configurable": {"thread_id": eng.thread_id}}

    # ---- turn 1: dispatch decision → interrupt at dispatch node ----
    plan_a = [
        {"step": 1, "agent_id": "w1", "agent_name": "W1", "task_id": "t1",
         "status": "pending", "instruction": "do A"}
    ]
    payload_dispatch = json.dumps({"action": "dispatch", "content": "", "plan": plan_a})
    res1 = await _invoke_fresh(eng, payload_dispatch, "请制定计划做 A", stubs)
    if isinstance(res1, dict) and res1.get("dispatch_plan") is not None:
        eng._dispatch_plan = list(res1["dispatch_plan"])
    fanout_calls.clear()
    reply_contents.clear()

    snap1 = await eng.graph.aget_state(cfg)
    if snap1.next != ("dispatch",):
        errs.append(f"[setup] turn1 expected interrupt at dispatch (next=('dispatch',)), got {snap1.next!r}")
        # still try to continue — but the boundary assertions below may not be meaningful
    else:
        cp_plan = (snap1.values or {}).get("dispatch_plan") or []
        print(f"[setup] turn1 interrupt OK: next={snap1.next}, checkpointed plan pending="
              f"{[s.get('status') for s in cp_plan]}")

    # ---- turn 2: a NEW non-confirm demand (a side question) while interrupted ----
    # The LLM stub decides "chat" with an answer to the side question — proving
    # the new demand reached llm_decide (not swallowed).
    chat_reply = "今天天气不错，适合推进 A。"
    payload_chat = json.dumps({"action": "chat", "content": chat_reply, "plan": []})
    res2 = await _invoke_fresh(eng, payload_chat, "顺便问下今天天气", stubs)

    # (a) the waiting plan A was NOT auto-fanned-out by the new demand
    if fanout_calls:
        errs.append(
            f"[2a] the new non-confirm demand triggered fan-out (dispatch_ready_steps called "
            f"{len(fanout_calls)}x) — the waiting plan was NOT protected from the new demand; "
            f"calls={fanout_calls}"
        )
    else:
        print("[2a] OK  the new non-confirm demand did NOT trigger fan-out (waiting plan protected)")

    # (b) the LLM-decide → chat path ran: the chat reply was emitted via _unified_reply
    if chat_reply not in reply_contents:
        errs.append(
            f"[2b] the new demand's chat reply {chat_reply!r} was NOT emitted via _unified_reply "
            f"(reply_contents={reply_contents}) — the new demand was swallowed (llm_decide did not run)"
        )
    else:
        print(f"[2b] OK  the new demand reached llm_decide → chat (reply emitted: {chat_reply!r})")

    # (c) the resident pending plan A is still intact in the checkpointer
    #     (a chat/ask/continue turn must NOT clobber the pending plan)
    snap2 = await eng.graph.aget_state(cfg)
    cp_plan2 = (snap2.values or {}).get("dispatch_plan") or []
    pending_a = [s for s in cp_plan2 if s.get("instruction") == "do A" and s.get("status") == "pending"]
    if not pending_a:
        errs.append(
            f"[2c] the resident pending plan A was clobbered/lost by the chat turn — "
            f"checkpointed dispatch_plan now = {cp_plan2} (expected plan A still pending)"
        )
    else:
        print(f"[2c] OK  resident pending plan A intact after chat turn (plan={cp_plan2})")

    # (d) the new demand was NOT turned into a confirm-resume: the graph ran to
    #     completion (chat → END); the dangling interrupt was resolved implicitly.
    if snap2.next:
        errs.append(
            f"[2d] after the new demand, next={snap2.next!r} — expected () (chat→END; the dangling "
            f"interrupt should have been resolved implicitly by the fresh-input routing)"
        )
    else:
        print("[2d] OK  after the new demand next=() (chat→END; dangling interrupt resolved, demand not swallowed)")

    # (e) defensive: the turn-2 action_taken is "chat" (llm_decide ran, classify did
    #     not route to confirm_dispatch / dispatch_next for the new demand)
    if isinstance(res2, dict) and res2.get("action_taken") != "chat":
        errs.append(
            f"[2e] turn-2 action_taken={res2.get('action_taken')!r} — expected 'chat' (llm_decide ran). "
            f"Classify may have misrouted the new demand to confirm_dispatch/dispatch_next."
        )
    else:
        print(f"[2e] OK  turn-2 action_taken='chat' (classify → llm_decide → chat, not confirm_dispatch)")

    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(
        "PASS — interrupted 态发新需求（非 confirm）不被吞、走 llm_decide：\n"
        "  · [2a] 新需求未触发 fan-out（等待中的 plan A 被保护，未被新需求提前派发）；\n"
        "  · [2b] 新需求走了 llm_decide → chat（chat 回复已 emit，证明未被吞）；\n"
        "  · [2c] 驻留 pending plan A 在 chat turn 后仍完好（node_llm_decide 非 dispatch 不返 dispatch_plan，replace_value no-op）；\n"
        "  · [2d] 新需求后 next=()（chat→END，dangling interrupt 被隐式 resolve，未被当 confirm-resume）；\n"
        "  · [2e] turn-2 action_taken='chat'（classify→llm_decide→chat，未误路由到 confirm_dispatch/dispatch_next）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
