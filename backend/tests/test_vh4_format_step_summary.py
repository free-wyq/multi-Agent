"""VH4 回归：format_step_summary 共享 helper + 魔数消除（task B7）.

锁住 B7 修复——``node_summarize`` 原内联 ``"\\n".join(...)`` 拼接 + 裸 ``[:200]``
魔数被抽到共享 ``format_step_summary(plan)`` helper，魔数收敛到模块常量
``STEP_FIELD_LIMIT``。B7 前的「重复」：summary 拼接（✅/❌ + agent + result-or-instruction）
是 ``node_summarize`` 一处内联，dispatcher 无类似拼接但 LLM 视图（_build_plan_adjust_state /
_build_step_recovery_state）用同款 ``result-or-instruction`` 截断——三处 ``[:200]`` 各自硬编码，
易漂移（一处改了别处忘改，口径不一致）。

B7 决策：抽 ``format_step_summary(plan)``（汇总拼接）+ ``_step_text(step)``（单步 result-or-instruction
截断）双 helper，魔数 ``STEP_FIELD_LIMIT=200`` 单一真源。``node_summarize`` 改调
``format_step_summary``，``_step_text`` 同时给未来 LLM 视图复用（B7 不动 LLM 视图——那是
``步骤N[label],extra`` 不同格式，只锁汇总拼接去重）。

为何不把 LLM 视图也抽共享：``_build_plan_adjust_state``/``_build_step_recovery_state`` 的
``步骤{s.get('step')}（{agent_name}）[{label}]{extra}`` 是 LLM prompt 上下文（中文 label +
状态分支 + 不同截断宽度 300/200/150），与人类可见的 ``✅ bob: result`` 汇总气泡格式不同、
受众不同、截断口径不同。强行合并会引入参数爆炸（label dict/extra 分支/宽度都不同）。B7 范围
只锁「汇总拼接去重 + 魔数收敛」，LLM 视图各自内联是合理的受众隔离（留未来任务评估是否抽
``format_plan_for_llm``）。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1/vh2/vh3 同款风格。

六段契约：

  A. format_step_summary 存在且单一真源
    1. 模块级 ``def format_step_summary(plan)`` 函数定义存在。
    2. ``node_summarize`` 调 ``format_step_summary(plan)`` 而非内联 ``"\\n".join``。
    3. ``node_summarize`` 内不再含裸 ``[:200]``（魔数已外移到 helper/常量）。

  B. STEP_FIELD_LIMIT 常量收敛魔数
    4. 模块级 ``STEP_FIELD_LIMIT = 200`` 常量定义存在（单一真源）。
    5. ``format_step_summary``/``_step_text`` 用 ``[:STEP_FIELD_LIMIT]`` 而非裸 ``[:200]``。
    6. 常量值 == 200（B7 不改行为，只去重——截断宽度仍是 200）。

  C. _step_text 单步 result-or-instruction 截断 helper
    7. ``def _step_text(step)`` 存在，返回 ``(result or instruction or "")[:STEP_FIELD_LIMIT]``。
    8. ``format_step_summary`` 调 ``_step_text(s)`` 而非各自内联 ``result-or-instruction`` 截断。

  D. 行为等价（B7 纯重构，输出不变）
    9. ``format_step_summary`` 输出格式 == 旧内联：``"\\n".join("✅|❌ {agent}: {text}")``。
   10. status emoji：completed→✅，其余（failed/dispatched/pending）→❌（与旧 ``if == 'completed'
       else '❌'`` 同口径，非按 label 分多态）。
   11. result 优先 instruction（``result or instruction``，result 为 None/空串时退 instruction）。

  E. 边界安全（不回归旧 TypeError 风险）
   12. ``_step_text`` 对 result=None+instruction=None 返回 ``""``（``or ""`` 兜底，旧内联
       ``(s.get('result') or s.get('instruction',''))[:200]`` 在 result=None+instruction=None
       时会 ``(None)[:200]`` TypeError，helper 修复此隐患——虽真实 plan 每步必有 instruction，
       但 helper 作为公共 API 该防御）。

  F. dispatcher 无重复拼接（B7 不引入跨模块依赖）
   13. ``engine/dispatcher.py`` 不含 ``✅``/``❌``/``协作结果汇总`` 拼接（汇总是 coordinator
       职责，dispatcher 只 ``🚀 步骤 N 派发`` announce，无重复——确认无跨模块重复待消除）。

为何纯静态：
  ``format_step_summary`` 是纯函数（plan → str），行为等价靠「输出格式 == 旧内联表达式」锚定。
  运行时实测需触发 all_done → node_summarize（端到端 + 真 worker report-back，重且依赖在线后端），
  而 B7 是纯结构重构（抽 helper + 常量，零行为变），静态契约锁「调用关系 + 魔数收敛 + 格式等价」
  比运行时实测更稳——MT-16 端到端测仍保「整合完整性」断言（agent_name 入汇总），本测锁结构。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD = REPO / "backend" / "engine" / "coordinator.py"
DISPATCHER = REPO / "backend" / "engine" / "dispatcher.py"


def _fn_body(src: str, fname: str) -> str:
    """抽 def/async def fname(...) 到下一个顶层 def 的函数体。"""
    m = re.search(
        rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n(?:async )?def )",
        src,
        re.S,
    )
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD.read_text(encoding="utf-8")
    disp = DISPATCHER.read_text(encoding="utf-8")

    # ── A. format_step_summary 存在且单一真源 ──
    # [1] def format_step_summary(plan) 存在
    m_def = re.search(r"^def format_step_summary\(plan:\s*list\[", coord, re.M)
    if not m_def:
        errs.append("[A1] 未找到模块级 def format_step_summary(plan: list[...]) 定义")
    else:
        print("[A1] OK  def format_step_summary(plan) 模块级定义存在")

    fs_body = _fn_body(coord, "format_step_summary")
    if not fs_body:
        errs.append("[setup] format_step_summary 函数体未找到")
        return errs

    # [2] node_summarize 调 format_step_summary(plan) 而非内联 join
    summarize_body = _fn_body(coord, "node_summarize")
    if not summarize_body:
        errs.append("[A2] node_summarize 函数体未找到")
    elif "format_step_summary(plan)" not in summarize_body and "format_step_summary(" not in summarize_body:
        errs.append("[A2] node_summarize 未调 format_step_summary(plan)（仍内联 join）")
    else:
        print("[A2] OK  node_summarize 调 format_step_summary(plan)（不再内联拼接）")

    # [3] node_summarize 内不再含裸 [:200]
    if "[:200]" in summarize_body:
        errs.append("[A3] node_summarize 仍含裸 [:200]（魔数未外移到 helper/常量）")
    else:
        print("[A3] OK  node_summarize 无裸 [:200]（魔数外移到 STEP_FIELD_LIMIT）")

    # ── B. STEP_FIELD_LIMIT 常量收敛魔数 ──
    # [4] 模块级 STEP_FIELD_LIMIT = 200 常量定义
    m_const = re.search(r"^STEP_FIELD_LIMIT\s*=\s*200\s*$", coord, re.M)
    if not m_const:
        errs.append("[B4] 未找到模块级 STEP_FIELD_LIMIT = 200 常量定义")
    else:
        print("[B4] OK  STEP_FIELD_LIMIT = 200 模块级常量（单一真源）")

    # [5] format_step_summary/_step_text 真切片用 [:STEP_FIELD_LIMIT] 而非裸 [:200]
    step_text_body = _fn_body(coord, "_step_text")
    combined = fs_body + step_text_body
    uses_const = "[:STEP_FIELD_LIMIT]" in combined
    # docstring 散文引用 `` ``[:200]`` `` （历史/对照说明）不是真切片。把三引号段
    # 整体剔除后再判（用非贪婪 ``.*?`` 跨行，含内嵌单 ``"`` 也能整段匹配——docstring
    # 不会含字面 ``"""``，故非贪婪到闭合 ``"""`` 即停）。
    stripped = re.sub(r'""".*?"""', "", combined, flags=re.S)
    real_bare_slice = "[:200]" in stripped
    if not uses_const:
        errs.append("[B5] format_step_summary/_step_text 未用 [:STEP_FIELD_LIMIT]（仍裸 [:200]）")
    elif real_bare_slice:
        errs.append("[B5] helper 真切片含 [:200]（魔数未收敛到常量，非 docstring 引用）")
    else:
        print("[B5] OK  helper 真切片用 [:STEP_FIELD_LIMIT]（裸 [:200] 仅 docstring 历史引用，非代码）")

    # [6] 常量值 == 200（B7 不改行为只去重）
    if m_const and "200" not in m_const.group(0):
        errs.append("[B6] STEP_FIELD_LIMIT 值非 200（B7 应零行为变，截断宽度不变）")
    else:
        print("[B6] OK  STEP_FIELD_LIMIT == 200（截断宽度不变，纯重构）")

    # ── C. _step_text 单步 result-or-instruction 截断 helper ──
    # [7] def _step_text(step) 存在，返回 (result or instruction or "")[:STEP_FIELD_LIMIT]
    if not step_text_body:
        errs.append("[C7] _step_text 函数体未找到")
    else:
        m_step = re.search(
            r"return\s*\(?\s*step\.get\(\s*[\"']result[\"']\s*\)\s*or\s*step\.get\(\s*[\"']instruction[\"']\s*\)\s*or\s*[\"'][\"']\s*\)?\[:STEP_FIELD_LIMIT\]",
            step_text_body,
        )
        if not m_step:
            errs.append("[C7] _step_text 未返回 (result or instruction or '')[:STEP_FIELD_LIMIT]")
        else:
            print("[C7] OK  _step_text 返回 (result or instruction or '')[:STEP_FIELD_LIMIT]")

    # [8] format_step_summary 调 _step_text(s) 而非内联 result-or-instruction
    if "_step_text(" not in fs_body:
        errs.append("[C8] format_step_summary 未调 _step_text(s)（单步截断仍内联）")
    else:
        print("[C8] OK  format_step_summary 调 _step_text(s)（单步截断单一真源）")

    # ── D. 行为等价（B7 纯重构，输出不变）──
    # [9] format_step_summary 输出格式 == 旧内联：\n.join(✅|❌ {agent}: {text})
    m_fmt = re.search(
        r'return\s*"\\n"\.join\(\s*f"\{\'✅\'\s*if\s+s\.get\([\'"]status[\'"]\)\s*==\s*[\'"]completed[\'"]\s*else\s*\'❌\'\}\s*\{s\.get\([\'"]agent_name[\'"],\s*[\'"][\'"]\)\}:\s*"\s*f"\{_step_text\(s\)\}"',
        fs_body,
        re.S,
    )
    if not m_fmt:
        # 宽松：含 ✅/❌ 三元 + agent_name + _step_text(s) + \n.join
        has_emoji_ternary = "✅" in fs_body and "❌" in fs_body and "completed" in fs_body
        has_join = '"\\n".join(' in fs_body
        has_agent = "agent_name" in fs_body
        has_step_text = "_step_text(s)" in fs_body
        if not (has_emoji_ternary and has_join and has_agent and has_step_text):
            errs.append("[D9] format_step_summary 格式与旧内联不等价（缺 ✅/❌三元/join/agent/_step_text）")
        else:
            print("[D9] OK  格式等价旧内联：\\n.join(✅|❌三元 + agent_name + _step_text)")
    else:
        print("[D9] OK  格式严格等价旧内联（✅|❌ {agent}: {text}, \\n.join）")

    # [10] status emoji：completed→✅，其余→❌（非按 label 多态）
    if "s.get('status') == 'completed'" in fs_body or 's.get("status") == "completed"' in fs_body:
        print("[D10] OK  emoji 按 status==completed 二分（✅/❌，非 label 多态）")
    else:
        errs.append("[D10] emoji 判定不符（应 status=='completed'→✅ else ❌）")

    # [11] result 优先 instruction（_step_text 内 result or instruction）
    if "result" in step_text_body and "instruction" in step_text_body and re.search(
        r"step\.get\(\s*[\"']result[\"']\s*\)\s*or\s*step\.get\(\s*[\"']instruction", step_text_body
    ):
        print("[D11] OK  result 优先 instruction（result or instruction）")
    else:
        errs.append("[D11] _step_text 非 result-or-instruction 优先序")

    # ── E. 边界安全（不回归旧 TypeError 隐患）──
    # [12] _step_text 对 result=None+instruction=None 返回 ""（or "" 兜底）
    if re.search(r"or\s*step\.get\(\s*[\"']instruction[\"']\s*\)\s*or\s*[\"'][\"']", step_text_body) or \
       re.search(r"or\s*[\"'][\"']\s*\)\s*\[:STEP_FIELD_LIMIT\]", step_text_body) or \
       'or ""' in step_text_body or "or ''" in step_text_body:
        print("[E12] OK  _step_text 有 or '' 兜底（None+None 返回 ''，修复旧 (None)[:200] TypeError 隐患）")
    else:
        errs.append("[E12] _step_text 缺 or '' 兜底（result=None+instruction=None 会 TypeError）")

    # ── F. dispatcher 无重复拼接（B7 不引入跨模块依赖）──
    # [13] dispatcher 不含 ✅/❌/协作结果汇总 拼接（汇总是 coordinator 职责）
    if "协作结果汇总" in disp or "全部完成" in disp:
        errs.append("[F13] dispatcher 含汇总拼接（与 coordinator 重复，应消除）")
    elif "✅" in disp or "❌" in disp:
        errs.append("[F13] dispatcher 含 ✅/❌ emoji（与 coordinator 汇总口径重复）")
    else:
        print("[F13] OK  dispatcher 无汇总拼接（🚀 步骤派发 announce 独立，无跨模块重复）")

    return errs


def main() -> int:
    print("=== VH4 回归：format_step_summary 共享 helper + 魔数消除 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VH4 回归契约锁定（B7 重构不退化）：\n"
        "  · A format_step_summary(plan) 模块级定义 + node_summarize 调用（不再内联 join）+ "
        "node 内无裸 [:200]；\n"
        "  · B STEP_FIELD_LIMIT=200 常量单一真源 + helper 用 [:STEP_FIELD_LIMIT]（魔数收敛，值不变）；\n"
        "  · C _step_text(step) 单步 result-or-instruction 截断 helper + format_step_summary 复用；\n"
        "  · D 行为等价：✅|❌ 三元 + agent_name + \\n.join 格式 + status==completed 二分 + "
        "result 优先 instruction（与旧内联同口径）；\n"
        "  · E _step_text 有 or '' 兜底（None+None 返回 ''，修复旧 (None)[:200] TypeError 隐患）；\n"
        "  · F dispatcher 无 ✅/❌/汇总拼接（🚀 步骤派发 announce 独立，无跨模块重复待消除）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
