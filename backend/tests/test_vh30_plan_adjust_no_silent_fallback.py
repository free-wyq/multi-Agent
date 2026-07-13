"""VH30 回归：_parse_plan_adjust_decision 不再 NameError 静默兜底（MT-14 步骤调整路径）.

锁住「先 ``v = extract_json(raw)`` 再判空」的修复——原代码 ``_parse_plan_adjust_decision``
直接 ``if v is None:`` 但 ``v`` 从未定义，**必抛 NameError**。NameError 在调用处
``node_handle_reply`` 的 ``try/except Exception``(:504-506) 里被吞成 ``decision=None``，
导致 **MT-14 步骤调整路径永远静默兜底为「不调整」（返回原 plan）**——LLM 即便返回合法
``adjust=true + revised_steps``，解析也根本没跑到，决策被静默丢弃。

Bug 触发条件：任何 worker report-back（MT-14 链路）——只要走到 ``_maybe_adjust_plan`` →
``chat_completion`` → ``_parse_plan_adjust_decision``，必抛 NameError → 静默兜底。
即 MT-14 的「动态调整」机制**从未真正生效过**（调整决策永远丢失），是 MT-14 端到端
测试（test_mt14_adjust_plan.py，需 live server，不在此静态锁覆盖）无法验证
``adjust=true`` 分支的根因之一.

修复：在 ``if v is None`` 之前补 ``v = extract_json(raw)``（与同文件
``_parse_step_recovery_decision``:830、``node_llm_decide`` 解析同款「先 extract_json 再判空」
两段法口径一致）.

三段契约（纯静态 + 函数直调，不依赖后端在线）：

  A. 源码结构锁——「先赋值再判空」修复存在
    1. ``_parse_plan_adjust_decision`` 函数体内 ``v = extract_json(raw)`` 出现在 ``if v is None``
       之前（顺序正确，NameError 根因消除）.
    2. 函数体内不再有「裸引用 ``v`` 但未先赋值」的 NameError 模式（``if v is None`` 前必有
       ``v = extract_json(...)`` 赋值）.

  B. 行为锁——函数直调不再 NameError，语义正确
    3. 无效 JSON / 空串 → 返回 None（调用者 ``not decision`` 短路返回原 plan，非静默——是
       显式「解析失败=不调整」的合法降级，区别于 NameError 被外层吞成 None 的隐式兜底）.
    4. ``adjust=true + revised_steps`` → 返回 dict，``adjust`` is True，``revised_steps`` 透传.
    5. ``revised_steps`` 缺失/非 list → 规范化为 ``[]``（不抛、不丢 adjust 标志，调用者按
       ``revised`` 空再短路）.
    6. ``adjust=false`` → 返回 dict，``adjust`` is False（调用者 ``not decision.get("adjust")``
       短路返回原 plan）.
    7. ``reason``/``announce`` 缺失 → ``""``（``str(v.get(...,""))`` None-safe，不抛）.

  C. 调用链语义锁——MT-14 调整决策不再被 NameError 静默吞
    8. ``_maybe_adjust_plan`` 调用处 ``try/except Exception`` 仍在（LLM 失败兜底语义不变），
       但 ``_parse_plan_adjust_decision`` 在 try 块内被调——修复后正常返回 decision 不抛，
       ``adjust=true`` 能真正进入修订拼接分支（:508 之后），不再被 except 吞成 None.

与 MT-14 端到端测试（test_mt14_adjust_plan.py）的关系：该测需 live server（localhost:8000）
跑全链路 worker report-back，验证「调整机制运转」；VH30 是其静态前置锁——保证解析层不再
是 NameError 黑洞，给端到端验证一个可信的解析底座（解析修好了，端到端才有可能看到 adjust
分支被真正走到）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = BACKEND if "BACKEND" in dir() else REPO / "backend"
COORD_PY = BACKEND / "engine" / "coordinator.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_func_body(text: str) -> str:
    """Pull the source of ``def _parse_plan_adjust_decision(raw: str) -> dict | None:`` body."""
    m = re.search(
        r"^def _parse_plan_adjust_decision\([^)]*\)[^:]*:\n((?:    .*\n|\n)*)",
        text,
        re.M,
    )
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = _read(COORD_PY)
    body = _extract_func_body(src)

    if not body:
        errs.append("[A0] 未找到 _parse_plan_adjust_decision 函数体（函数被删/改名？）")
        return errs

    # ── A. 源码结构锁 ──────────────────────────────────────────
    # A1: v = extract_json(raw) 必须在 if v is None 之前
    lines = body.splitlines()
    assign_idx = None
    nonecheck_idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.match(r"v\s*=\s*extract_json\s*\(\s*raw\s*\)", s):
            assign_idx = i
        if re.match(r"if\s+v\s+is\s+None\s*:", s) and nonecheck_idx is None:
            nonecheck_idx = i
    if assign_idx is None:
        errs.append("[A1] _parse_plan_adjust_decision 内未找到 `v = extract_json(raw)` 赋值（NameError 根因未修）")
    if nonecheck_idx is None:
        errs.append("[A2] _parse_plan_adjust_decision 内未找到 `if v is None:` 判空")
    if assign_idx is not None and nonecheck_idx is not None and assign_idx > nonecheck_idx:
        errs.append(
            f"[A1] `v = extract_json(raw)` (行 {assign_idx}) 在 `if v is None:` (行 {nonecheck_idx}) "
            f"之后——顺序错误，仍会 NameError"
        )
    if assign_idx is not None and nonecheck_idx is not None and assign_idx < nonecheck_idx:
        print(f"[A1] OK  `v = extract_json(raw)` (行 {assign_idx}) 在 `if v is None:` (行 {nonecheck_idx}) 之前——先赋值再判空，NameError 根因消除")

    # A2: 不再有「裸 if v is None 前无 v 赋值」的 NameError 模式（A1 已覆盖顺序，这里再断言无裸引用）
    # 即 if v is None 之前必须有 v = extract_json —— 已由 A1 顺序断言覆盖，此处只补一条「函数内存在 extract_json 调用」
    if "extract_json" not in body:
        errs.append("[A2] _parse_plan_adjust_decision 函数体内未出现 extract_json（解析逻辑丢失）")
    else:
        print("[A2] OK  函数体内含 extract_json 调用（解析真源在，非裸引用未定义 v）")

    # ── B. 行为锁——函数直调 ───────────────────────────────────
    # 把 backend 加入 sys.path 后直接导入函数调用
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    # engine 包可能在导入时触发副作用，用最小化导入：直接 import 函数
    try:
        from engine.coordinator import _parse_plan_adjust_decision as parse  # type: ignore
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B0] 导入 _parse_plan_adjust_decision 失败：{type(e).__name__}: {e}")
        return errs

    # B3: 无效 JSON / 空串 → None
    for bad in ("", "   ", "not json at all", "{broken", "null"):
        try:
            r = parse(bad)
        except NameError as e:
            errs.append(f"[B3] `{bad!r}` 触发 NameError（修复未生效）: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            errs.append(f"[B3] `{bad!r}` 抛非预期 {type(e).__name__}: {e}")
            continue
        if r is not None:
            errs.append(f"[B3] `{bad!r}` 应返回 None，实际 {r!r}")
    else:
        print("[B3] OK  无效 JSON / 空串 / null → None（显式解析失败降级，非 NameError 静默吞）")

    # B4: adjust=true + revised_steps → dict，adjust True，revised 透传
    r = parse('{"adjust": true, "reason": "需重构", "announce": "调整计划", "revised_steps": [{"step": 2, "agent_id": "a1", "instruction": "do", "depends_on": []}]}')
    if not isinstance(r, dict):
        errs.append(f"[B4] adjust=true 应返回 dict，实际 {type(r).__name__}")
    else:
        if r.get("adjust") is not True:
            errs.append(f"[B4] adjust 应为 True，实际 {r.get('adjust')!r}")
        if not (isinstance(r.get("revised_steps"), list) and len(r["revised_steps"]) == 1):
            errs.append(f"[B4] revised_steps 应透传 1 项 list，实际 {r.get('revised_steps')!r}")
        if r.get("reason") != "需重构":
            errs.append(f"[B4] reason 应透传，实际 {r.get('reason')!r}")
        if r.get("announce") != "调整计划":
            errs.append(f"[B4] announce 应透传，实际 {r.get('announce')!r}")
    if not any(e.startswith("[B4]") for e in errs):
        print("[B4] OK  adjust=true + revised_steps → dict 透传（MT-14 调整决策能真正解析出来）")

    # B5: revised_steps 缺失/非 list → []
    for raw in ('{"adjust": true}', '{"adjust": true, "revised_steps": "notalist"}', '{"adjust": true, "revised_steps": 123}'):
        r = parse(raw)
        if not (isinstance(r, dict) and r.get("revised_steps") == [] and r.get("adjust") is True):
            errs.append(f"[B5] `{raw}` revised_steps 应规范为 []，实际 {r!r}")
    if not any(e.startswith("[B5]") for e in errs):
        print("[B5] OK  revised_steps 缺失/非 list → []（规范化不抛不丢 adjust 标志）")

    # B6: adjust=false → adjust False
    r = parse('{"adjust": false, "revised_steps": []}')
    if not (isinstance(r, dict) and r.get("adjust") is False):
        errs.append(f"[B6] adjust=false 应返回 adjust=False，实际 {r!r}")
    else:
        print("[B6] OK  adjust=false → adjust False（调用者短路返回原 plan）")

    # B7: reason/announce 缺失 → ""
    r = parse('{"adjust": true, "revised_steps": []}')
    if not (isinstance(r, dict) and r.get("reason") == "" and r.get("announce") == ""):
        errs.append(f"[B7] reason/announce 缺失应为 ''，实际 {r!r}")
    else:
        print("[B7] OK  reason/announce 缺失 → ''（str(v.get(...,'')) None-safe）")

    # ── C. 调用链语义锁 ────────────────────────────────────────
    # C8: _maybe_adjust_plan 调用 _parse_plan_adjust_decision 仍在 try 块内
    # 抓 _maybe_adjust_plan 区域里 try: ... _parse_plan_adjust_decision 的相对位置
    m = re.search(r"decision\s*=\s*_parse_plan_adjust_decision\(\s*raw\s*\)", src)
    if not m:
        errs.append("[C8] 未找到 `decision = _parse_plan_adjust_decision(raw)` 调用点")
    else:
        # 向上找最近的 try: 与 except Exception
        call_line = src[: m.start()].count("\n") + 1
        head = src.rsplit("\n", src.count("\n") - (src[: m.start()].count("\n")) + 1)[0]
        # 简化：检查调用点上方 8 行内有 try:，下方 4 行内有 except Exception
        preceding = "\n".join(src.splitlines()[max(0, call_line - 9): call_line - 1])
        following = "\n".join(src.splitlines()[call_line: call_line + 5])
        if re.search(r"^\s*try\s*:", preceding, re.M) and re.search(r"except\s+Exception", following, re.M):
            print("[C8] OK  调用点在 try/except Exception 内（LLM 失败兜底语义不变，修复后正常返回不再被吞）")
        else:
            errs.append("[C8] 调用点未在 try/except Exception 内（兜底语义可能改变，需复核）")

    return errs


def main() -> int:
    print("=== VH30 回归：_parse_plan_adjust_decision 不再 NameError 静默兜底（MT-14）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "MT-14 步骤调整解析路径锁定：\n"
        "  · A 先 `v = extract_json(raw)` 再 `if v is None`（NameError 根因消除，与 _parse_step_recovery/node_llm_decide 同款两段法）；\n"
        "  · B 函数直调不再 NameError——无效 JSON→None、adjust=true 透传 revised、缺失规范化 []、None-safe reason/announce；\n"
        "  · C 调用点仍在 try/except Exception 内（兜底语义不变，修复后 decision 正常返回不被吞，adjust=true 能进修订拼接分支）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
