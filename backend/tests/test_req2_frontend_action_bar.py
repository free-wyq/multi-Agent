"""需求2-前端 回归：ChatMessageBubble Bubble.footer 槽位操作栏（复制 + 重新生成）.

锁住 [需求2-前端] 改动——``src/components/ChatMessageBubble.tsx`` 在 ``Bubble.footer`` 槽位加
操作栏：复用 ``BubbleCopyButton`` 复制 + 新增「重新生成」AntD ``Button``（ReloadOutlined）。

设计单真源：``docs/structured-result-card-schema.md`` mockup 第 3 层「[操作栏] 📋复制内容 🔄重新生成」。
后端 regenerate 端点（[需求2-后端] line 24）待评估——本任务只做前端操作栏 UI，按钮 disabled 态兜底
（onRegenerate 未注入 → disabled + tooltip「开发中」），不依赖后端就绪。

核心契约：
  - 操作栏位置=``Bubble.footer`` 槽位（非 hover 浮动组 .bubble-action-group，footer 常驻可见）。
  - 复制复用 ``BubbleCopyButton``（import 同组件，不重写复制逻辑——开源自复用 [[use-open-source-not-handrolled]]）。
  - 「重新生成」=AntD ``Button``（type=text + ReloadOutlined），点击调 ``onRegenerate``。
  - ``onRegenerate`` 是 optional prop——后端 regenerate 端点就绪后 ChatPanel 注入；未注入时按钮
    ``disabled`` + tooltip「重新生成（开发中）」（始终渲染按钮本体满足「新增重新生成 Button」契约，
    不留空响应占位）。
  - 显隐守卫：流式中（isStreaming=true）不显示（内容还在变）；用户气泡（isUser=true）不显示
    （用户消息不需重生成）。
  - 与产物下载卡共存：footer 同时承载 artifact 块 + 操作栏（先产物后操作栏竖排），不互斥。

纯静态契约（读 TS 源码断言，不依赖前端构建/在线），与 test_req2_frontend_card_render.py / test_vh26
同款风格（grep TS 源码 + 正则断言）。

六段契约：

  A. footer 槽位承载操作栏（非 hover 组）
    1. ``footer={`` prop 仍在（Bubble.footer 槽位）。
    2. footer 内含 ``chat-action-bar`` 容器（操作栏外层，区别于 chat-artifact-block 产物块）。

  B. 复制复用 BubbleCopyButton（开源自复用）
    3. import ``BubbleCopyButton`` from './BubbleCopyButton'（复用现成组件，不重写复制逻辑）。
    4. 操作栏内渲染 ``<BubbleCopyButton content={content} />``（与 ChatPanel hover 组同款复制）。

  C. 「重新生成」AntD Button
    5. import ``ReloadOutlined`` from '@ant-design/icons'（重新生成图标）。
    6. 操作栏内渲染 ``<Button`` + ``ReloadOutlined``（type=text + size=small）。
    7. Button ``onClick={onRegenerate}``（点击调注入的回调）。

  D. onRegenerate optional prop + disabled 兜底
    8. ChatMessageBubbleProps 新增 ``onRegenerate?: () => void``（optional）。
    9. 解构 props 含 ``onRegenerate``。
   10. Button ``disabled`` 守卫含 ``!onRegenerate``（未注入 → disabled，不留空响应占位）；
       [需求2-后端] 落地 regenerate 端点后追加 ``regenerating`` loading 态——disabled 条件
       兼容 ``!onRegenerate || regenerating`` 形态（regenerating=true 重跑中也禁用防连点）。
   11. Button tooltip 按 onRegenerate 有无区分（「重新生成」/「重新生成（开发中）」）。

  E. 显隐守卫（流式/用户气泡不显示操作栏）
   12. 操作栏渲染条件含 ``!isStreaming``（流式中不显示）。
   13. 操作栏渲染条件含 ``!isUser``（用户气泡不显示）。

  F. 与产物下载卡共存（footer 同时承载两区）
   14. footer 同时含 chat-artifact-block（产物块）+ chat-action-bar（操作栏），不互斥。
   15. CSS ``.chat-action-bar`` 样式存在（操作栏视觉，区别于产物块）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUBBLE_TSX = REPO / "src" / "components" / "ChatMessageBubble.tsx"
BUBBLE_CSS = REPO / "src" / "components" / "ChatMessageBubble.css"


def _strip_ts_comments(src: str) -> str:
    """剔 // 单行注释（粗剔，契约断言不依赖字符串内 // 精度）。"""
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _brace_slice(src: str, bi: int) -> str:
    """从 src[bi]=='{' 起，跨花括号配对到匹配的 `}`，返回含两端花括号切片。"""
    depth = 0
    i = bi
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[bi : i + 1]
        i += 1
    return src[bi:] if bi < len(src) else ""


