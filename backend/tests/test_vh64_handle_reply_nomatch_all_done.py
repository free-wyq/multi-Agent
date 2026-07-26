"""VH64 回归：缺陷4 修复——report-back 命中且 all_done 时直达 summarize_group.

锁住 ``node_handle_reply_group`` 的 ``matched_idx is None`` 分支缺陷4 修复（option 1）：

  背景（见 memory ``coordinator-duplicate-stream-defect-2026-07-26`` + repro）：
  原 ``matched_idx is None`` 无条件 ``goto="llm_decide"``。当一份 report-back 的
  ``task_id`` 没匹配到任何 ``status=="dispatched"`` 的步骤（典型：单步计划已收尾、
  重复/迟到的 report-back、task_id mismatch）时，``llm_decide`` 跑一整段协调者 LLM
  流式决策，其 ``content`` 经 ``node_chat`` 落成一条 NEW ``agent_reply`` 气泡，
  叠在 worker 自己的 execute announce 之上——即「同段决策文本二次推送」缺陷4。

  修复（option 1，deterministic + 流式安全）：``matched_idx is None`` 时先判 plan 是否
  all_done（全部 completed/failed）。是 → 直达 ``summarize_group``（汇总模板文本，非流式
  LLM 决策，不产二次气泡）；否（plan 真在途，stray report）→ 保留原 ``llm_decide`` fallback
  （Leader LLM 处理意外 report，rare on happy path）。

  为何 option 1 而非 option 2（llm_decide 流式对 report-back 上下文去重）：
  option 2 仍会跑一整段 LLM（成本 + 延迟 + 仍可能产 chat 气泡），且去重逻辑跨
  LLM prompt/parse 脆弱。option 1 在 plan 已收尾时跳过 LLM 整段——既消除二次气泡，
  又 honor all-done 契约（无事可决）。stray report mid-flight 仍走 llm_decide 兜底，
  不破 resident route_after_handle_reply 的 fall-back 语义（vh37 D10 锁的「no-match→
  llm_decide」对 *未全部完成* 的 plan 仍成立——本测试 D-new 只覆盖 all-done 新分支）。

六段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. 静态源码锁——matched_idx None 分支含 all_done 判定
    1. ``node_handle_reply_group`` body 含 ``matched_idx is None`` 检查.
    2. 该分支含 all_done 判定（``all(s.get("status") in ("completed", "failed") ...)``）.
    3. all_done 命中分支 ``goto="summarize_group"``（非 ``llm_decide``）.
    4. 未 all_done 仍 ``goto="llm_decide"``（保留 stray-report fallback）.

  B. 行为锁——no-match + all_done → summarize_group（缺陷4 修复核心）
    5. 构造 all_done plan（step1 completed、step2 failed），incoming_data.task_id="unknown"
       （miss 任意 step）→ ``Command(goto="summarize_group", action_taken="summarize")``.

  C. 行为锁——no-match + plan 在途 → llm_decide（fallback 不破，vh37 D10 同款）
    6. 构造未 all_done plan（step1 dispatched 在途），incoming_data.task_id="unknown"
       → ``Command(goto="llm_decide")``.

  D. 行为锁——matched（命中）路径不受影响（all_done 命中走原 summarize_group）
    7. step1 dispatched + task_id 命中 + success → ``Command(goto="summarize_group")``
       （原 B7/C7 vh37 已锁，此处仅回归确认新分支未碰命中路径）.

  E. 汇总文本锁——summarize_group 走 _unified_reply 模板（非 llm_decide 流式）
    8. ``node_summarize_group`` body 含 ``_unified_reply`` + ``format_step_summary``
       （模板文本，无 LLM 流式 → 不产二次气泡）.

  F. 不回归 resident——node_handle_reply（dict 版）不动
    9. resident ``node_handle_reply`` 仍返 dict（``action_taken``，非 Command goto）.
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
            node_handle_reply,
            node_handle_reply_group,
            node_summarize_group,
        )
        from langgraph.types import Command  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 静态源码锁 ──────────────────────────────────────────
    hr_body = _fn_body(coord_src, "node_handle_reply_group")

    # A1 matched_idx is None 检查存在
    if "matched_idx is None" not in hr_body:
        errs.append("[A1] node_handle_reply_group 缺 ``matched_idx is None`` 检查")
    else:
        print("[A1] OK  matched_idx is None 检查在位")

    # 截取 matched_idx is None 分支体（到下一个 ``plan[matched_idx]`` 行）
    nomatch_idx = hr_body.find("if matched_idx is None:")
    if nomatch_idx == -1:
        errs.append("[A2] 未找到 ``if matched_idx is None:`` 分支")
        nomatch_block = ""
    else:
        # 分支体到 ``plan[matched_idx]["status"]`` 之前
        end_marker = hr_body.find('plan[matched_idx]["status"]', nomatch_idx)
        nomatch_block = hr_body[nomatch_idx:end_marker] if end_marker != -1 else hr_body[nomatch_idx:]

    # A2 all_done 判定
    if "completed" not in nomatch_block or "failed" not in nomatch_block or "all(" not in nomatch_block:
        errs.append("[A2] matched_idx None 分支缺 all_done 判定（all(s.status in completed/failed)）")
    else:
        print("[A2] OK  matched_idx None 分支含 all_done 判定")

    # A3 all_done 命中 → summarize_group（非 llm_decide）
    if 'goto="summarize_group"' not in nomatch_block and "goto='summarize_group'" not in nomatch_block:
        errs.append("[A3] all_done 命中分支应 goto summarize_group（修复核心）")
    else:
        print("[A3] OK  all_done 命中 → summarize_group（缺陷4 修复：跳过 llm_decide 不产二次气泡）")

    # A4 未 all_done 仍 llm_decide
    if 'goto="llm_decide"' not in nomatch_block and "goto='llm_decide'" not in nomatch_block:
        errs.append("[A4] 未 all_done 分支应保留 goto llm_decide（stray-report fallback 不破）")
    else:
        print("[A4] OK  未 all_done → llm_decide（stray-report mid-flight fallback 保留）")

    # ── B. 行为锁——no-match + all_done → summarize_group ────
    try:
        async def _run_b5():
            with patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()), \
                 patch("engine.coordinator.format_step_summary", return_value="SUM"):
                # all_done plan：step1 completed、step2 failed，无 dispatched 步骤
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                     "status": "completed", "task_id": "t1", "result": "ok", "depends_on": []},
                    {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                     "status": "failed", "task_id": "t2", "result": "err", "depends_on": [1]},
                ]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "stray late report", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "unknown", "success": True}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_b5())
        if cmd.goto != "summarize_group":
            errs.append(f"[B5] no-match + all_done 应 goto summarize_group，实际 {cmd.goto!r}")
        elif cmd.update.get("action_taken") != "summarize":
            errs.append(f"[B5] action_taken 应为 summarize，实际 {cmd.update.get('action_taken')!r}")
        else:
            print("[B5] OK  no-match + all_done → summarize_group（缺陷4 修复：直达汇总不绕 llm_decide）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] no-match+all_done 测试异常：{type(e).__name__}: {e}")

    # ── C. 行为锁——no-match + plan 在途 → llm_decide ────────
    try:
        async def _run_c6():
            with patch("engine.coordinator._unified_reply", AsyncMock()):
                # 未 all_done：step1 仍在 dispatched（在途），step2 pending
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                     "status": "dispatched", "task_id": "t1", "depends_on": []},
                    {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                     "status": "pending", "task_id": None, "depends_on": [1]},
                ]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "stray", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "unknown", "success": True}}
                return await node_handle_reply_group(st)
        cmd = asyncio.run(_run_c6())
        if cmd.goto != "llm_decide":
            errs.append(f"[C6] no-match + plan 在途 应 goto llm_decide（stray fallback），实际 {cmd.goto!r}")
        else:
            print("[C6] OK  no-match + plan 在途 → llm_decide（stray-report mid-flight fallback 保真）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] no-match+in-flight 测试异常：{type(e).__name__}: {e}")

    # ── D. 命中路径不破——matched + all_done → summarize_group ─
    try:
        async def _run_d7():
            with patch("engine.coordinator._maybe_adjust_remaining_steps", AsyncMock(return_value=None)) as adj, \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", AsyncMock()):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "done A", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": True}}
                return await node_handle_reply_group(st), adj
        cmd, adj = asyncio.run(_run_d7())
        if cmd.goto != "summarize_group":
            errs.append(f"[D7] matched + all_done 应 goto summarize_group（命中路径不破），实际 {cmd.goto!r}")
        elif cmd.update.get("dispatch_plan", [{}])[0].get("status") != "completed":
            errs.append("[D7] 命中路径 step 应标 completed")
        else:
            print("[D7] OK  matched + all_done → summarize_group（命中路径未被新分支碰）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D7] matched 测试异常：{type(e).__name__}: {e}")

    # ── E. 汇总文本锁——summarize_group 走模板非 LLM 流式 ────
    sum_body = _fn_body(coord_src, "node_summarize_group")
    if "_unified_reply" not in sum_body:
        errs.append("[E8] node_summarize_group 未调 _unified_reply（汇总模板真源断）")
    elif "format_step_summary" not in sum_body:
        errs.append("[E8] node_summarize_group 未用 format_step_summary（模板文本真源断）")
    else:
        print("[E8] OK  summarize_group 走 _unified_reply + format_step_summary（模板非 LLM 流式 → 不产二次气泡）")

    # ── F. 不回归 resident——node_handle_reply 仍 dict ────────
    if not inspect.iscoroutinefunction(node_handle_reply):
        errs.append("[F9] resident node_handle_reply 应仍是 async")
    else:
        # resident 版仍返 dict（action_taken），非 Command goto——本修复只改 group twin
        rh_body = _fn_body(coord_src, "node_handle_reply")
        if "action_taken" not in rh_body:
            errs.append("[F9] resident node_handle_reply 应仍返 action_taken dict（本修复只改 group twin）")
        else:
            print("[F9] OK  resident node_handle_reply 仍返 dict（本修复仅作用于 group twin）")

    return errs


def main() -> int:
    print("=== VH64 回归：缺陷4 修复——report-back no-match + all_done → summarize_group（不绕 llm_decide）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "缺陷4 修复锁定：\n"
        "  · A 静态源码（matched_idx None 分支含 all_done 判定 + summarize_group goto + llm_decide fallback 保留）；\n"
        "  · B no-match + all_done → summarize_group（直达汇总，跳过 llm_decide 不产二次气泡）；\n"
        "  · C no-match + plan 在途 → llm_decide（stray-report mid-flight fallback 不破）；\n"
        "  · D 命中路径不受影响（matched + all_done 仍 summarize_group）；\n"
        "  · E summarize_group 走 _unified_reply + format_step_summary 模板（非 LLM 流式）；\n"
        "  · F resident node_handle_reply 不动（本修复仅作用于 group twin）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
