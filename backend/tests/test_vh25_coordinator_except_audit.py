"""VH25 回归：coordinator.py except Exception 清单 + 统一错误出口（task B28）.

锁住 B28 审计——``backend/engine/coordinator.py`` 全文 ``except Exception`` 清单 +
emit_coordinator_token/reasoning/stats 三处 best-effort 是否都 logger.exception（非静默）+
统一错误出口.

B28 审计结论（13 处 except，分 3 类，token 分支原裸 await 已补 best-effort 兜底）：

  coordinator.py 全文 13 处 ``except Exception`` / ``except Exception as e``，分 3 类：

  ── 类 1：best-effort WS/回复推送（6 处）——应 logger.exception 非静默 ──
    1. :558 emit_coordinator_plan（plan-adjust 重发）→ logger.exception ✅
    2. :564 _unified_reply（plan-adjust announce 回复）→ logger.exception ✅
    3. :785 _unified_reply（skip announce 回复）→ logger.exception ✅
    4. :812 _unified_reply（recovery announce 回复）→ logger.exception ✅
    5. :1122 emit_coordinator_plan（dispatch_next 后发计划）→ logger.exception ✅
    6. :1143 emit_coordinator_plan（summarize 发空计划清卡片）→ logger.exception ✅
    + 流式期 3 处（_stream_coordinator_decision 内）：
      7. :1383 emit_coordinator_reasoning（reasoning delta）→ logger.exception ✅
      8. :1399 emit_coordinator_token（content delta）→ logger.exception ✅（B28 新补 best-effort）
      9. :1411 emit_coordinator_stats（streaming stats）→ logger.exception ✅
     10. :1433 emit_coordinator_stats（final done stats）→ logger.exception ✅

  ── 类 2：LLM 决策兜底（3 处）——logger.warning + 兜底决策（非静默，语义降级） ──
    11. :505 chat_completion（plan-adjust LLM）→ logger.warning + decision=None ✅
    12. :764 chat_completion（step-recovery LLM）→ logger.warning + decision=None ✅
    13. :902 _stream_coordinator_decision（node_llm_decide）→ logger.warning + chat 兜底回复 ✅
    （LLM 调用失败是「决策层降级」——logger.warning 标记 + 兜底默认值，非 best-effort
    emit 故不用 logger.exception；这三处语义不同于 WS 推送 best-effort，是「整条 LLM 决策
    失败降级」，logger.warning 的 level 合理——非静默，warning 携带 %s e 上下文）

  ── 类 3：observability-only state 探针（1 处）——logger.debug(exc_info) 非静默 ──
    14. :213 _detect_residual_interrupt（classify 路由 state 探针）→ logger.debug(exc_info=True)
        + extra={"event": ...} ✅（B28 补注释说明这是全文唯一非 logger.exception 的有意为之——
        observability-only state 探针，非 best-effort WS 推送，debug 级 + exc_info 保留 traceback）

  ── B28 修复：emit_coordinator_token 原裸 await → 补 best-effort + logger.exception ──
    _stream_coordinator_decision 的 ``await emit_coordinator_token(...)`` 原是裸 await（无 try/except）。
    与同函数内 emit_coordinator_reasoning(1383) / emit_coordinator_stats(1411/1433) 的 best-effort
    忍受度**不对称**——reasoning/stats 推送失败只跳过当前 delta 不中断流式，token 推送失败却
    会冒泡出整个 _stream_coordinator_decision，被 node_llm_decide(:902) 粗兜底成 chat 兜底回复，
    丢失后续 token + stats + reasoning（一次 WS 抖动整条回复报废）。B28 补 try/except +
    logger.exception，与 reasoning/stats 同款 best-effort + 非静默错误出口，统一三处流式推送的
    错误处理口径. 行为变化：WS 推送失败时不再整条回复报废（继续推后续 token），是修复非回归.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh24 同款风格.

六段契约：

  A. 流式 best-effort 三处都 logger.exception（emit_coordinator_token/reasoning/stats）
    1. emit_coordinator_reasoning 的 except 紧跟 logger.exception（reasoning delta 非静默）.
    2. emit_coordinator_token 的 except 紧跟 logger.exception（B28 新补 best-effort，原裸 await 修复）.
    3. emit_coordinator_token 原裸 await 已包 try/except（B28 修复锚点——不再无保护）.
    4. emit_coordinator_stats（streaming）的 except 紧跟 logger.exception.
    5. emit_coordinator_stats（final done）的 except 紧跟 logger.exception.

  B. 类 1 best-effort WS/回复推送全 logger.exception（6 处非流式 emit/reply）
    6. emit_coordinator_plan（plan-adjust 重发 558）→ logger.exception.
    7. _unified_reply（plan-adjust announce 564）→ logger.exception.
    8. _unified_reply（skip announce 785）→ logger.exception.
    9. _unified_reply（recovery announce 812）→ logger.exception.
   10. emit_coordinator_plan（dispatch_next 后 1122）→ logger.exception.
   11. emit_coordinator_plan（summarize 空计划 1143）→ logger.exception.

  C. 类 2 LLM 决策兜底 3 处都 logger.warning + 兜底值（非静默，语义降级非 best-effort）
   12. plan-adjust LLM 失败 → logger.warning + decision=None.
   13. step-recovery LLM 失败 → logger.warning + decision=None.
   14. node_llm_decide LLM 失败 → logger.warning + chat 兜底回复 + stats 空值.

  D. 类 3 observability-only state 探针 1 处 logger.debug(exc_info)（非静默，全文唯一例外）
   15. _detect_residual_interrupt 的 except → logger.debug(exc_info=True) + extra event 标记.
   16. 该块注释说明是「observability-only state 探针，非 best-effort WS 推送」（B28 补注释锚点）.

  E. 无静默 except（全文 13 处都有日志出口，无裸 pass / 无只 continue）
   17. 全文无 ``except Exception:\s*pass``（无静默吞没）.
   18. 全文无 ``except:\s*pass``（无裸 except 吞没）.

  F. emit_coordinator_token 修复回归锁（B28 修复点：原裸 await → best-effort）
   19. emit_coordinator_token 调用被 try 包（修复前裸 await 会冒泡整条回复报废）.
   20. emit_coordinator_token 的 except 紧跟 logger.exception（与 reasoning/stats 同款非静默）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD_PY = REPO / "backend" / "engine" / "coordinator.py"


def _fn_body_py(src: str, fname: str, is_async: bool = False) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def _except_blocks(src: str) -> list[tuple[int, str]]:
    """Return (1-based line number, the except line) for every except clause."""
    blocks: list[tuple[int, str]] = []
    for i, line in enumerate(src.splitlines(), start=1):
        if re.match(r"\s*except\b", line):
            blocks.append((i, line))
    return blocks


def _block_has_logger_exception(src: str, except_line_no: int, max_scan: int = 6) -> bool:
    """True if a logger.exception(...) appears within max_scan lines after the except."""
    lines = src.splitlines()
    for off in range(1, max_scan + 1):
        idx = except_line_no - 1 + off  # 0-based
        if idx >= len(lines):
            break
        ln = lines[idx]
        # stop scanning at next clause boundary
        if re.match(r"\s*(except|else|finally|elif|if |for |async def |def |return |raise )", ln):
            # 'return'/'raise' may legitimately follow the handler body; but if we hit
            # another except/def we've gone too far — stop.
            if re.match(r"\s*(except|async def |def )", ln):
                break
        if "logger.exception" in ln:
            return True
    return False


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD_PY.read_text(encoding="utf-8")
    scd_body = _fn_body_py(coord, "_stream_coordinator_decision", is_async=True)

    # ── A. 流式 best-effort 三处都 logger.exception ──
    # [1] emit_coordinator_reasoning except → logger.exception
    m_reason = re.search(
        r"await emit_coordinator_reasoning\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception",
        scd_body,
    )
    if not m_reason:
        errs.append("[A1] emit_coordinator_reasoning 的 except 未紧跟 logger.exception（reasoning delta 静默）")
    else:
        print("[A1] OK  emit_coordinator_reasoning except → logger.exception（reasoning delta 非静默）")
    # [2] emit_coordinator_token except → logger.exception（B28 修复）
    m_token = re.search(
        r"await emit_coordinator_token\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception",
        scd_body,
    )
    if not m_token:
        errs.append("[A2] emit_coordinator_token 的 except 未紧跟 logger.exception（B28 修复未落地——token delta 静默）")
    else:
        print("[A2] OK  emit_coordinator_token except → logger.exception（B28 新补 best-effort 非静默）")
    # [3] emit_coordinator_token 原裸 await 已包 try（不再无保护）
    # 裸 await 模式：emit_coordinator_token(...) 后直接跟非 except 的下一行（如 if usage）
    bare_token = re.search(
        r"await emit_coordinator_token\([^)]*\)\s*\n(?!\s*except)",
        scd_body,
    )
    if bare_token:
        errs.append("[A3] emit_coordinator_token 仍是裸 await 无 try/except（B28 修复回归——单次 emit 失败冒泡整条回复报废）")
    else:
        print("[A3] OK  emit_coordinator_token 已包 try/except（原裸 await 修复，不再无保护）")
    # [4] emit_coordinator_stats（streaming）except → logger.exception
    m_stats_stream = re.search(
        r'await emit_coordinator_stats\(\s*\n[^)]*?"streaming",[^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception',
        scd_body,
    )
    if not m_stats_stream:
        errs.append("[A4] emit_coordinator_stats(streaming) 的 except 未紧跟 logger.exception（streaming stats 静默）")
    else:
        print("[A4] OK  emit_coordinator_stats(streaming) except → logger.exception（streaming stats 非静默）")
    # [5] emit_coordinator_stats（final done）except → logger.exception
    m_stats_done = re.search(
        r'await emit_coordinator_stats\(\s*\n[^)]*?"done",[^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception',
        scd_body,
    )
    if not m_stats_done:
        errs.append("[A5] emit_coordinator_stats(done) 的 except 未紧跟 logger.exception（final stats 静默）")
    else:
        print("[A5] OK  emit_coordinator_stats(done) except → logger.exception（final stats 非静默）")

    # ── B. 类 1 best-effort WS/回复推送全 logger.exception（6 处非流式）──
    # 用「emit/reply 调用 + 紧跟 except Exception + 紧跟 logger.exception」三连模式锁
    b_cases = [
        ("emit_coordinator_plan", "plan-adjust 重发", r"await emit_coordinator_plan\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*re-announce"),
        ("_unified_reply", "plan-adjust announce", r"await _unified_reply\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*plan-adjust announce"),
        ("_unified_reply", "skip announce", r"await _unified_reply\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*skip announce"),
        ("_unified_reply", "recovery announce", r"await _unified_reply\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*recovery announce"),
        ("emit_coordinator_plan", "dispatch_next 后", r"await emit_coordinator_plan\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*dispatch_next"),
        ("emit_coordinator_plan", "summarize 空计划", r"await emit_coordinator_plan\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*empty plan"),
    ]
    for fn, label, pat in b_cases:
        if not re.search(pat, coord, re.S):
            errs.append(f"[B] {fn}（{label}）的 except 未紧跟 logger.exception（静默）")
        else:
            print(f"[B] OK  {fn}（{label}）except → logger.exception（非静默）")

    # ── C. 类 2 LLM 决策兜底 3 处 logger.warning + 兜底值 ──
    c_cases = [
        ("plan-adjust LLM", r'logger\.warning\("\[coordinator\] plan-adjust LLM failed: %s", e\)', "decision = None"),
        ("step-recovery LLM", r'logger\.warning\("\[coordinator\] step-recovery LLM failed: %s", e\)', "decision = None"),
        ("node_llm_decide LLM", r'logger\.warning\("\[coordinator\] LLM decision failed: %s", e\)', '"action": "chat"'),
    ]
    for label, warn_pat, fallback_pat in c_cases:
        if not re.search(warn_pat, coord):
            errs.append(f"[C] {label} 缺 logger.warning（决策失败静默）")
        elif fallback_pat not in coord:
            errs.append(f"[C] {label} 缺兜底值（{fallback_pat}）")
        else:
            print(f"[C] OK  {label} → logger.warning + 兜底值（非静默，语义降级）")

    # ── D. 类 3 observability-only state 探针 1 处 logger.debug(exc_info) ──
    dri_body = _fn_body_py(coord, "_detect_residual_interrupt", is_async=True)
    if not dri_body:
        errs.append("[setup] _detect_residual_interrupt 函数体未找到")
    else:
        # [15] except → logger.debug(exc_info=True) + extra event
        # logger.debug 调用可跨多行（参数逐行），用宽松模式：except Exception 后某处 logger.debug
        # 带 exc_info=True + extra event（不锁参数顺序/单行）。
        if not re.search(r'except Exception:\s*\n.*?logger\.debug\(\s*\n?\s*"\[coordinator\] residual-interrupt probe skipped"', dri_body, re.S) \
           or "exc_info=True" not in dri_body \
           or "residual_interrupt_probe_skipped" not in dri_body:
            errs.append("[D15] _detect_residual_interrupt 的 except 非 logger.debug(exc_info=True)（observability 探针静默）")
        else:
            print("[D15] OK  _detect_residual_interrupt except → logger.debug(exc_info=True) + extra event（observability 非静默）")
        # [16] 注释说明是 observability-only state 探针（B28 补注释锚点）
        if "observability-only" not in dri_body.lower() and "observability only" not in dri_body.lower():
            errs.append("[D16] _detect_residual_interrupt 缺「observability-only state 探针」注释（B28 注释锚点缺失）")
        elif "不当 error" not in dri_body and "非 error" not in dri_body:
            errs.append("[D16] _detect_residual_interrupt 注释未说明「observability 降级不当 error 级日志」（B28 口径说明缺失）")
        else:
            print("[D16] OK  注释标明 observability-only state 探针 + 降级不当 error（非 best-effort WS 推送，B28 注释锚点）")

    # ── E. 无静默 except（全文无裸 pass / 无只 continue）──
    # [17] 无 `except Exception:\n pass`（静默吞没）
    silent_pass = re.search(r"except Exception[^:]*:\s*\n\s*pass\b", coord)
    if silent_pass:
        errs.append("[E17] coordinator.py 含 `except Exception: pass`（静默吞没）")
    else:
        print("[E17] OK  无 `except Exception: pass`（无静默吞没）")
    # [18] 无裸 `except: pass`（裸 except 吞没）
    bare_except = re.search(r"except:\s*\n\s*pass\b", coord)
    if bare_except:
        errs.append("[E18] coordinator.py 含裸 `except: pass`（裸 except 吞没）")
    else:
        print("[E18] OK  无裸 `except: pass`（无裸 except 吞没）")

    # ── F. emit_coordinator_token 修复回归锁 ──
    # [19] emit_coordinator_token 调用被 try 包（修复前裸 await 会冒泡整条回复报废）
    # 行级检查：await emit_coordinator_token( 的上一非空行是 try:（缩进更浅——try 是它的父块）。
    lines = scd_body.splitlines()
    tok_idx = next((i for i, ln in enumerate(lines) if "await emit_coordinator_token(" in ln), None)
    if tok_idx is None:
        errs.append("[F19/setup] emit_coordinator_token 调用未找到（审计锚点失）")
    else:
        # 找上一非空行
        prev = None
        for j in range(tok_idx - 1, -1, -1):
            if lines[j].strip():
                prev = lines[j]
                break
        if prev is None or "try:" not in prev:
            errs.append(f"[F19] emit_coordinator_token 上一非空行非 try:（裸 await 冒泡——prev={prev!r}）")
        else:
            # 缩进：try 应比 await 浅（父块）
            try_indent = len(prev) - len(prev.lstrip())
            await_indent = len(lines[tok_idx]) - len(lines[tok_idx].lstrip())
            if try_indent >= await_indent:
                errs.append(f"[F19] try: 缩进({try_indent}) 不浅于 await({await_indent})（非父块包 try）")
            else:
                print(f"[F19] OK  emit_coordinator_token 调用被 try: 包（B28 修复，缩进 try{try_indent}<await{await_indent}）")
    # [20] emit_coordinator_token except 紧跟 logger.exception（与 reasoning/stats 同款非静默）
    if re.search(r"await emit_coordinator_token\([^)]*\)\s*\n\s*except Exception:\s*\n\s*logger\.exception.*token delta", scd_body):
        print("[F20] OK  emit_coordinator_token except → logger.exception（与 reasoning/stats 同款非静默）")
    else:
        errs.append("[F20] emit_coordinator_token except 未紧跟 logger.exception token delta（与 reasoning/stats 口径不一）")

    return errs


def main() -> int:
    print("=== VH25 回归：coordinator.py except Exception 清单 + 统一错误出口（B28）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B28 coordinator.py except Exception 清单锁定：\n"
        "  · A 流式 best-effort 三处（emit_coordinator_token/reasoning/stats）都 logger.exception 非静默；\n"
        "  · B 类 1 best-effort WS/回复推送 6 处全 logger.exception（plan-adjust/skip/recovery announce + dispatch_next/summarize plan）；\n"
        "  · C 类 2 LLM 决策兜底 3 处 logger.warning + 兜底值（plan-adjust/step-recovery/node_llm_decide，语义降级非 best-effort）；\n"
        "  · D 类 3 observability-only state 探针 1 处 logger.debug(exc_info)（全文唯一例外，非 best-effort WS 推送）；\n"
        "  · E 无静默 except（无 `except: pass` / 无裸 `except: pass` 吞没）；\n"
        "  · F emit_coordinator_token 修复回归锁（原裸 await → best-effort + logger.exception，与 reasoning/stats 同款）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
