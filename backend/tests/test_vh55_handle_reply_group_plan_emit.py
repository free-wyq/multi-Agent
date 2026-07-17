"""VH55 回归：node_handle_reply_group 状态推进广播 emit（Bug B：计划实时可视化）.

锁住「直接干」模式下计划不可见的修复——``node_handle_reply_group`` 在步骤状态从
``dispatched`` 翻 ``completed``/``failed`` 后必须 ``emit_coordinator_plan`` 推给前端，
否则前端 ``PlanConfirmCard`` 停在派发态看不到进度（只有 dispatch_next 派发时 emit、
summarize 收尾 emit 空 plan，中间报告回收这步缺 emit）。

五段契约（纯静态 + 函数直调 stub，不依赖 live server / 真实 LLM）：

  A. emit 调用锁——状态 mutation 后、MT-15/MT-14 之前有 emit_coordinator_plan
    1. ``node_handle_reply_group`` body 含 ``emit_coordinator_plan(`` 调用.
    2. emit 位于 ``plan[matched_idx]["status"]`` mutation 之后.
    3. emit 位于 ``_maybe_handle_step_failure`` / ``_maybe_adjust_remaining_steps`` 之前.

  B. best-effort 锁——emit 包 try/except + logger.exception（对齐 vh25 class 1 模式）
    4. emit 调用包在 ``try:`` ... ``except Exception:`` + ``logger.exception``.

  C. success 路径——completed emit 被调一次，step 标 completed
    5. 2 步计划（step1 dispatched deps=[]、step2 pending deps=[1]），patch adjust no-op +
       ``_unified_reply`` + emit 记录器，断言 emit 被调一次，``plan[0].status=='completed'``，
       随后 ``Command(goto="dispatch_next_group")``.

  D. failure 路径——failed emit 被调一次，step 标 failed
    6. success=False，patch ``_maybe_handle_step_failure`` keep_failed（单步计划 all_done），
       断言 emit 被调一次，``plan[0].status=='failed'``，随后 ``Command(goto="summarize_group")``.

  E. 向后兼容锁——resident node_handle_reply 不动（legacy 路径不加新 emit）
    7. resident ``node_handle_reply`` body 无新增 emit（coordinator.py:~409 处 mutation 不 emit）.
"""
from __future__ import annotations

import asyncio
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


def _fn_body(src: str, fn_name: str) -> str:
    """Return one async function body (def ... to next column-0 def)."""
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


