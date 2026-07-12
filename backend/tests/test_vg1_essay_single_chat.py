"""VG1 回归：作文请求走单聊 chat + 气泡白名单不漏 trace 事件（task A7）.

锁住 A4/A5 的 prompt 根因修复 + ChatPanel 气泡白名单契约——防「写 200 字作文」3 气泡
回归。纯静态契约（读源码断言，不依赖后端在线），与 test_va2/va3/va6 同款风格。

两段契约：

  A. prompt 根因（A4/A5）：创作类归 chat 不归 execute/dispatch
    1. build_brain_prompt chat 条款含「直接生成文本内容」枚举（写文章/写文案/写总结/
       翻译/改写/润色 + 创作作文/诗歌/故事/邮件/报告）。
    2. build_brain_prompt execute 条款收紧为「写代码/改配置/运行命令/调用工具」+
       反向提醒「单纯生成文字内容不属于 execute」（写作文/翻译/润色/写总结显式排除）。
    3. COORDINATOR_SYSTEM 规则段含「直接生成文本内容归 chat」+「不要拆成 dispatch 计划」。
    4. COORDINATOR_SYSTEM dispatch 条款收紧 + 反向提醒「写文章/翻译/润色不属于 dispatch」。
    5. build_coordinator_prompt action 说明 chat 条款含创作枚举 + dispatch 反向提醒。

  B. 气泡白名单（防 task_dispatch/task_complete 漏成气泡）：
    6. ChatPanel CHAT_MESSAGE_TYPES 恰为 {agent_reply, user_input, task_log, slash_card}
       ——task_dispatch/task_complete/task_failed/task_token/task_think/task_tool/
       coordinator_think/coordinator_plan/coordinator_reasoning/coordinator_stats 等
       trace 事件不进白名单（不桥接成独立气泡）。
    7. 白名单不含 task_complete/task_dispatch（这两个是收尾/派发信号，桥接成气泡会
       与随后的持久化 agent_reply 重复，即「3 气泡」缺陷的根因之一）。
    8. 白名单仍是 4 项（不多不少——防误删 agent_reply/user_input/task_log/slash_card
       任一项，也防误加 task_dispatch/task_complete）。

为何纯静态：
  prompt 根因 + 气泡白名单都是「代码文本/结构」契约，运行时 LLM 决策有随机性
  （A1 实测当前 LLM 已判 chat，但根因靠 prompt 锚定，非靠 LLM 运气）。静态契约
  锁住「prompt 明确归 chat」+「白名单不含 trace 事件」两个确定性条件，比运行时
  LLM 判定更可靠。运行时覆盖由 test_va1 复现脚本（四组实测全 chat）承担。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "backend" / "llm" / "prompts.py"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"

# 创作类关键词（A4/A5 chat 条款应含的枚举）
CREATION_KEYWORDS = ["写文章", "写文案", "写总结", "翻译", "改写", "润色"]
# execute/dispatch 该含的工程关键词
ENGINEERING_KEYWORDS = ["写代码", "改配置", "运行命令", "调用工具"]
# 白名单应恰为这 4 项
EXPECTED_WHITELIST = {"agent_reply", "user_input", "task_log", "slash_card"}
# 不该进白名单的 trace 事件（漏成气泡会重复/噪音）
EXCLUDED_TRACE_TYPES = [
    "task_dispatch", "task_complete", "task_failed",
    "task_token", "task_think", "task_tool",
    "coordinator_think", "coordinator_plan", "coordinator_reasoning", "coordinator_stats",
]


def _build_brain_prompt_text() -> str:
    """调 build_brain_prompt 取实际 prompt 文本（验证 A4 落地，非 grep 源码字面）。"""
    import sys
    sys.path.insert(0, str(REPO / "backend"))
    from llm.prompts import build_brain_prompt
    return build_brain_prompt(
        "backend_engineer", "后端工程师", "(无)",
        "请帮我写一篇 200 字作文，题目是《晨光里的公园》",
    )


def _build_coordinator_prompt_text() -> str:
    """调 build_coordinator_prompt 取实际 prompt 文本（验证 A5 落地）。"""
    import sys
    sys.path.insert(0, str(REPO / "backend"))
    from llm.prompts import build_coordinator_prompt, COORDINATOR_SYSTEM
    prompt = build_coordinator_prompt(
        "协调者",
        [("agent_backend_1", "后端工程师", "backend_engineer")],
        "(无)", "（空闲，无进行中的调度）",
        "user", "请帮我写一篇 200 字作文，题目是《晨光里的公园》",
    )
    return COORDINATOR_SYSTEM + "\n" + prompt


def assert_contract() -> list[str]:
    errs: list[str] = []

    # ── A. prompt 根因（A4/A5）──
    brain = _build_brain_prompt_text()
    coord_full = _build_coordinator_prompt_text()
    prompts_src = PROMPTS.read_text(encoding="utf-8")

    # [1] build_brain_prompt chat 条款含创作枚举
    missing_chat = [kw for kw in CREATION_KEYWORDS if kw not in brain]
    if missing_chat:
        errs.append(f"[A1] build_brain_prompt chat 条款缺创作关键词 {missing_chat}")
    elif "直接生成文本内容" not in brain:
        errs.append("[A1] build_brain_prompt 缺「直接生成文本内容」总括（chat 条款未锚定创作）")
    else:
        print(f"[A1] OK  build_brain_prompt chat 条款含创作枚举 +「直接生成文本内容」")

    # [2] build_brain_prompt execute 收紧 + 反向提醒
    missing_eng = [kw for kw in ENGINEERING_KEYWORDS if kw not in brain]
    if missing_eng:
        errs.append(f"[A2] build_brain_prompt execute 条款缺工程关键词 {missing_eng}")
    elif "单纯生成文字" not in brain or "不属于 execute" not in brain:
        errs.append("[A2] build_brain_prompt execute 缺反向提醒「单纯生成文字不属于 execute」")
    else:
        print(f"[A2] OK  build_brain_prompt execute 收紧为工程任务 + 反向提醒创作不属于 execute")

    # [3] COORDINATOR_SYSTEM 规则段含「直接生成文本内容归 chat」+「不要拆成 dispatch」
    if "直接生成文本内容" not in coord_full:
        errs.append("[A3] COORDINATOR_SYSTEM 缺「直接生成文本内容」条款（A5 未落地）")
    elif "不要拆成 dispatch" not in coord_full:
        errs.append("[A3] COORDINATOR_SYSTEM 缺「不要拆成 dispatch」（创作归 chat 不拆计划）")
    else:
        print("[A3] OK  COORDINATOR_SYSTEM 含「直接生成文本内容归 chat + 不要拆成 dispatch」")

    # [4] COORDINATOR_SYSTEM dispatch 收紧 + 反向提醒
    if "不属于 dispatch" not in coord_full:
        errs.append("[A4] COORDINATOR_SYSTEM 缺「不属于 dispatch」反向提醒")
    elif "单纯生成文字" not in coord_full:
        errs.append("[A4] COORDINATOR_SYSTEM 缺「单纯生成文字」创作排除措辞")
    else:
        print("[A4] OK  COORDINATOR_SYSTEM dispatch 收紧 + 反向提醒创作不属于 dispatch")

    # [5] build_coordinator_prompt action 说明 chat 含创作 + dispatch 反向提醒
    # action 说明在 build_coordinator_prompt 返回里（coord_full 后半段）
    if "都走 chat" not in coord_full and "仍归 chat" not in coord_full:
        errs.append("[A5] build_coordinator_prompt action 说明 chat 条款未锚定创作归 chat")
    elif "不属于 dispatch" not in coord_full:
        errs.append("[A5] build_coordinator_prompt action 说明 dispatch 缺反向提醒")
    else:
        print("[A5] OK  build_coordinator_prompt action 说明 chat 含创作 + dispatch 反向提醒")

    # ── B. 气泡白名单（防 trace 事件漏成气泡）──
    panel = PANEL.read_text(encoding="utf-8")

    # [6] CHAT_MESSAGE_TYPES 恰为 4 项白名单
    m = re.search(r"const CHAT_MESSAGE_TYPES = new Set\(\[(.*?)\]\)", panel, re.S)
    if not m:
        errs.append("[B6] 未找到 CHAT_MESSAGE_TYPES 定义")
    else:
        # 抽出所有 'xxx' 字符串字面量
        types = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        if types != EXPECTED_WHITELIST:
            extra = types - EXPECTED_WHITELIST
            missing = EXPECTED_WHITELIST - types
            detail = []
            if missing:
                detail.append(f"缺 {missing}")
            if extra:
                detail.append(f"多 {extra}")
            errs.append(f"[B6] CHAT_MESSAGE_TYPES != 期望白名单：{', '.join(detail)}（实际 {types}）")
        else:
            print(f"[B6] OK  CHAT_MESSAGE_TYPES 恰为 4 项白名单：{sorted(types)}")

    # [7] 白名单不含 task_complete/task_dispatch（防 3 气泡重复）
    if m:
        whitelist_str = m.group(1)
        leaked = [t for t in ("task_complete", "task_dispatch", "task_failed") if f"'{t}'" in whitelist_str]
        if leaked:
            errs.append(f"[B7] CHAT_MESSAGE_TYPES 误含 {leaked}（收尾/派发信号不该成气泡——会与 agent_reply 重复即 3 气泡）")
        else:
            print(f"[B7] OK  白名单不含 task_complete/task_dispatch/task_failed（不漏成气泡）")

    # [8] 白名单排除全部 trace 事件类型（防 coordinator_think 等漏成气泡）
    if m:
        leaked_trace = [t for t in EXCLUDED_TRACE_TYPES if f"'{t}'" in whitelist_str]
        if leaked_trace:
            errs.append(f"[B8] CHAT_MESSAGE_TYPES 误含 trace 事件 {leaked_trace}（trace 不该桥接成独立气泡）")
        else:
            print(f"[B8] OK  白名单排除全部 {len(EXCLUDED_TRACE_TYPES)} 类 trace 事件（不漏成独立气泡）")

    return errs


def main() -> int:
    print("=== VG1 回归：作文走单聊 chat + 气泡白名单不漏 trace ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VG1 回归契约锁定：\n"
        "  · A4 build_brain_prompt 创作归 chat（写文章/写文案/写总结/翻译/改写/润色 + 创作作文诗歌故事邮件报告）"
        " + execute 收紧为工程任务 + 反向提醒创作不属于 execute；\n"
        "  · A5 COORDINATOR_SYSTEM + build_coordinator_prompt action 说明 创作归 chat 不归 dispatch"
        "（直接生成文本内容 → chat，不要拆 dispatch；dispatch 反向提醒创作不属于）；\n"
        "  · B 气泡白名单恰为 {agent_reply, user_input, task_log, slash_card}，"
        "task_dispatch/task_complete/task_failed + 8 类 trace 事件不漏成独立气泡"
        "（防「协调者回复两次/3 气泡」缺陷回归）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
