"""VH9 回归：TEAM_INTERACTION_SUFFIX 单一真源消除两处互动文案重复（task B12）.

锁住 B12 修复——``engine/registry.py:678-685`` ``sys_for_invoke`` 内联拼接「团队互动」
串（与 ``llm/prompts.py`` 的互动语义文字相近但分叉）抽到 ``llm.prompts.TEAM_INTERACTION_SUFFIX``
常量，合并到一处真源。B12 同时让 ``build_brain_prompt`` 内嵌的同款语义段也引用同一常量
（f-string 插值），system 层 persona 追加（registry）+ 决策层 prompt 内嵌（prompts）两层
强化共用一段文字，改文案只改常量一处。

B12 前的重复：
  - registry ``_handle_notify`` worker 分支：``sys_for_invoke = system_prompt + "\n\n"
    + "作为团队成员，群里除了本职工作也可能有轻松的互动（成语接龙、你画我猜、多轮
    讨论）。这类互动请正常参与、配合规则，不必端着工作人设拒绝；接龙等游戏按规则
    接续即可。"`` —— system 层 persona 追加。
  - prompts ``build_brain_prompt``：内嵌「作为团队成员，群里除了本职工作也可能有轻松的
    互动（成语接龙、你画我猜、闲聊）——这类互动请正常参与、配合规则，不必端着工作人设
    拒绝。接龙等游戏按规则接上即可，不知道前一个成语时可从上下文里其他成员最近说过的
    成语接续。」—— 决策层 prompt 内嵌。
  两段文字相近但分叉（registry 用「多轮讨论 / 按规则接续即可」，prompts 用「闲聊 /
    按规则接上即可 / 不知道前一个成语时...接续」），是两份独立维护的文案副本，改一处
    另一处不同步 → 语义漂移风险。

B12 后：单一真源 ``TEAM_INTERACTION_SUFFIX``（取 prompts 版本——更完整，含「不知道前一个
成语时可从上下文里其他成员最近说过的成语接续」兜底），registry 与 build_brain_prompt
都引用它。registry 用字符串拼接（system_prompt + "\n\n" + TEAM_INTERACTION_SUFFIX），
build_brain_prompt 用 f-string ``{TEAM_INTERACTION_SUFFIX}`` 插值。两处文字一致，改文案
只改常量一处。

为何取 prompts 版本而非 registry 版本：prompts 版本语义更完整——「按规则接上即可」比
registry 的「按规则接续即可」更准（接龙是「接上」前一个成语，「接续」偏指继续行动）；
且含「不知道前一个成语时可从上下文里其他成员最近说过的成语接续」兜底（agent 上下文
读取能力），registry 版本无此条。取更完整版合并，行为只增不减。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh8 同款风格。

六段契约：

  A. TEAM_INTERACTION_SUFFIX 单一真源（llm/prompts.py）
    1. ``llm/prompts.py`` 定义 ``TEAM_INTERACTION_SUFFIX`` 常量（模块级赋值，非函数内）。
    2. 常量含「作为团队成员」+「成语接龙」+「按规则接上即可」核心语义（合并 prompts 版本
       更完整文案）。
    3. ``llm/__init__.py`` 导出 ``TEAM_INTERACTION_SUFFIX``（__all__ 含之，公共 API）。

  B. registry 引用常量（消除内联拼接）
    4. registry ``_handle_notify`` worker 分支 ``sys_for_invoke`` 用 ``TEAM_INTERACTION_SUFFIX``
       （非内联「作为团队成员...按规则接续即可」串）。
    5. registry 顶部 ``from llm import TEAM_INTERACTION_SUFFIX``（import 真源）。
    6. registry 不再含旧内联文案片段「你画我猜、多轮讨论」/「按规则接续即可」（B12 去重）。

  C. build_brain_prompt 内嵌引用常量（f-string 插值，决策层共用真源）
    7. ``build_brain_prompt`` 函数体含 ``{TEAM_INTERACTION_SUFFIX}`` f-string 插值（非硬编码
       「作为团队成员」散文副本）。
    8. build_brain_prompt 渲染输出含 TEAM_INTERACTION_SUFFIX 全文（运行时插值生效）。

  D. 行为零变（system 层追加 + 决策层内嵌两层强化保持）
    9. registry sys_for_invoke 拼接 = ``system_prompt + "\n\n" + TEAM_INTERACTION_SUFFIX``
       （单聊不加——单聊 engine coordinator_id="" → is_coordinator=False → 守卫不触发）。
   10. registry 守卫仍是「单聊 engine 不加互动语义」——Path C 后守卫改判
       ``if not self.is_coordinator and self.coordinator_id:``（单聊 engine 的
       coordinator_id="" 使守卫短路，等效于旧 ``if not self.single_chat``）。
       断言守卫文本含 is_coordinator + coordinator_id 两判定（非旧 single_chat）。

  E. 文案一致性（两处共用一段文字，改文案只改常量一处）
   11. registry 与 build_brain_prompt 引用同一常量（非两份独立文案副本）。
   12. 常量定义唯一（prompts.py 仅一处 ``TEAM_INTERACTION_SUFFIX =`` 赋值）。

  F. 无回归（A4/A5 创作归 chat / 单聊不加互动语义契约不破）
   13. build_brain_prompt 仍含「直接生成文本内容」+「不属于 execute」条款（A4 创作归 chat
       不回归——B12 只动互动语义段，不动 chat/execute 判定条款）。
   14. COORDINATOR_SYSTEM 仍含「不属于 dispatch」+「直接生成文本内容」（A5 不回归）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "backend" / "llm" / "prompts.py"
LLM_INIT = REPO / "backend" / "llm" / "__init__.py"
REGISTRY = REPO / "backend" / "engine" / "registry.py"


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


def _strip_docstrings(src: str) -> str:
    """剔三引号 docstring（防散文引用被误判为代码字面量，B6-B11 稳定坑）。"""
    return re.sub(r'""".*?"""', "", src, flags=re.S)


def assert_contract() -> list[str]:
    errs: list[str] = []
    prompts = PROMPTS.read_text(encoding="utf-8")
    init = LLM_INIT.read_text(encoding="utf-8")
    registry = REGISTRY.read_text(encoding="utf-8")

    # ── A. TEAM_INTERACTION_SUFFIX 单一真源 ──
    # [1] prompts.py 定义 TEAM_INTERACTION_SUFFIX 常量（模块级赋值）
    m_const = re.search(r"^TEAM_INTERACTION_SUFFIX\s*=\s*", prompts, re.M)
    if not m_const:
        errs.append("[A1] llm/prompts.py 未定义模块级 TEAM_INTERACTION_SUFFIX 常量（B12 未抽真源）")
        return errs
    print("[A1] OK  llm/prompts.py 定义模块级 TEAM_INTERACTION_SUFFIX 常量")
    # 抽常量赋值体（到下一个顶层 def / 赋值 / 文件尾）
    const_block_m = re.search(
        r"TEAM_INTERACTION_SUFFIX\s*=\s*\((.*?)\)\s*(?=\n\ndef |\n\n)",
        prompts,
        re.S,
    )
    const_block = const_block_m.group(1) if const_block_m else ""
    # [2] 常量含核心语义「作为团队成员」+「成语接龙」+「按规则接上即可」
    has_member = "作为团队成员" in const_block
    has_idiom = "成语接龙" in const_block
    has_rule = "按规则接上即可" in const_block
    if not (has_member and has_idiom and has_rule):
        errs.append(
            f"[A2] TEAM_INTERACTION_SUFFIX 缺核心语义（作为团队成员={has_member} 成语接龙={has_idiom} 按规则接上即可={has_rule}）"
        )
    else:
        print("[A2] OK  TEAM_INTERACTION_SUFFIX 含「作为团队成员」+「成语接龙」+「按规则接上即可」核心语义")

    # [3] llm/__init__.py 导出 TEAM_INTERACTION_SUFFIX
    if "TEAM_INTERACTION_SUFFIX" not in init:
        errs.append("[A3] llm/__init__.py 未导出 TEAM_INTERACTION_SUFFIX（非公共 API）")
    elif "TEAM_INTERACTION_SUFFIX" not in re.search(r"__all__\s*=\s*\[(.*?)\]", init, re.S).group(1):
        errs.append("[A3] llm/__init__.py __all__ 未含 TEAM_INTERACTION_SUFFIX")
    else:
        print("[A3] OK  llm/__init__.py 导出 TEAM_INTERACTION_SUFFIX（from llm import 可用）")

    # ── B. registry 引用常量（消除内联拼接）──
    notify_body = _fn_body(registry, "_handle_notify", indent_opts=("    ",))
    if not notify_body:
        errs.append("[B4] _handle_notify 函数体未找到")
    else:
        # [4] sys_for_invoke 用 TEAM_INTERACTION_SUFFIX（非内联串）
        if "TEAM_INTERACTION_SUFFIX" not in notify_body:
            errs.append("[B4] registry _handle_notify sys_for_invoke 未用 TEAM_INTERACTION_SUFFIX（B12 未接线）")
        else:
            print("[B4] OK  registry sys_for_invoke 引用 TEAM_INTERACTION_SUFFIX（消除内联拼接）")
        # [9] sys_for_invoke 拼接 = system_prompt + "\n\n" + TEAM_INTERACTION_SUFFIX
        if not re.search(
            r'sys_for_invoke\s*=\s*\(\s*\(self\.system_prompt\s+or\s+""\)\s*\+\s*"\\n\\n"\s*\+\s*TEAM_INTERACTION_SUFFIX\s*\)',
            notify_body,
            re.S,
        ):
            errs.append("[B9] sys_for_invoke 拼接结构异常（应 system_prompt + '\\n\\n' + TEAM_INTERACTION_SUFFIX）")
        else:
            print("[B9] OK  sys_for_invoke = system_prompt + '\\n\\n' + TEAM_INTERACTION_SUFFIX（system 层追加结构不变）")
        # [10] 守卫仍是「单聊 engine 不加互动语义」——Path C 后 single_chat flag 删除，
        # 守卫改判 ``if not self.is_coordinator and self.coordinator_id:``（单聊 engine
        # coordinator_id="" 使守卫短路 → 不加 suffix，等效旧 ``if not self.single_chat``）。
        # 断言守卫含 is_coordinator + coordinator_id 两判定（非旧 single_chat）。
        if not re.search(r"if\s+not\s+self\.is_coordinator\s+and\s+self\.coordinator_id\s*:", notify_body):
            errs.append("[B10] sys_for_invoke 缺 if not self.is_coordinator and self.coordinator_id 守卫（单聊 engine 不加互动语义，Path C 改判）")
        else:
            print("[B10] OK  sys_for_invoke 守卫 if not self.is_coordinator and self.coordinator_id（单聊 coordinator_id=\"\" 不加，Path C 等效旧 single_chat 守卫）")

    # [5] registry 顶部 from llm import TEAM_INTERACTION_SUFFIX
    if "from llm import" not in registry or "TEAM_INTERACTION_SUFFIX" not in registry.split("class AgentEngine")[0]:
        errs.append("[B5] registry 未 from llm import TEAM_INTERACTION_SUFFIX（import 真源缺失）")
    else:
        print("[B5] OK  registry from llm import TEAM_INTERACTION_SUFFIX（import 真源）")

    # [6] registry 不再含旧内联文案片段（剔 docstring 后判可执行代码）
    reg_code = _strip_docstrings(registry)
    old_frags = ["你画我猜、多轮讨论", "按规则接续即可", "不必端着工作人设\n拒绝；"]
    leftover = [f for f in old_frags if f in reg_code]
    if leftover:
        errs.append(f"[B6] registry 仍含旧内联文案片段 {leftover}（B12 未去重）")
    else:
        print("[B6] OK  registry 不再含旧内联「你画我猜、多轮讨论」/「按规则接续即可」片段（去重完成）")

    # ── C. build_brain_prompt 内嵌引用常量（f-string 插值）──
    bp_body = _fn_body(prompts, "build_brain_prompt", indent_opts=("",))
    if not bp_body:
        errs.append("[C7] build_brain_prompt 函数体未找到")
    else:
        # [7] 函数体含 {TEAM_INTERACTION_SUFFIX} f-string 插值
        if "{TEAM_INTERACTION_SUFFIX}" not in bp_body:
            errs.append("[C7] build_brain_prompt 未用 {TEAM_INTERACTION_SUFFIX} f-string 插值（仍硬编码散文副本）")
        else:
            print("[C7] OK  build_brain_prompt 用 {TEAM_INTERACTION_SUFFIX} f-string 插值（决策层共用真源）")
        # [8] 渲染输出含 TEAM_INTERACTION_SUFFIX 全文（运行时插值生效）
        #     需把 backend/ 加 sys.path 才能 import llm.prompts（v* 测试纯静态，
        #     此处为运行时插值验证临时加 path，与纯静态契约主体隔离）。
        try:
            sys.path.insert(0, str(REPO / "backend"))
            from llm.prompts import build_brain_prompt, TEAM_INTERACTION_SUFFIX
            rendered = build_brain_prompt("backend_engineer", "后端工程师", "[ctx]", "msg")
            if TEAM_INTERACTION_SUFFIX not in rendered:
                errs.append("[C8] build_brain_prompt 渲染输出未含 TEAM_INTERACTION_SUFFIX 全文（插值未生效）")
            else:
                print("[C8] OK  build_brain_prompt 渲染输出含 TEAM_INTERACTION_SUFFIX 全文（运行时插值生效）")
        except Exception as e:
            errs.append(f"[C8] build_brain_prompt 运行时渲染验证异常: {e}")

    # ── D. 行为零变（[B9]/[B10] 已锁 system 层追加结构 + 单聊守卫）──
    print("[D] OK  行为零变（B9 拼接结构 + B10 单聊守卫已锁，system 层追加语义不变）")

    # ── E. 文案一致性 ──
    # [11] registry 与 build_brain_prompt 引用同一常量（非两份独立副本）
    reg_uses = "TEAM_INTERACTION_SUFFIX" in reg_code
    bp_uses = "{TEAM_INTERACTION_SUFFIX}" in bp_body
    if not (reg_uses and bp_uses):
        errs.append(f"[E11] 两处未都引用 TEAM_INTERACTION_SUFFIX（registry={reg_uses} build_brain_prompt={bp_uses}）")
    else:
        print("[E11] OK  registry + build_brain_prompt 都引用 TEAM_INTERACTION_SUFFIX（共用真源，非两份副本）")
    # [12] 常量定义唯一（prompts.py 仅一处 TEAM_INTERACTION_SUFFIX = 赋值）
    const_defs = re.findall(r"^TEAM_INTERACTION_SUFFIX\s*=", prompts, re.M)
    if len(const_defs) != 1:
        errs.append(f"[E12] prompts.py 有 {len(const_defs)} 处 TEAM_INTERACTION_SUFFIX 赋值（应唯一 1 处）")
    else:
        print("[E12] OK  prompts.py 仅 1 处 TEAM_INTERACTION_SUFFIX 赋值（常量定义唯一）")

    # ── F. 无回归（A4/A5 创作归 chat / 单聊不加互动语义契约不破）──
    # [13] build_brain_prompt 仍含「直接生成文本内容」+「不属于 execute」（A4 创作归 chat）
    if "直接生成文本内容" not in prompts:
        errs.append("[F13] build_brain_prompt 缺「直接生成文本内容」（A4 创作归 chat 回归）")
    elif "不属于 execute" not in prompts:
        errs.append("[F13] build_brain_prompt 缺「不属于 execute」反向提醒（A4 回归）")
    else:
        print("[F13] OK  build_brain_prompt 仍含「直接生成文本内容」+「不属于 execute」（A4 创作归 chat 不回归）")
    # [14] COORDINATOR_SYSTEM 仍含「不属于 dispatch」+「直接生成文本内容」（A5）
    coord_sys_m = re.search(r'COORDINATOR_SYSTEM\s*=\s*r"""(.*?)"""', prompts, re.S)
    coord_sys = coord_sys_m.group(1) if coord_sys_m else ""
    if "不属于 dispatch" not in coord_sys:
        errs.append("[F14] COORDINATOR_SYSTEM 缺「不属于 dispatch」（A5 创作归 chat 不归 dispatch 回归）")
    elif "直接生成文本内容" not in coord_sys:
        errs.append("[F14] COORDINATOR_SYSTEM 缺「直接生成文本内容」（A5 回归）")
    else:
        print("[F14] OK  COORDINATOR_SYSTEM 仍含「直接生成文本内容」+「不属于 dispatch」（A5 不回归）")

    return errs


def main() -> int:
    print("=== VH9 回归：TEAM_INTERACTION_SUFFIX 单一真源消除互动文案重复（B12）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B12 互动语义单一真源锁定：\n"
        "  · A TEAM_INTERACTION_SUFFIX 单一真源（llm/prompts.py 模块级常量 + __init__ 导出）；\n"
        "  · B registry _handle_notify sys_for_invoke 引用常量（消除内联拼接 + 顶部 import 真源 + 旧文案片段去重）；\n"
        "  · C build_brain_prompt 用 {TEAM_INTERACTION_SUFFIX} f-string 插值（决策层共用真源，渲染输出含全文）；\n"
        "  · D 行为零变：sys_for_invoke = system_prompt + '\\n\\n' + 常量（system 层追加结构不变）+ 仍 if not single_chat 守卫（单聊不加）；\n"
        "  · E 文案一致性：registry + build_brain_prompt 都引用同一常量 + 常量定义唯一（改文案只改一处）；\n"
        "  · F 无回归：A4 创作归 chat（直接生成文本内容/不属于 execute）+ A5 不归 dispatch 契约不破。\n"
        "  取 prompts 版本合并（更完整：按规则接上即可 + 不知道前一个成语时从上下文接续兜底），行为只增不减。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
