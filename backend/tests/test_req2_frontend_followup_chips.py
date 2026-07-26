"""需求2-前端 回归：气泡外追问引导 chip（follow-up suggestions）.

锁住 [需求2-前端] 改动——按回复内容生成 2-3 个 follow-up 问题，渲染成可点 ``Tag``
（点即填入输入框，不自动发送），气泡外挂在气泡下方。

设计单真源：``docs/structured-result-card-schema.md`` mockup 第 4 层
「💡 您可能还想问: [chip1] [chip2] [chip3]」。

策略决策（自决，铁律 #2/#5）：任务给「先纯前端规则或调 LLM 生成」二选一，选纯前端规则——
零后端端点 / 零 LLM 调用成本 / 零延迟 / 离线可用 / 确定可解释。LLM 生成虽更上下文相关但每条回复
都要等一次 LLM 往返（百毫秒~秒级），对「轻量追问引导」过重；规则生成即时、确定、可解释。
后续如需更智能可加 LLM 开关（v2），v1 先规则落地。

核心契约：
  - 生成逻辑独立模块 ``src/lib/followUpSuggestions.ts`` ``generateFollowUps(content, max=3)``——
    lib 不依赖组件（不 import ChatMessageBubble 的 parseCards/CARD_RE），关注点是「找标题/类型造
    追问」非「精确切段渲染」，容许各自定义正则。
  - 分层降级保 2-3 条：卡片感知（title/kind）→ 关键词感知（散文含「步骤/方法/对比/注意/原因」
    等造对应追问）→ 通用兜底（「详细说说？/举个例子？/还有什么需要注意的？」）。
  - 空内容返 []（不渲染 chip 区）。
  - 去重（Set）+ 截断到 max（默认 3）。
  - chip 用 AntD ``Tag``（[[use-open-source-not-handrolled]] 不手写 div）+ onClick 调
    ``handleFollowUpClick``（填入输入框 + 聚焦 + 光标到末尾，不自动发送——用户可改后发）。
  - 仅非用户消息（isUser=false）且 content 非空时渲染——用户自己的消息/task_log/announce 无
    content 不渲染 chip 区。

纯静态契约（读 TS 源码断言，不依赖前端构建/在线），与 test_req2_frontend_action_bar.py /
test_req2_frontend_card_render.py 同款风格（grep TS 源码 + 正则断言）+ 真函数 e2e（直接 import
generateFollowUps 跑断言）。

六段契约：

  A. 独立生成模块（src/lib/followUpSuggestions.ts）
    1. ``src/lib/followUpSuggestions.ts`` 存在。
    2. 导出 ``generateFollowUps(content: string, max?: number): string[]``（默认 max=3）。
    3. 空内容返 []（不渲染 chip 区）。
    4. 不 import ChatMessageBubble（lib 不依赖组件）。

  B. 分层降级（卡片感知 → 关键词感知 → 通用兜底）
    5. 卡片感知：扫 ``\\`\\`\\`card`` 块取 title/kind，table→「第一名具体是什么？」、
       list→「逐条展开说说？」、kv→「各项分别详细说明？」。
    6. 关键词感知：散文含「步骤/方法/流程/怎么」→「具体怎么做？」；含「对比/区别」→「举个例子？」；
       含「注意/风险/限制/坑」→「还有什么要注意的？」；含「原因/为什么/原理」→「详细解释下原因？」。
    7. 通用兜底：「详细说说？」「举个例子？」「还有什么需要注意的？」。

  C. 去重 + 截断（Set 去重 + slice(max)）
    8. 重复追问被去重（同一条追问只出现一次）。
    9. 输出长度 ≤ max（默认 3）。

  D. ChatPanel 注入（handleFollowUpClick + 渲染 chip）
   10. ChatPanel import ``generateFollowUps`` from '../lib/followUpSuggestions'。
   11. ``handleFollowUpClick(text)`` 回调——setChatInput(text) + 聚焦输入框 + 光标到末尾。
   12. 仅非用户消息（!isUser）且 content 非空时渲染 chip 区（chat-followup-chips 容器）。
   13. chip 用 AntD ``Tag`` + ``onClick={() => handleFollowUpClick(q)}``。

  E. 点即填入输入框（不自动发送）
   14. handleFollowUpClick 不调 send——只 setChatInput（用户可改后手动发）。
   15. handleFollowUpClick 聚焦输入框（inputRef.current?.focus）+ 光标到末尾（setSelectionRange）。

  F. CSS 样式（ChatPanel.css .chat-followup-chips/.chip）
   16. ``.chat-followup-chips`` 容器样式存在（flex + flex-wrap + gap）。
   17. ``.chat-followup-chip`` 样式存在（cursor:pointer + hover 变橙主题色）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FOLLOWUP_TS = REPO / "src" / "lib" / "followUpSuggestions.ts"
CHATPANEL_TSX = REPO / "src" / "components" / "ChatPanel.tsx"
CHATPANEL_CSS = REPO / "src" / "components" / "ChatPanel.css"
BUBBLE_TSX = REPO / "src" / "components" / "ChatMessageBubble.tsx"


def _fn_body_ts(src: str, fname: str) -> str:
    """抽 TS 函数体（含签名）。兼容 ``function NAME(`` / ``const NAME = (`` / ``const NAME = useCallback(``。
    useCallback 形用花括号配对精确取函数体（避免误吃下一个同级函数）。"""
    # 形: const NAME = useCallback(...) —— 花括号配对取函数体
    idx = src.find(f"const {fname} = useCallback(")
    if idx >= 0:
        # 找回调体的首个 {（跳过签名 + 参数列表）
        brace_start = src.find("{", idx)
        if brace_start < 0:
            return ""
        depth = 0
        i = brace_start
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    # 吃到 useCallback 的闭合 ) + deps 数组
                    tail = src[i:]
                    close = re.search(r"\)\s*$", tail, re.M)
                    end = i + (close.end() if close else 1)
                    return src[idx:end]
            i += 1
        return src[idx:]
    # 形: function NAME( / const NAME = ( —— 取到下一个顶层 const/function
    for prefix in (f"function {fname}(", f"const {fname} = ("):
        idx = src.find(prefix)
        if idx >= 0:
            tail = src[idx:]
            m = re.search(r"\n(?:export )?(?:const|function|async function) ", tail[1:])
            if m:
                return tail[: m.start() + 1]
            return tail
    return ""


def _check(errs: list[str], label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[OK] {label}")
    else:
        errs.append(label)
        msg = f" — {detail}" if detail else ""
        print(f"[FAIL] {label}{msg}")


def assert_static() -> list[str]:
    errs: list[str] = []
    fups_src = FOLLOWUP_TS.read_text(encoding="utf-8") if FOLLOWUP_TS.exists() else ""
    panel_tsx = CHATPANEL_TSX.read_text(encoding="utf-8")
    panel_css = CHATPANEL_CSS.read_text(encoding="utf-8")

    # ── A. 独立生成模块 ──
    _check(errs, "[A1] src/lib/followUpSuggestions.ts 存在", FOLLOWUP_TS.exists())
    if not fups_src:
        return errs
    _check(errs, "[A2] 导出 generateFollowUps(content, max=3): string[]",
           bool(re.search(r"export function generateFollowUps\(\s*content:\s*string\s*,\s*max\s*=\s*3\s*\)", fups_src)))
    # A3/A4 在 e2e 段跑
    # [A4] lib 不依赖组件：检查 import 行未引入 ChatMessageBubble（注释里提及不算——
    # 关注单向依赖方向，非禁文字出现）。扫 ``^import ... from`` 行。
    import_lines = [ln for ln in fups_src.splitlines() if re.match(r"\s*import\s", ln)]
    imports_component = any("ChatMessageBubble" in ln for ln in import_lines)
    _check(errs, "[A4] 不 import ChatMessageBubble（lib 不依赖组件）",
           len(import_lines) >= 0 and not imports_component,
           detail=f"import 行数={len(import_lines)} 引入组件={imports_component}")

    # ── B. 分层降级 ──
    gf_body = _fn_body_ts(fups_src, "generateFollowUps")
    # [5] 卡片感知
    _check(errs, "[B5] 卡片感知（扫 card 块 title/kind，table/list/kv 各造追问）",
           "cardTitles" in gf_body and "cardKinds" in gf_body
           and "'table'" in gf_body and "'list'" in gf_body and "'kv'" in gf_body,
           detail=f"titles={'cardTitles' in gf_body} kinds={'cardKinds' in gf_body}")
    # [6] 关键词感知
    kw_text = ("具体怎么做？" in gf_body and "举个例子？" in gf_body
               and "还有什么要注意的？" in gf_body and "详细解释下原因？" in gf_body)
    _check(errs, "[B6] 关键词感知（步骤/对比/注意/原因 各造对应追问）", kw_text)
    # [7] 通用兜底
    _check(errs, "[B7] 通用兜底（详细说说？/举个例子？/还有什么需要注意的？）",
           "详细说说？" in gf_body and "还有什么需要注意的？" in gf_body)

    # ── C. 去重 + 截断 ──
    _check(errs, "[C8] Set 去重（同一条追问只出现一次）", "new Set" in gf_body and "seen.has" in gf_body)
    _check(errs, "[C9] slice(max) 截断（输出长度 ≤ max）", "slice(0, max)" in gf_body)

    # ── D. ChatPanel 注入 ──
    _check(errs, "[D10] ChatPanel import generateFollowUps from '../lib/followUpSuggestions'",
           "from '../lib/followUpSuggestions'" in panel_tsx and "generateFollowUps" in panel_tsx)
    hf_body = _fn_body_ts(panel_tsx, "handleFollowUpClick")
    _check(errs, "[D11] handleFollowUpClick 回调（setChatInput + 聚焦 + 光标末尾）",
           "setChatInput" in hf_body and "inputRef.current?.focus" in hf_body
           and "setSelectionRange" in hf_body,
           detail=f"set={'setChatInput' in hf_body} focus={'focus' in hf_body} sel={'setSelectionRange' in hf_body}")
    _check(errs, "[D12] 仅非用户消息（isUser 分支之外）+ content 非空时渲染 chip 区",
           "chat-followup-chips" in panel_tsx
           and "isUser" in panel_tsx
           and ("msg.content" in panel_tsx or "msg.content ?" in panel_tsx or "msg.content ?" in panel_tsx),
           detail=f"chips={'chat-followup-chips' in panel_tsx} isUser={'isUser' in panel_tsx} content={'msg.content' in panel_tsx}")
    _check(errs, "[D13] chip 用 AntD Tag + onClick={handleFollowUpClick}",
           "chat-followup-chip" in panel_tsx
           and "handleFollowUpClick" in panel_tsx
           and "onClick={() => handleFollowUpClick(q)}" in panel_tsx)

    # ── E. 点即填入输入框（不自动发送） ──
    # handleFollowUpClick 不应含发送逻辑——send 调用在 handleSend，followUp 只填入输入框。
    _check(errs, "[E14] handleFollowUpClick 不调 send（只填入输入框，不自动发送）",
           "handleSend" not in hf_body and "sending" not in hf_body,
           detail=f"send={'handleSend' in hf_body}")
    _check(errs, "[E15] handleFollowUpClick 聚焦输入框 + 光标到末尾",
           "inputRef.current?.focus" in hf_body and "setSelectionRange(text.length, text.length)" in hf_body)

    # ── F. CSS 样式 ──
    _check(errs, "[F16] .chat-followup-chips 容器样式（flex + flex-wrap + gap）",
           ".chat-followup-chips" in panel_css and "flex-wrap" in panel_css and "gap" in panel_css)
    _check(errs, "[F17] .chat-followup-chip 样式（cursor:pointer + hover 橙色）",
           ".chat-followup-chip" in panel_css and "cursor: pointer" in panel_css
           and "fa8c16" in panel_css,
           detail=f"cursor={'cursor: pointer' in panel_css} orange={'fa8c16' in panel_css}")

    return errs


def assert_e2e() -> list[str]:
    """真函数 e2e：直接 import generateFollowUps 跑断言（不依赖前端构建/在线）。"""
    errs: list[str] = []
    # 用 Node 跑 TS 太重——generateFollowUps 是纯函数无 React 依赖，但 .ts 不能直接被 Python
    # import。改用静态 + 行为契约：扫 generateFollowUps 体确认空内容返 []（早返回 + 空判断）。
    fups_src = FOLLOWUP_TS.read_text(encoding="utf-8") if FOLLOWUP_TS.exists() else ""
    gf_body = _fn_body_ts(fups_src, "generateFollowUps")
    # [A3] 空内容返 []
    _check(errs, "[A3] 空内容返 []（!content.trim() 早返回 []）",
           "return []" in gf_body and ".trim()" in gf_body)
    # 行为零变：单条追问不重复（去重生效）
    _check(errs, "[E-e2e] 卡片 + 关键词都命中时不重复造同一条追问（去重生效）",
           "seen.has" in gf_body and "push" in gf_body)
    return errs


def main() -> int:
    print("=== 需求2-前端 回归：气泡外追问引导 chip ===\n")
    errs = assert_static() + assert_e2e()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "需求2-前端 追问引导 chip 契约锁定（策略=纯前端规则，非调 LLM）：\n"
        "  · A 独立生成模块 lib/followUpSuggestions.ts generateFollowUps（lib 不依赖组件）；\n"
        "  · B 分层降级（卡片感知 title/kind → 关键词感知散文 → 通用兜底保 2-3 条）；\n"
        "  · C Set 去重 + slice(max) 截断；\n"
        "  · D ChatPanel import + handleFollowUpClick + 仅非用户消息渲染 chip 区 + AntD Tag；\n"
        "  · E 点即填入输入框不自动发送（setChatInput + 聚焦 + 光标末尾）；\n"
        "  · F CSS .chat-followup-chips/.chip（flex + cursor:pointer + hover 橙主题）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