def _strip_comments_and_strings(src: str) -> str:
    """Strip triple-quoted blocks + ``#`` line comments → leave only code."""
    no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
    no_doc = re.sub(r"'''[\s\S]*?'''", "", no_doc)
    out_lines = []
    for ln in no_doc.splitlines():
        hash_idx = ln.find("#")
        if hash_idx >= 0:
            ln = ln[:hash_idx]
        out_lines.append(ln)
    return "\n".join(out_lines)


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord_src = _read(COORD_PY)

    try:
        from engine.coordinator import (  # type: ignore
            node_handle_reply,
            node_handle_reply_group,
        )
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    hr_body = _fn_body(coord_src, "node_handle_reply_group")
    hr_code = _strip_comments_and_strings(hr_body)

    # ── A. emit 调用锁 ──────────────────────────────────────────
    # A1 body 含 emit_coordinator_plan 调用
    if "emit_coordinator_plan(" not in hr_code:
        errs.append("[A1] node_handle_reply_group 未调 emit_coordinator_plan（Bug B：状态推进不广播）")
    else:
        print("[A1] OK  node_handle_reply_group 含 emit_coordinator_plan 调用")

    # A2 emit 位于 status mutation 之后
    mut_idx = hr_code.find('["status"] = "completed" if success else "failed"')
    emit_idx = hr_code.find("emit_coordinator_plan(")
    if mut_idx < 0:
        errs.append("[A2] 未找到 status mutation 行（结构可能已变，需人工复核）")
    elif emit_idx < 0:
        errs.append("[A2] 未找到 emit_coordinator_plan 调用（A1 已报，跳过顺序断言）")
    elif emit_idx < mut_idx:
        errs.append("[A2] emit_coordinator_plan 应位于 status mutation 之后，实际在之前")
    else:
        print("[A2] OK  emit 位于 status mutation 之后")

    # A3 emit 位于 MT-15/MT-14 恢复之前
    mt15_idx = hr_code.find("_maybe_handle_step_failure")
    mt14_idx = hr_code.find("_maybe_adjust_remaining_steps")
    recovery_idx = min(
        x for x in (mt15_idx, mt14_idx) if x >= 0
    ) if any(x >= 0 for x in (mt15_idx, mt14_idx)) else -1
    if emit_idx < 0:
        errs.append("[A3] emit 未找到（A1 已报）")
    elif recovery_idx < 0:
        errs.append("[A3] 未找到 MT-15/MT-14 恢复调用（结构可能已变，需人工复核）")
    elif emit_idx > recovery_idx:
        errs.append("[A3] emit 应位于 _maybe_handle_step_failure/_maybe_adjust_remaining_steps 之前，实际在之后")
    else:
        print("[A3] OK  emit 位于 MT-15/MT-14 恢复之前（mutation→广播→恢复 顺序正确）")

    # ── B. best-effort 锁 ────────────────────────────────────────
    # A4/B4 emit 包 try/except + logger.exception
    try_block = hr_code
    try_idx = try_block.find("try:")
    except_idx = try_block.find("except Exception")
    log_exc_idx = try_block.find("logger.exception")
    if try_idx < 0 or except_idx < 0:
        errs.append("[B4] emit 未包 try/except（best-effort 模式缺失，emit 失败会挡恢复逻辑）")
    elif log_exc_idx < 0 or log_exc_idx < except_idx:
        errs.append("[B4] except 块缺 logger.exception（对齐 vh25 class 1 best-effort 模式）")
    elif not (try_idx < emit_idx < except_idx):
        errs.append("[B4] emit 不在 try/except 块内（顺序异常）")
    else:
        print("[B4] OK  emit 包 try/except Exception + logger.exception（best-effort，对齐 vh25 class 1）")

    # ── C. success 路径 emit ────────────────────────────────────
    try:
        async def _run_c5():
            emitted = []
            async def fake_adjust(state, plan): return plan
            async def record_emit(group_id, coordinator_id, plan):
                emitted.append([dict(s) for s in plan])
            with patch("engine.coordinator._maybe_adjust_remaining_steps", fake_adjust), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", record_emit):
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
                return await node_handle_reply_group(st), emitted
        cmd, emitted = asyncio.run(_run_c5())
        if cmd.goto != "dispatch_next_group":
            errs.append(f"[C5] success+pending 应 goto dispatch_next_group，实际 {cmd.goto!r}")
        elif len(emitted) != 1:
            errs.append(f"[C5] handle_reply_group 应 emit 一次（completed），实际 {len(emitted)} 次")
        elif not emitted or emitted[0][0].get("status") != "completed":
            errs.append(f"[C5] emit 的 plan[0].status 应为 completed，实际 {emitted[0][0].get('status')!r}")
        else:
            print("[C5] OK  success 路径 emit 一次 plan[0]=completed → dispatch_next_group（状态推进广播）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C5] success emit 测试异常：{type(e).__name__}: {e}")

    # ── D. failure 路径 emit ────────────────────────────────────
    try:
        async def _run_d6():
            emitted = []
            async def fake_failure(state, plan, idx): return plan  # keep_failed
            async def record_emit(group_id, coordinator_id, plan):
                emitted.append([dict(s) for s in plan])
            with patch("engine.coordinator._maybe_handle_step_failure", fake_failure), \
                 patch("engine.coordinator._unified_reply", AsyncMock()), \
                 patch("engine.coordinator.emit_coordinator_plan", record_emit):
                plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                         "status": "dispatched", "task_id": "t1", "depends_on": []}]
                st = {"group_id": "g1", "coordinator_id": "c1", "agent_id": "c1",
                      "agent_name": "Coord", "system_prompt": "", "dispatch_plan": plan,
                      "incoming_message": "err", "incoming_sender": "w1",
                      "incoming_kind": "agent_reply",
                      "incoming_data": {"task_id": "t1", "success": False}}
                return await node_handle_reply_group(st), emitted
        cmd, emitted = asyncio.run(_run_d6())
        if cmd.goto != "summarize_group":
            errs.append(f"[D6] failure+keep_failed+all_done 应 goto summarize_group，实际 {cmd.goto!r}")
        elif len(emitted) != 1:
            errs.append(f"[D6] handle_reply_group 应 emit 一次（failed），实际 {len(emitted)} 次")
        elif not emitted or emitted[0][0].get("status") != "failed":
            errs.append(f"[D6] emit 的 plan[0].status 应为 failed，实际 {emitted[0][0].get('status')!r}")
        else:
            print("[D6] OK  failure 路径 emit 一次 plan[0]=failed → summarize_group（失败状态推进广播）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D6] failure emit 测试异常：{type(e).__name__}: {e}")

    # ── E. 向后兼容：resident node_handle_reply 不动 ────────────
    # E7 resident node_handle_reply body 不含新增 emit（legacy 路径，生产已死，不加 emit）
    resident_body = _fn_body(coord_src, "node_handle_reply")
    resident_code = _strip_comments_and_strings(resident_body)
    # resident 仍有 status mutation（matched_idx）——确认它没有伴随的 emit_coordinator_plan 调用
    if "emit_coordinator_plan(" in resident_code:
        errs.append("[E7] resident node_handle_reply 不应新增 emit（legacy 路径不动，避免噪音）")
    else:
        print("[E7] OK  resident node_handle_reply 未加 emit（legacy 路径不动，群图 twin 独占修复）")

    return errs


def main() -> int:
    print("=== VH55 回归：node_handle_reply_group 状态推进广播 emit（Bug B：计划实时可视化）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "handle_reply_group 状态推进广播锁定：\n"
        "  · A emit_coordinator_plan 在 status mutation 之后、MT-15/MT-14 之前；\n"
        "  · B emit 包 try/except + logger.exception（best-effort，对齐 vh25 class 1）；\n"
        "  · C success 路径 emit 一次 plan[0]=completed → dispatch_next_group；\n"
        "  · D failure 路径 emit 一次 plan[0]=failed → summarize_group；\n"
        "  · E resident node_handle_reply 不动（legacy 路径，群图 twin 独占修复）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