def _footer_body_ts(src: str) -> str:
    """抽 Bubble 的 footer={...} prop 值（JSX 表达式：三元/<>片段）。
    从 `footer={` 起跨花括号配对到匹配的 `}`，返回含两端花括号切片。"""
    m = re.search(r"footer\s*=\s*\{", src)
    if not m:
        return ""
    # 花括号起点 = m.end()-1（m 末尾是 `{`）
    bi = m.end() - 1
    return _brace_slice(src, bi)


def assert_contract() -> list[str]:
    errs: list[str] = []
    if not BUBBLE_TSX.exists():
        errs.append("[setup] src/components/ChatMessageBubble.tsx 不存在")
        return errs
    src = BUBBLE_TSX.read_text(encoding="utf-8")
    src_nc = _strip_ts_comments(src)
    css = BUBBLE_CSS.read_text(encoding="utf-8") if BUBBLE_CSS.exists() else ""

    footer_body = _footer_body_ts(src)

    # ── A. footer 槽位承载操作栏 ──
    # [1] footer={ prop 仍在
    if not re.search(r"footer\s*=\s*\{", src):
        errs.append("[A1] ChatMessageBubble 缺 footer={ prop（Bubble.footer 槽位）")
    else:
        print("[A1] OK  footer={ prop 存在（Bubble.footer 槽位）")
    # [2] footer 内含 chat-action-bar 容器
    if not footer_body:
        errs.append("[A2] footer prop 体未抽到（无法校验操作栏容器）")
    elif "chat-action-bar" not in footer_body:
        errs.append("[A2] footer 内缺 chat-action-bar 容器（操作栏外层）")
    else:
        print("[A2] OK  footer 内含 chat-action-bar 容器（区别于产物块）")

    # ── B. 复制复用 BubbleCopyButton ──
    # [3] import BubbleCopyButton from './BubbleCopyButton'
    if not re.search(r"import\s+BubbleCopyButton\s+from\s+['\"][^'\"]*BubbleCopyButton['\"]", src):
        errs.append("[B3] ChatMessageBubble 未 import BubbleCopyButton（应复用现成复制组件）")
    else:
        print("[B3] OK  import BubbleCopyButton（复用现成组件，不重写复制逻辑）")
    # [4] 操作栏内渲染 <BubbleCopyButton content={content} />
    if not footer_body:
        errs.append("[B4] footer prop 体未抽到（无法校验 BubbleCopyButton 渲染）")
    elif "<BubbleCopyButton" not in footer_body:
        errs.append("[B4] footer 操作栏内未渲染 <BubbleCopyButton>")
    elif "content={content}" not in footer_body and "content={content }" not in footer_body:
        errs.append("[B4] BubbleCopyButton 未传 content={content}")
    else:
        print("[B4] OK  操作栏内 <BubbleCopyButton content={content} />")

    # ── C. 「重新生成」AntD Button ──
    # [5] import ReloadOutlined
    if "ReloadOutlined" not in src:
        errs.append("[C5] ChatMessageBubble 未 import ReloadOutlined（重新生成图标）")
    else:
        print("[C5] OK  import ReloadOutlined（重新生成图标）")
    # [6] 操作栏内 <Button + ReloadOutlined
    if not footer_body:
        errs.append("[C6] footer prop 体未抽到（无法校验重新生成 Button）")
    elif "<Button" not in footer_body:
        errs.append("[C6] footer 操作栏内未渲染 <Button>（重新生成）")
    elif "ReloadOutlined" not in footer_body:
        errs.append("[C6] footer Button 未用 ReloadOutlined 图标")
    else:
        print("[C6] OK  操作栏内 <Button ... icon={<ReloadOutlined />}（type=text 重新生成）")
    # [7] Button onClick={onRegenerate}
    if not footer_body:
        errs.append("[C7] footer prop 体未抽到（无法校验 onClick）")
    elif "onRegenerate" not in footer_body:
        errs.append("[C7] footer 重新生成 Button 未绑 onRegenerate 回调")
    elif not re.search(r"onClick\s*=\s*\{?\s*onRegenerate", footer_body):
        errs.append("[C7] 重新生成 Button onClick 未指向 onRegenerate")
    else:
        print("[C7] OK  Button onClick={onRegenerate}")

    # ── D. onRegenerate optional prop + disabled 兜底 ──
    # [8] props 新增 onRegenerate?: () => void
    if not re.search(r"onRegenerate\s*\?\s*:\s*\(\s*\)\s*=>\s*void", src):
        errs.append("[D8] ChatMessageBubbleProps 缺 onRegenerate?: () => void（optional prop）")
    else:
        print("[D8] OK  props 新增 onRegenerate?: () => void（optional）")
    # [9] 解构 props 含 onRegenerate
    # 解构在函数签名 ``}: ChatMessageBubbleProps) {`` 之前。查 ``onRegenerate,`` 在解构列表。
    # 简化：源码中 onRegenerate 出现在解构（紧跟 actionGroup 后）+ footer 内使用。
    if "onRegenerate," not in src and "onRegenerate }" not in src and "onRegenerate\n" not in src:
        # 放宽：onRegenerate 至少出现 2 处（解构 + 使用）
        cnt = src.count("onRegenerate")
        if cnt < 2:
            errs.append(f"[D9] onRegenerate 在源码出现 {cnt} 次（应 ≥2：解构 + 使用）")
        else:
            print(f"[D9] OK  onRegenerate 解构 + 使用（出现 {cnt} 次）")
    else:
        print("[D9] OK  props 解构含 onRegenerate")
    # [10] Button disabled 守卫含 !onRegenerate（[需求2-后端] 追加 regenerating loading 态后
    # 兼容 `disabled={!onRegenerate || regenerating}` / `disabled={!onRegenerate}` 两形态）
    if not footer_body:
        errs.append("[D10] footer prop 体未抽到（无法校验 disabled 兜底）")
    elif "!onRegenerate" not in footer_body:
        errs.append("[D10] 重新生成 Button disabled 守卫缺 !onRegenerate（未注入应 disabled 兜底）")
    elif "disabled" not in footer_body:
        errs.append("[D10] 重新生成 Button 缺 disabled 守卫")
    else:
        print("[D10] OK  Button disabled 含 !onRegenerate 兜底（regenerating loading 态兼容）")
    # [11] tooltip 按 onRegenerate 有无区分
    if not footer_body:
        errs.append("[D11] footer prop 体未抽到（无法校验 tooltip 区分）")
    elif "开发中" not in footer_body:
        errs.append("[D11] footer 缺「开发中」tooltip（onRegenerate 未注入时应提示开发中）")
    elif not re.search(r"onRegenerate\s*\?\s*['\"]", footer_body):
        errs.append("[D11] footer tooltip 未按 onRegenerate 有无区分文案")
    else:
        print("[D11] OK  tooltip 按 onRegenerate 有无区分（重新生成 / 重新生成（开发中））")

    # ── E. 显隐守卫 ──
    # [12] 操作栏渲染条件含 !isStreaming
    if not footer_body:
        errs.append("[E12] footer prop 体未抽到（无法校验 !isStreaming 守卫）")
    elif "!isStreaming" not in footer_body:
        errs.append("[E12] 操作栏渲染条件缺 !isStreaming（流式中不应显示）")
    else:
        print("[E12] OK  操作栏含 !isStreaming 守卫（流式中不显示）")
    # [13] 操作栏渲染条件含 !isUser
    if not footer_body:
        errs.append("[E13] footer prop 体未抽到（无法校验 !isUser 守卫）")
    elif "!isUser" not in footer_body:
        errs.append("[E13] 操作栏渲染条件缺 !isUser（用户气泡不应显示重生成）")
    else:
        print("[E13] OK  操作栏含 !isUser 守卫（用户气泡不显示）")

    # ── F. 与产物下载卡共存 + CSS ──
    # [14] footer 同时含 chat-artifact-block + chat-action-bar
    if not footer_body:
        errs.append("[F14] footer prop 体未抽到（无法校验共存）")
    elif "chat-artifact-block" not in footer_body or "chat-action-bar" not in footer_body:
        errs.append("[F14] footer 未同时含 chat-artifact-block + chat-action-bar（应共存不互斥）")
    else:
        print("[F14] OK  footer 同时承载产物块 + 操作栏（共存不互斥）")
    # [15] CSS .chat-action-bar 存在
    if not css:
        errs.append("[F15] ChatMessageBubble.css 不存在")
    elif ".chat-action-bar" not in css:
        errs.append("[F15] ChatMessageBubble.css 缺 .chat-action-bar 样式")
    else:
        print("[F15] OK  CSS .chat-action-bar 样式存在")

    return errs


def main() -> int:
    print("=== 需求2-前端 回归：ChatMessageBubble footer 操作栏（复制 + 重新生成）契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "需求2-前端 操作栏契约锁定（footer 槽位 + 复制复用 + 重新生成 Button + disabled 兜底）：\n"
        "  · A footer={ prop 内含 chat-action-bar 容器；\n"
        "  · B 复制复用 BubbleCopyButton（import 现成组件，不重写复制逻辑）；\n"
        "  · C 「重新生成」AntD Button（type=text + ReloadOutlined + onClick=onRegenerate）；\n"
        "  · D onRegenerate?: ()=>void optional prop + disabled={!onRegenerate} 兜底 + tooltip 区分；\n"
        "  · E 显隐守卫 !isStreaming（流式中不显示）+ !isUser（用户气泡不显示）；\n"
        "  · F footer 同时承载产物块 + 操作栏（共存）+ CSS .chat-action-bar 样式。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
