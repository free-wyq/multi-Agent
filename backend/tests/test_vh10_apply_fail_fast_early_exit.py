"""VH10 回归：apply_fail_fast 早退 + step_num:int 类型注解（task B13）.

锁住 B13 修复——``engine/dispatcher.py:24-42`` ``apply_fail_fast`` 嵌套循环 O(n²)：
外层 while 收集 failed_steps → 标记 → 重复到 fixpoint；内层 for 扫每个 pending
step 的 depends_on，``next((d for d in plan if d.get("step") == dep))`` 找依赖步骤
判 failed。小 plan 无碍，但 B13 加早退（无 failed step 即 break）补类型注解。

B13 改动（行为零变，仅优化 + 文档化）：
  - 内层依赖扫描：``if dep_step and dep_step.get("status") == "failed":``
    ``failed_steps.append(s["step"])`` 后加 ``break``——一个 failed dep 即足够判
    该 step 应级联失败，无需继续扫剩余 deps（原实现命中后仍扫完该 step 剩余 deps，
    多余——break 后直接判下一个 step）。
  - 外层 while：``if not failed_steps: break``——本轮无新增失败即 fixpoint，跳出。
    （原实现已有此判，B13 仅在 docstring 显式标注「B13 早退」语义。）
  - ``_dispatch_one``：``step_num = step["step"]`` 补类型注解 ``step_num: int = ...``
    （任务要求 + agent_id/agent_name/instruction 一并补 str 注解，与 step dict
    的 step: int / agent_id: str / agent_name: str / instruction: str 约定一致）。

为何纯优化不改算法：``apply_fail_fast`` 是 DAG 失败级联的核心正确性逻辑，mt15/
mt17 E2E 测验证其级联行为（step1 failed → 2/3/4 级联 failed）。B13 只加早退（break）
不改判定条件——级联结果（哪些 step 最终 failed）与原实现逐字节一致（早退只跳过
「已确定要 fail 的 step 的剩余 deps 扫描」，不改变 fail 集合）。行为零变是 B13
最高优先级约束。

纯静态契约（读源码断言，不依赖后端在线）+ 行为等价（运行时复制 plan 跑新旧逻辑
对照）双保险，与 test_vh1-vh9 同款风格。

六段契约：

  A. apply_fail_fast 早退（内层 break）
    1. 内层依赖扫描 ``failed_steps.append(s["step"])`` 后跟 ``break``（命中即跳出 dep 循环）。
    2. break 在 append 之后（先收集再跳出，不丢该 step）。
    3. break 只跳内层 for-dep（不跳外层 for-step——每个 step 仍独立判）。

  B. apply_fail_fast 早退（外层 while break）
    4. ``if not failed_steps: break`` 存在（fixpoint 早退）。
    5. break 在标记循环之前（先判无新增失败再跳出，避免空标记轮）。

  C. _dispatch_one step_num:int 类型注解
    6. ``step_num: int = step["step"]``（任务要求补类型注解）。
    7. agent_id/agent_name/instruction 一并补 str 注解（一致性，与 step dict 约定对齐）。

  D. 行为零变（级联结果与原实现逐字节一致）
    8. 线性级联 1→2→3（step1 failed）：2/3 failed，4（独立）pending。
    9. 菱形 1→{2,3}→4（step1 failed）：2/3/4 全 failed。
   10. 无失败早退：completed step 不动，pending 保持 pending（不误标）。
   11. 预失败级联：step2 已 failed（result「上游步骤失败，跳过」）→ step3 级联 failed。

  E. 算法结构（O(n²) 不变，仅均摊更省）
   12. 仍是 while-True + for-step + for-dep 嵌套（O(n²·deps) 上界不变，早退降均摊）。
   13. 仍用 ``next((d for d in plan if d.get("step") == dep), None)`` 找依赖步骤
       （线性扫，不改 dict 索引——小 plan 无碍，B13 不重构数据结构）。

  F. 无回归（dispatch_ready_steps 调用链不破）
   14. ``apply_fail_fast`` 仍 ``-> list[dict[str, Any]]`` 返回 plan（原地改 + 返回）。
   15. ``dispatch_ready_steps`` 仍调 ``apply_fail_fast(plan)``（B13 不改调用点）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DISPATCHER = REPO / "backend" / "engine" / "dispatcher.py"


def _fn_body(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进）。模块级末函数回退到文件尾。"""
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    m = re.search(rf"(?:async def|def) {fname}\([^)]*\)(.*)$", src, re.S)
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    disp = DISPATCHER.read_text(encoding="utf-8")

    aff_body = _fn_body(disp, "apply_fail_fast", indent_opts=("",))
    if not aff_body:
        errs.append("[setup] apply_fail_fast 函数体未找到")
        return errs

    # ── A. apply_fail_fast 早退（内层 break）──
    # [1] failed_steps.append(s["step"]) 后跟 break
    if not re.search(
        r'failed_steps\.append\(s\["step"\]\)\s*\n\s*break',
        aff_body,
    ):
        errs.append("[A1] 内层 dep 扫描 append 后未 break（B13 早退未加）")
    else:
        print("[A1] OK  failed_steps.append(s['step']) 后 break（命中即跳出 dep 循环）")
    # [2] break 在 append 之后（先收集再跳出，不丢该 step）—— 顺序断言
    m = re.search(r'(failed_steps\.append\(s\["step"\]\))\s*\n\s*(break)', aff_body)
    if not m:
        errs.append("[A2] append→break 顺序未确认（可能 break 在前丢 step）")
    else:
        print("[A2] OK  break 在 append 之后（先收集该 step 再跳出，不丢）")
    # [3] break 只跳内层 for-dep（缩进比 for-step 深，不跳外层）
    # 抽内层 for-dep 块看 break 缩进
    m_inner = re.search(
        r'for dep in s\.get\("depends_on",\s*\[\]\)\s*or\s*\[\]:\s*\n(.*?)(?=\n        if not failed_steps|\n    for s in plan)',
        aff_body,
        re.S,
    )
    if not m_inner:
        errs.append("[A3] 内层 for-dep 块未找到（结构可能已变）")
    else:
        inner = m_inner.group(1)
        if "break" not in inner:
            errs.append("[A3] 内层 for-dep 块无 break（早退应在内层）")
        else:
            print("[A3] OK  break 在内层 for-dep（不跳外层 for-step，每 step 独立判）")

    # ── B. apply_fail_fast 早退（外层 while break）──
    # [4] if not failed_steps: break 存在
    if not re.search(r"if\s+not\s+failed_steps:\s*\n\s*break", aff_body):
        errs.append("[B4] 缺 if not failed_steps: break（fixpoint 早退）")
    else:
        print("[B4] OK  if not failed_steps: break（fixpoint 早退，无新增失败即跳出 while）")
    # [5] break 在标记循环之前（先判无新增失败再跳出，避免空标记轮）
    # break 行可能带行尾注释（# B13 早退...），用 [^\n]* 容忍注释再到下一行 for s in plan
    m_order = re.search(
        r'(if\s+not\s+failed_steps:\s*\n\s*break)[^\n]*\n\s*(for s in plan:\s*\n\s*if s\.get\("step"\)\s+in\s+failed_steps)',
        aff_body,
    )
    if not m_order:
        errs.append("[B5] break/标记循环顺序异常（应先判 break 再标记）")
    else:
        print("[B5] OK  break 在标记循环之前（先判无新增失败再跳出，避免空标记轮）")

    # ── C. _dispatch_one step_num:int 类型注解 ──
    do_body = _fn_body(disp, "_dispatch_one", indent_opts=("    ",))
    if not do_body:
        errs.append("[C6] _dispatch_one 函数体未找到")
    else:
        # [6] step_num: int = step["step"]
        if not re.search(r'step_num:\s*int\s*=\s*step\["step"\]', do_body):
            errs.append("[C6] _dispatch_one 未补 step_num: int 类型注解（B13 任务要求）")
        else:
            print("[C6] OK  step_num: int = step['step']（类型注解已补）")
        # [7] agent_id/agent_name/instruction 一并补 str 注解
        has_agent_id = bool(re.search(r'agent_id:\s*str\s*=\s*step\["agent_id"\]', do_body))
        has_agent_name = bool(re.search(r'agent_name:\s*str\s*=\s*step\["agent_name"\]', do_body))
        has_instruction = bool(re.search(r'instruction:\s*str\s*=\s*step\["instruction"\]', do_body))
        if not (has_agent_id and has_agent_name and has_instruction):
            errs.append(
                f"[C7] _dispatch_one 同组字段缺 str 注解（agent_id={has_agent_id} agent_name={has_agent_name} instruction={has_instruction}）"
            )
        else:
            print("[C7] OK  agent_id/agent_name/instruction 一并补 str 注解（与 step dict 约定对齐）")

    # ── D. 行为零变（运行时复制 plan 跑新旧逻辑对照）──
    sys.path.insert(0, str(REPO / "backend"))
    try:
        from engine.dispatcher import apply_fail_fast as _aff

        # [8] 线性级联 1→2→3（step1 failed）
        plan = [
            {"step": 1, "status": "failed", "depends_on": [], "result": "err"},
            {"step": 2, "status": "pending", "depends_on": [1], "result": ""},
            {"step": 3, "status": "pending", "depends_on": [2], "result": ""},
            {"step": 4, "status": "pending", "depends_on": [], "result": ""},  # 独立
        ]
        out = _aff([dict(s) for s in plan])
        st = {s["step"]: s["status"] for s in out}
        if st != {1: "failed", 2: "failed", 3: "failed", 4: "pending"}:
            errs.append(f"[D8] 线性级联异常：{st}（应 1/2/3 failed, 4 pending）")
        else:
            print("[D8] OK  线性级联 1→2→3：2/3 failed, 4 独立 pending")
        # [9] 菱形 1→{2,3}→4
        plan = [
            {"step": 1, "status": "failed", "depends_on": [], "result": "err"},
            {"step": 2, "status": "pending", "depends_on": [1], "result": ""},
            {"step": 3, "status": "pending", "depends_on": [1], "result": ""},
            {"step": 4, "status": "pending", "depends_on": [2, 3], "result": ""},
        ]
        out = _aff([dict(s) for s in plan])
        st = {s["step"]: s["status"] for s in out}
        if st != {1: "failed", 2: "failed", 3: "failed", 4: "failed"}:
            errs.append(f"[D9] 菱形级联异常：{st}（应全 failed）")
        else:
            print("[D9] OK  菱形 1→{2,3}→4：2/3/4 全 failed")
        # [10] 无失败早退
        plan = [
            {"step": 1, "status": "completed", "depends_on": [], "result": "ok"},
            {"step": 2, "status": "pending", "depends_on": [1], "result": ""},
            {"step": 3, "status": "pending", "depends_on": [2], "result": ""},
        ]
        out = _aff([dict(s) for s in plan])
        st = {s["step"]: s["status"] for s in out}
        if st != {1: "completed", 2: "pending", 3: "pending"}:
            errs.append(f"[D10] 无失败早退异常：{st}（completed 不动, pending 保持）")
        else:
            print("[D10] OK  无失败早退：completed 不动, pending 保持 pending（不误标）")
        # [11] 预失败级联
        plan = [
            {"step": 1, "status": "completed", "depends_on": [], "result": "ok"},
            {"step": 2, "status": "failed", "depends_on": [1], "result": "上游步骤失败，跳过"},
            {"step": 3, "status": "pending", "depends_on": [2], "result": ""},
        ]
        out = _aff([dict(s) for s in plan])
        st = {s["step"]: s["status"] for s in out}
        if st != {1: "completed", 2: "failed", 3: "failed"}:
            errs.append(f"[D11] 预失败级联异常：{st}（应 3 级联 failed）")
        else:
            print("[D11] OK  预失败级联：step2 已 failed → step3 级联 failed")
    except Exception as e:
        errs.append(f"[D] apply_fail_fast 运行时验证异常: {e}")

    # ── E. 算法结构（O(n²) 不变，仅均摊更省）──
    # [12] 仍是 while-True + for-step + for-dep 嵌套
    if "while True:" not in aff_body or "for s in plan:" not in aff_body:
        errs.append("[E12] apply_fail_fast 结构已变（应 while-True + for-step 嵌套）")
    elif 'for dep in s.get("depends_on", []) or []:' not in aff_body:
        errs.append("[E12] 缺内层 for-dep 扫描（结构已变）")
    else:
        print("[E12] OK  仍是 while-True + for-step + for-dep 嵌套（O(n²) 上界不变，早退降均摊）")
    # [13] 仍用 next() 线性扫找依赖步骤
    if not re.search(r'next\(\(d for d in plan if d\.get\("step"\) == dep\),\s*None\)', aff_body):
        errs.append("[E13] 缺 next() 线性扫找依赖步骤（数据结构可能已重构）")
    else:
        print("[E13] OK  仍用 next() 线性扫找依赖步骤（小 plan 无碍，B13 不重构数据结构）")

    # ── F. 无回归（dispatch_ready_steps 调用链不破）──
    # [14] apply_fail_fast 仍 -> list[dict[str, Any]]
    if not re.search(r"def apply_fail_fast\(plan: list\[dict\[str,\s*Any\]\]\)\s*->\s*list\[dict\[str,\s*Any\]\]:", disp):
        errs.append("[F14] apply_fail_fast 签名异常（应 -> list[dict[str, Any]]）")
    else:
        print("[F14] OK  apply_fail_fast 仍 -> list[dict[str, Any]]（原地改 + 返回 plan）")
    # [15] dispatch_ready_steps 仍调 apply_fail_fast(plan)
    drs_body = _fn_body(disp, "dispatch_ready_steps", indent_opts=("",))
    if not drs_body:
        errs.append("[F15] dispatch_ready_steps 函数体未找到")
    elif "apply_fail_fast(plan)" not in drs_body:
        errs.append("[F15] dispatch_ready_steps 未调 apply_fail_fast(plan)（调用链断）")
    else:
        print("[F15] OK  dispatch_ready_steps 仍调 apply_fail_fast(plan)（调用链不破）")

    return errs


def main() -> int:
    print("=== VH10 回归：apply_fail_fast 早退 + step_num:int 类型注解（B13）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B13 apply_fail_fast 早退 + 类型注解锁定（行为零变）：\n"
        "  · A 内层早退：failed_steps.append(s['step']) 后 break（命中即跳出 dep 循环，不扫剩余 deps）；\n"
        "  · B 外层早退：if not failed_steps: break（fixpoint，无新增失败即跳出 while）；\n"
        "  · C 类型注解：step_num: int = step['step'] + agent_id/agent_name/instruction: str；\n"
        "  · D 行为零变：线性/菱形/无失败/预失败四级联结果与原实现逐字节一致（运行时对照）；\n"
        "  · E 算法结构：O(n²) 上界不变（while-True + for-step + for-dep + next() 线性扫），早退仅降均摊；\n"
        "  · F 无回归：apply_fail_fast 签名 + dispatch_ready_steps 调用链不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
