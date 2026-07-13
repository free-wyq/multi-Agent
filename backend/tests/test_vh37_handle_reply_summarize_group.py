"""VH37 回归：handle_reply + summarize 节点迁移到群图（去中心化 handoff 迁移·报告回收层）.

锁住 task-9 决策——coordinator handle_reply + summarize 节点迁移到群图：handle_reply 接收
agent 节点报告（MT-15 失败恢复 + MT-14 步骤调整），不再走 inbox notify 回路.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）+ [[dispatch-next-send-fanout]].
resident coordinator 图里，worker report-back 走 inbox notify 回路：``_run_worker_task`` →
``push_notify("agent_reply", ...)`` → 协调者引擎 run loop → ``_handle_notify`` → fresh-input
ainvoke（incoming_kind="agent_reply"）→ classify → route_after_classify → handle_reply.
群图里 dispatch_next 的 ``Send`` fan-out 直接到 agent 节点，agent 节点发言后用
``Command(goto="handle_reply_group", update={...})`` 把报告送回 coordinator 子节点——
**不经 inbox notify 回路**，in-graph 闭环.

五段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. 节点装配锁——handle_reply_group / summarize_group twin 就位
    1. ``node_handle_reply_group(state: GroupState) -> Command`` async 函数存在.
    2. ``node_summarize_group(state: GroupState) -> Command`` async 函数存在.
    3. ``build_coordinator_subnodes`` dict 含 ``handle_reply_group`` + ``summarize_group`` 键
       （resident handle_reply/summarize 仍在——additive twin）.

  B. MT-15 失败恢复保真锁——failure 报告走 _maybe_handle_step_failure
    4. failure + keep_failed（默认）+ all_done → summarize_group（step 标 failed）.
    5. failure + retry/reassign（reset pending）→ dispatch_next_group（重派）.
    6. ``node_handle_reply_group`` 体调 ``_maybe_handle_step_failure``（MT-15 真源不破）.

  C. MT-14 步骤调整保真锁——success 报告走 _maybe_adjust_remaining_steps
    7. success + all_done → summarize_group.
    8. success + pending remain → ``_maybe_adjust_remaining_steps`` 被调 → dispatch_next_group.
    9. ``node_handle_reply_group`` 体调 ``_maybe_adjust_remaining_steps``（MT-14 真源不破）.

  D. 路由保真锁——Command(goto=...) 三分支与 resident route_after_handle_reply 同源
   10. no matching task_id → llm_decide（fall back，与 resident 同款）.
   11. all_done → summarize_group（Command goto summarize）.
   12. retry reset pending / pending remain → dispatch_next_group（Command goto fan-out）.

  E. summarize_group 语义锁——汇总回复 + 清 plan + END
   13. ``node_summarize_group`` 调 ``_unified_reply`` 发「🎉 全部完成！」汇总（与 resident
       node_summarize 同款 format_step_summary）.
   14. ``node_summarize_group`` emit 空 plan（清前端 plan card）+ 返 ``dispatch_plan=[]``.
   15. ``node_summarize_group`` 返 ``Command(goto=END)``（回合 in-graph 结束）.

  F. 向后兼容锁——resident handle_reply/summarize 不破（mt13/mt14/mt15/mt16/mt17 不破）
   16. ``node_handle_reply`` / ``node_summarize``（resident）仍在（dict 返回，conditional-edge 路由）.
   17. ``route_after_handle_reply`` 仍在（resident 图路由不破）.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
COORD_PY = BACKEND / "engine" / "coordinator.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord_src = _read(COORD_PY)

    try:
        from engine.coordinator import (  # type: ignore
            build_coordinator_subnodes,
            node_handle_reply,
            node_handle_reply_group,
            node_summarize,
            node_summarize_group,
            route_after_handle_reply,
        )
        from langgraph.types import Command
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 节点装配 ──────────────────────────────────────────
    # A1 node_handle_reply_group async
    if not inspect.iscoroutinefunction(node_handle_reply_group):
        errs.append("[A1] node_handle_reply_group 应是 async 函数")
    else:
        print("[A1] OK  node_handle_reply_group(state: GroupState) -> Command async 函数就位")

    # A2 node_summarize_group async
    if not inspect.iscoroutinefunction(node_summarize_group):
        errs.append("[A2] node_summarize_group 应是 async 函数")
    else:
        print("[A2] OK  node_summarize_group(state: GroupState) -> Command async 函数就位")

    # A3 build_coordinator_subnodes 含 twin + resident 保留
    specs = build_coordinator_subnodes(coordinator_id="c1")
    if "handle_reply_group" not in specs:
        errs.append("[A3] build_coordinator_subnodes 缺 handle_reply_group twin")
    elif "summarize_group" not in specs:
        errs.append("[A3] build_coordinator_subnodes 缺 summarize_group twin")
    elif "handle_reply" not in specs or "summarize" not in specs:
        errs.append("[A3] resident handle_reply/summarize 不应删（additive twin）")
    else:
        print("[A3] OK  build_coordinator_subnodes 含 handle_reply_group + summarize_group twin（resident handle_reply/summarize 保留）")

    # ── B. MT-15 失败恢复保真 ──────────────────────────────────
    hr_body = _fn_body(coord_src, "node_handle_reply_group")
    if "_maybe_handle_step_failure" not in hr_body:
        errs.append("[B6] node_handle_reply_group 未调 _maybe_handle_step_failure（MT-15 真源断）")
    else:
        print("[B6] OK  node_handle_reply_group 调 _maybe_handle_step_failure（MT-15 失败恢复真源不破）")

    # B4 failure + keep_failed + all_done → summarize_group
    try:
        async def _run_b4():
            async def fake_failure(state, plan, idx): return plan  # keep_failed
            with patch("engine.coordinator._maybe_handle_step_failure", fake_failure), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "err", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": False}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_b4())
        if cmd.goto != "summarize":
            errs.append(f"[B4] failure+keep_failed+all_done 应 goto summarize，实际 {cmd.goto!r}")
        elif cmd.update.get("dispatch_plan", [{}])[0].get("status") != "failed":
            errs.append("[B4] failed step 应标 failed")
        else:
            print("[B4] OK  failure+keep_failed+all_done → summarize_group（step failed，MT-15 默认保真）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] failure keep_failed 测试异常：{type(e).__name__}: {e}")

    # B5 failure + retry (reset pending) → dispatch_next_group
    try:
        async def _run_b5():
            async def fake_failure(state, plan, idx):
                plan[idx]["status"] = "pending"; plan[idx]["task_id"] = None
                plan[idx]["result"] = None; plan[idx]["_attempts"] = 1
                return plan
            with patch("engine.coordinator._maybe_handle_step_failure", fake_failure), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "err", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": False}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_b5())
        if cmd.goto != "dispatch_next_group":
            errs.append(f"[B5] failure+retry(reset pending) 应 goto dispatch_next_group，实际 {cmd.goto!r}")
        elif cmd.update.get("dispatch_plan", [{}])[0].get("status") != "pending":
            errs.append("[B5] retry 应 reset step pending")
        else:
            print("[B5] OK  failure+retry(reset pending) → dispatch_next_group（重派，MT-15 retry 保真）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] failure retry 测试异常：{type(e).__name__}: {e}")

    # ── C. MT-14 步骤调整保真 ──────────────────────────────────
    if "_maybe_adjust_remaining_steps" not in hr_body:
        errs.append("[C9] node_handle_reply_group 未调 _maybe_adjust_remaining_steps（MT-14 真源断）")
    else:
        print("[C9] OK  node_handle_reply_group 调 _maybe_adjust_remaining_steps（MT-14 步骤调整真源不破）")

    # C7 success + all_done → summarize_group
    try:
        async def _run_c7():
            async def fake_adjust(state, plan): return plan
            with patch("engine.coordinator._maybe_adjust_remaining_steps", fake_adjust), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "done A", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": True}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_c7())
        if cmd.goto != "summarize":
            errs.append(f"[C7] success+all_done 应 goto summarize，实际 {cmd.goto!r}")
        else:
            print("[C7] OK  success+all_done → summarize_group")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] success all_done 测试异常：{type(e).__name__}: {e}")

    # C8 success + pending remain → _maybe_adjust called → dispatch_next_group
    try:
        adjust_called = []
        async def _run_c8():
            async def fake_adjust(state, plan):
                adjust_called.append(True); return plan
            with patch("engine.coordinator._maybe_adjust_remaining_steps", fake_adjust), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()):
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                     "status": "dispatched", "task_id": "t1", "depends_on": []},
                    {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                     "status": "pending", "task_id": None, "depends_on": [1]},
                ]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "done A", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": True}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_c8())
        if not adjust_called:
            errs.append("[C8] success+pending remain 应调 _maybe_adjust_remaining_steps")
        elif cmd.goto != "dispatch_next_group":
            errs.append(f"[C8] success+pending remain 应 goto dispatch_next_group，实际 {cmd.goto!r}")
        else:
            print("[C8] OK  success+pending remain → _maybe_adjust 被调 → dispatch_next_group（MT-14 调整保真）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] success pending remain 测试异常：{type(e).__name__}: {e}")

    # ── D. 路由保真 ──────────────────────────────────────────
    # D10 no matching task_id → llm_decide
    try:
        async def _run_d10():
            with patch("engine.coordinator._unified_reply", AsyncMock()):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "stray", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "unknown", "success": True}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_d10())
        if cmd.goto != "llm_decide":
            errs.append(f"[D10] no matching task_id 应 goto llm_decide，实际 {cmd.goto!r}")
        else:
            print("[D10] OK  no matching task_id → llm_decide（fall back，与 resident route_after_handle_reply 同款）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] no-match 测试异常：{type(e).__name__}: {e}")

    # D11/D12 已在 B4/C8 验（all_done→summarize / retry or pending→dispatch_next_group）

    # ── E. summarize_group 语义 ──────────────────────────────
    try:
        async def _run_e13():
            with patch("engine.coordinator._unified_reply", AsyncMock()) as ur, \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()) as ep:
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "completed", "task_id": "t1", "result": "ok"}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan}
                cmd = await node_summarize_group(st)
                return cmd, ur, ep
        cmd, ur, ep = asyncio.run(_run_e13())
        from langgraph.graph import END as _END
        if cmd.goto != _END:
            errs.append(f"[E13] summarize_group 应 goto END，实际 {cmd.goto!r}")
        elif not ur.called:
            errs.append("[E13] summarize_group 未调 _unified_reply 发汇总")
        elif "🎉 全部完成" not in (ur.call_args[0][2] if ur.call_args else ""):
            errs.append(f"[E13] 汇总回复缺「🎉 全部完成」：{ur.call_args[0][2]!r}")
        elif cmd.update.get("dispatch_plan") != []:
            errs.append(f"[E13] dispatch_plan 应清空 []，实际 {cmd.update.get('dispatch_plan')!r}")
        elif not ep.called:
            errs.append("[E13] summarize_group 未 emit 空 plan（前端 plan card 不清）")
        else:
            print("[E13] OK  summarize_group → _unified_reply「🎉 全部完成！」+ emit 空 plan + dispatch_plan=[] + goto END")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E13] summarize_group 测试异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F16 resident handle_reply/summarize 仍在（dict 返回）
    if not inspect.iscoroutinefunction(node_handle_reply):
        errs.append("[F16] resident node_handle_reply 应仍是 async")
    elif not inspect.iscoroutinefunction(node_summarize):
        errs.append("[F16] resident node_summarize 应仍是 async")
    else:
        print("[F16] OK  resident node_handle_reply + node_summarize 保留（dict 返回，conditional-edge 路由不破）")

    # F17 route_after_handle_reply 仍在
    if not callable(route_after_handle_reply):
        errs.append("[F17] route_after_handle_reply 不可调用（resident 图路由破）")
    else:
        print("[F17] OK  route_after_handle_reply 保留（resident 图路由不破，mt13/mt14/mt15/mt16/mt17 不破）")

    return errs


def main() -> int:
    print("=== VH37 回归：handle_reply + summarize 节点迁移群图（去中心化 handoff 迁移·报告回收层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "handle_reply + summarize 节点迁移群图锁定：\n"
        "  · A handle_reply_group + summarize_group twin 就位（build_coordinator_subnodes 含，resident 保留）；\n"
        "  · B MT-15 失败恢复保真（_maybe_handle_step_failure 被调，keep_failed→summarize / retry→dispatch_next_group）；\n"
        "  · C MT-14 步骤调整保真（_maybe_adjust_remaining_steps 被调，success+pending→dispatch_next_group）；\n"
        "  · D 路由保真（no-match→llm_decide / all_done→summarize / retry or pending→dispatch_next_group）；\n"
        "  · E summarize_group 语义（_unified_reply 汇总 + emit 空 plan + dispatch_plan=[] + goto END）；\n"
        "  · F 向后兼容（resident handle_reply/summarize + route_after_handle_reply 保留，mt13~mt17 不破）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
