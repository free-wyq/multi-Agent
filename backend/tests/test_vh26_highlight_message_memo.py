"""VH26 回归：持久化气泡复用 ChatMessageBubble + memberNames 稳定集（B29 + 任务4 演进）.

历史：原 B29 优化对象是 ``src/components/ChatPanel.tsx`` 内的 ``HighlightMessage`` 组件——
``memo`` 包裹 + ``memberNames`` 稳定集两道防线降流式期高频重渲染。后来 commit 55c6eca 把
持久化非用户气泡**复用 ChatMessageBubble**（消除两份渲染逻辑分叉），``HighlightMessage``
组件随之删除。本测试随之演进——锁「持久化气泡复用 ChatMessageBubble」这一现状（任务4 把
该复用正式确认为 card 渲染 gap 修复的落地方案），并保留 memberNames 稳定集契约（逻辑迁
到 ChatPanel flatMap 内联 + ChatMessageBubble props 浅比较）。

为何演进而非删除：B29 优化的「历史气泡重渲染短路」目标未变——只是实现载体从独立的
``HighlightMessage = memo(...)`` 组件，变成「ChatPanel flatMap 内联渲染 ChatMessageBubble」。
持久化气泡的 ``content`` 落盘不变（除非编辑——本项目无编辑），``ChatMessageBubble`` 内
``contentSegments = useMemo([content])`` / ``toolRows = useMemo([toolEvents])`` 等 useMemo
对 content 不变的历史气泡天然短路重算（deps 浅比较命中即跳过）——与原 memo 同效。

四段契约：

  A. 持久化气泡复用 ChatMessageBubble（任务4 落地，原 HighlightMessage 已删）
    1. ChatPanel.tsx 不再有 ``const HighlightMessage = memo(function HighlightMessage(``（旧组件已删）。
    2. ChatPanel.tsx 不再有 ``<HighlightMessage`` 调用点（旧调用已换 ChatMessageBubble）。
    3. ChatPanel.tsx chatMessages.flatMap 内非用户气泡渲染 ``<ChatMessageBubble``（复用确认）。

  B. memberNames 稳定集（B29 逻辑迁移到 flatMap 内联，O(M).some → O(1).has）
    4. ChatPanel.tsx 含 ``const memberNames = new Set<string>()``（投影 members 成稳定集）。
    5. Set 含 agent_name + alias（去空——``if (m.agent_name)`` / ``if (m.alias)`` 守卫）。
    6. mention 高亮走 ``renderMarkdownWithMentions(c, memberNames)``（memberNames 注入渲染）。

  C. @mention 切分正则不变（B21 锁的 mention 分割正则，迁移到 renderMarkdown.tsx 后仍 byte-identical）
    7. ``renderMarkdownWithMentions`` 内 split 正则仍是 ``/(@[^\\s,，.。!！?？:：;；\\n]+)/g``。

  D. 行为零变 + 无残留旧组件
    8. 全仓 src/ 不再有 ``HighlightMessage`` 标识符（组件定义 + 调用 + 注释提及全清——任务4 收尾）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHATPANEL_TSX = REPO / "src" / "components" / "ChatPanel.tsx"
RENDERMARKDOWN_TSX = REPO / "src" / "lib" / "renderMarkdown.tsx"
SRC_DIR = REPO / "src"


def _strip_ts_comments(src: str) -> str:
    """剔 // 单行注释（保留字符串字面量内的 //——粗剔够用于契约断言）。"""
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def assert_contract() -> list[str]:
    errs: list[str] = []
    panel = CHATPANEL_TSX.read_text(encoding="utf-8") if CHATPANEL_TSX.exists() else ""
    panel_nc = _strip_ts_comments(panel)

    # ── A. 持久化气泡复用 ChatMessageBubble（任务4 落地）──
    # [1] 旧 HighlightMessage memo 组件已删
    if re.search(r"const HighlightMessage = memo\(\s*function HighlightMessage\(", panel):
        errs.append("[A1] ChatPanel 仍含 const HighlightMessage = memo(function ...)（旧组件应已删，持久化气泡复用 ChatMessageBubble）")
    else:
        print("[A1] OK  旧 HighlightMessage memo 组件已删（持久化气泡复用 ChatMessageBubble）")
    # [2] 旧 <HighlightMessage 调用点已换
    if "<HighlightMessage" in panel_nc:
        errs.append("[A2] ChatPanel 仍含 <HighlightMessage 调用点（应换 <ChatMessageBubble）")
    else:
        print("[A2] OK  无 <HighlightMessage 调用点（已换 <ChatMessageBubble）")
    # [3] chatMessages.flatMap 内非用户气泡渲染 <ChatMessageBubble（复用确认）
    flatmap_idx = panel.find("chatMessages.flatMap(")
    cmb_idx = panel.find("<ChatMessageBubble", flatmap_idx if flatmap_idx >= 0 else 0)
    if flatmap_idx >= 0 and cmb_idx > flatmap_idx:
        print("[A3] OK  chatMessages.flatMap 内非用户气泡渲染 <ChatMessageBubble（复用确认）")
    else:
        errs.append(f"[A3] chatMessages.flatMap 内未渲染 <ChatMessageBubble（复用未落地）flatmap={flatmap_idx} cmb={cmb_idx}")

    # ── B. memberNames 稳定集（B29 逻辑迁移到 flatMap 内联）──
    # [4] const memberNames = new Set<string>()
    if not re.search(r"const memberNames = new Set<string>\(\)", panel):
        errs.append("[B4] ChatPanel 缺 const memberNames = new Set<string>()（无稳定集）")
    else:
        print("[B4] OK  const memberNames = new Set<string>() 把 members 投影成稳定集")
    # [5] Set 含 agent_name + alias（去空守卫）
    if re.search(r"if \(m\.agent_name\) memberNames\.add\(m\.agent_name\)", panel) and re.search(r"if \(m\.alias\) memberNames\.add\(m\.alias\)", panel):
        print("[B5] OK  Set 含 agent_name + alias（if 守卫去空）")
    else:
        errs.append("[B5] memberNames 投影缺 agent_name + alias 去空守卫")
    # [6] mention 高亮走 renderMarkdownWithMentions(c, memberNames)
    if "renderMarkdownWithMentions(c, memberNames)" in panel or re.search(r"renderMarkdownWithMentions\([^,]+,\s*memberNames\)", panel):
        print("[B6] OK  mention 高亮走 renderMarkdownWithMentions(c, memberNames)（memberNames 注入渲染）")
    else:
        errs.append("[B6] ChatPanel 未用 renderMarkdownWithMentions(c, memberNames)（mention 高亮未注入稳定集）")

    # ── C. @mention 切分正则不变（B21 锁，迁移到 renderMarkdown.tsx 后仍 byte-identical）──
    rm = RENDERMARKDOWN_TSX.read_text(encoding="utf-8") if RENDERMARKDOWN_TSX.exists() else ""
    # JS 源里正则字面量是 /(@[^\s,，.。!！?？:：;；\n]+)/g；读进 Python 字符串后反斜杠是单字符。
    split_needle = "text.split(/(@[^\\s,，.。!！?？:：;；\\n]+)/g)"
    if split_needle not in rm:
        errs.append("[C7] renderMarkdown.tsx split 正则变（B21 锁的 mention 分割破）")
    else:
        print("[C7] OK  split 正则不变（B21 锁的 @mention 分割，迁移到 renderMarkdown.tsx 后仍 byte-identical）")

    # ── D. 无残留旧组件标识符（任务4 收尾：注释提及也清）──
    # 全仓 src/ 不再有 HighlightMessage 标识符（组件定义 + 调用 + 注释提及全清）
    leftover: list[str] = []
    for p in SRC_DIR.rglob("*.tsx"):
        s = p.read_text(encoding="utf-8")
        if "HighlightMessage" in s:
            leftover.append(str(p.relative_to(REPO)))
    if leftover:
        errs.append(f"[D8] src/ 仍有 HighlightMessage 残留（注释提及未清）: {leftover}")
    else:
        print("[D8] OK  src/ 无 HighlightMessage 残留（组件 + 调用 + 注释全清）")

    return errs


def main() -> int:
    print("=== VH26 回归：持久化气泡复用 ChatMessageBubble + memberNames 稳定集（B29 + 任务4 演进）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "持久化气泡复用 ChatMessageBubble 锁定（任务4）：\n"
        "  · A 旧 HighlightMessage memo 组件已删 + <ChatMessageBubble 复用确认（chatMessages.flatMap 内）；\n"
        "  · B memberNames 稳定集（Set<string> agent_name+alias 去空，renderMarkdownWithMentions 注入）；\n"
        "  · C @mention split 正则不变（迁移到 renderMarkdown.tsx 后 byte-identical）；\n"
        "  · D src/ 无 HighlightMessage 残留（组件 + 调用 + 注释全清）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
