"""需求2-前端 回归：ChatMessageBubble 结构化卡片渲染子组件契约.

锁住 [需求2-前端] 改动——``src/components/ChatMessageBubble.tsx`` 新增结构化卡片渲染
子组件（AntD Card/Descriptions/List/Table），解析 ``` ```card ``` 围栏约定格式块。
设计单真源：``docs/structured-result-card-schema.md``（[需求2-设计] commit 9df5116）。
后端提示词/解析已落（[需求2-后端] commit b9a0597，``backend/llm/card_fragment.py``）。

核心契约（设计 §6 + §2）：
  - CARD_RE 正则与后端 ``llm.card_fragment.CARD_FRAGMENT_RE`` **byte-identical**
    （``/```card\\s*\\n([\\s\\S]*?)```/g``）——后端计数与前端解析对同一 content 的块数判定一致。
  - 卡片是 ``content`` 子串，走现有透传，不改 DB/事件/message shape。
  - 三 kind：``kv``→Descriptions / ``list``→List / ``table``→Table；未知 kind→降级普通代码块。
  - 非法 JSON 块降级普通代码块（不静默丢弃——设计 §6）。
  - 字段容错：items 非数组当空；rows 行长≠columns 截断补空（设计 §5）。
  - 所有值 string（数字也 stringify）。

纯静态契约（读 TS 源码断言，不依赖前端构建/在线），与 test_vh26_highlight_message_memo.py
同款风格（grep TS 源码 + 正则断言）。本任务只校验源码结构，不跑 React 渲染。

七段契约：

  A. 解析器（parseCards + CARD_RE + splitContentByCards · 单一真源）
    1. ``CARD_RE`` 常量存在（与后端 byte-identical）。
    2. CARD_RE 模式 = ``/```card\\s*\\n([\\s\\S]*?)```/g``（含 g flag 全局扫描）。
    3. ``parseCards(content)`` 返回 {json, raw, start, end}（合法 payload + 区间；非法块 json=null）。
    4. ``splitContentByCards(content)`` 把 content 切成 text/card 段交替（按出现顺序）。
    5. 非法 JSON 块保留 raw（降级 code 块，不静默丢弃——设计 §6）。

  B. 渲染子组件（StructuredCard · 三 kind 分支）
    6. ``StructuredCard`` 组件存在（function 声明，接 card prop）。
    7. kv→AntD Descriptions（size="small" column=1）。
    8. list→AntD List（size="small" bullet）。
    9. table→AntD Table（size="small" pagination=false）。
   10. 未知 kind→降级普通代码块（pre.chat-card-fallback，设计 §5）。
   11. 解析失败块→降级普通代码块（pre.chat-card-fallback，设计 §6）。
   12. 字段容错：items 非数组当空；rows 截断补齐 columns（设计 §5）。

  C. contentRender 接入（无卡片走原路径 · 有卡片按段切）
   13. contentRender 含 hasCards 分支（无卡片走原 renderContent/纯文本路径）。
   14. 有卡片时 contentSegments.map 按段渲染（text→原路径，card→StructuredCard）。
   15. 流式光标（chat-streaming-cursor）仍保留（hasCards 分支也追光标）。

  D. AntD 组件用开源非手搓（[[use-open-source-not-handrolled]]）
   16. import AntD Card/Descriptions/List/Table（不手写 div 容器）。
   17. 三 kind 分别用 Descriptions/List/Table 渲染（非自绘 div 表格）。

  E. 与后端 byte-identical（CARD_RE == CARD_FRAGMENT_RE）
   18. CARD_RE 模式字符串与 backend CARD_FRAGMENT_RE 模式字符串完全一致。

  F. 行为零变（无卡片回复走原路径，不破坏既有渲染）
   19. hasCards=false 时 contentRender 走原 renderContent?renderContent(String(c)):String(c) 路径。
   20. ChatMessageBubble props 签名不变（content/renderContent/isStreaming 仍在）。

  G. CSS 样式（chat-card-block + 三 kind 容器 + 降级块）
   21. ChatMessageBubble.css 含 .chat-card-block（卡片外层）。
   22. ChatMessageBubble.css 含 .chat-card-fallback（降级 code 块）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUBBLE_TSX = REPO / "src" / "components" / "ChatMessageBubble.tsx"
BUBBLE_CSS = REPO / "src" / "components" / "ChatMessageBubble.css"
BACKEND_CARD_FRAGMENT = REPO / "backend" / "llm" / "card_fragment.py"

# 与后端 CARD_FRAGMENT_RE byte-identical 的期望模式（设计 §6 + 后端 card_fragment.py）。
# 后端 re.compile(r"```card\s*\n([\s\S]*?)```")；前端 JS 正则字面量带 g flag。
EXPECTED_CARD_RE_PATTERN = r"```card\s*\n([\s\S]*?)```"


def _strip_ts_comments(src: str) -> str:
    """剔 // 单行注释（粗剔，契约断言不依赖字符串内 // 精度）。"""
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _paren_match(src: str, open_idx: int) -> int:
    """从 src[open_idx]=='(' 起，跨圆括号配对到匹配的 `)`，返回其 index（找不到返 -1）。"""
    depth = 0
    i = open_idx
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _fn_body_ts(src: str, fname: str, indent_opts=("", "    ")) -> str:
    """抽 TS function/const 箭头函数完整定义（函数体）。试多种声明形：
       - function NAME(...) { ... }           （含解构参数 function NAME({ card }: T) { ... }）
       - const NAME = (props) => { ... }
       - const NAME: Type = (...) => { ... }
    关键：先跨圆括号配对跳过参数列表，再找函数体 `{`（避免误吃解构 `{ card }` 的花括号）。"""
    # 形1: (async )?function NAME( ... ) { body }
    m = re.search(rf"(?:async )?function {fname}\(", src)
    if m:
        open_paren = src.find("(", m.end() - 1)
        close_paren = _paren_match(src, open_paren)
        if close_paren >= 0:
            # 函数体 `{` 在 `)` 之后（可能隔 type annotation `: T`）
            bi = src.find("{", close_paren)
            if bi >= 0:
                return _brace_slice(src, bi)
    # 形2/3: const NAME ... = ( ... ) => { body }  /  const NAME: Type = ... => { body }
    m = re.search(rf"const {fname}\b", src)
    if m:
        # 找 = 后的箭头函数体 {
        # 先找箭头 `=>`，再找其后的 `{`（跨过参数列表 `(...)` 的话也 OK——箭头后直接是 `{`）
        arrow_idx = src.find("=>", m.end())
        if arrow_idx >= 0:
            bi = src.find("{", arrow_idx)
            if bi >= 0:
                return _brace_slice(src, bi)
    return ""


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


def assert_contract() -> list[str]:
    errs: list[str] = []
    if not BUBBLE_TSX.exists():
        errs.append("[setup] src/components/ChatMessageBubble.tsx 不存在")
        return errs
    src = BUBBLE_TSX.read_text(encoding="utf-8")
    src_nc = _strip_ts_comments(src)
    css = BUBBLE_CSS.read_text(encoding="utf-8") if BUBBLE_CSS.exists() else ""
    backend_cf = BACKEND_CARD_FRAGMENT.read_text(encoding="utf-8") if BACKEND_CARD_FRAGMENT.exists() else ""
    # [任务10d] parseCards/splitContentByCards/CARD_RE 抽到 src/lib/cardSegments.ts（单一真源）。
    # 组件文件 import 复用，纯函数定义在 lib。两处都查——lib 优先（真源），回退组件（兼容）。
    card_segments_path = REPO / "src" / "lib" / "cardSegments.ts"
    card_segments = card_segments_path.read_text(encoding="utf-8") if card_segments_path.exists() else ""
    cs_src = card_segments or src

    # ── A. 解析器（单一真源 src/lib/cardSegments.ts）──
    # [1] CARD_RE 常量存在
    if not re.search(r"\bCARD_RE\b\s*=", cs_src):
        errs.append("[A1] cardSegments.ts 未定义 CARD_RE 常量")
    else:
        print("[A1] OK  CARD_RE 常量存在（cardSegments.ts 单一真源）")
    # [2] CARD_RE 模式 byte-identical（含 g flag）
    m = re.search(r"const CARD_RE\s*=\s*/([^/]+)/([gimsuy]*)", cs_src)
    if not m:
        errs.append("[A2] CARD_RE 未用正则字面量 /.../flags 定义（cardSegments.ts）")
    else:
        pat, flags = m.group(1), m.group(2)
        if pat != EXPECTED_CARD_RE_PATTERN:
            errs.append(
                f"[A2] CARD_RE 模式 {pat!r} != 期望 {EXPECTED_CARD_RE_PATTERN!r}"
                "（必须与后端 CARD_FRAGMENT_RE byte-identical）"
            )
        elif "g" not in flags:
            errs.append(f"[A2] CARD_RE 缺 g flag（matchAll 全局扫描需 g）flags={flags!r}")
        else:
            print(f"[A2] OK  CARD_RE 模式 byte-identical + g flag（flags={flags!r}）")
    # [3] parseCards 返回 {json, raw, start, end}
    pc_body = _fn_body_ts(cs_src, "parseCards")
    if not pc_body:
        errs.append("[A3] parseCards 函数未找到（cardSegments.ts）")
    else:
        # parseCards 用对象字面量 out.push({...})：
        #   - `json:` / `json =` 出现在 push 的对象字面量里（字段）或 const json =（局部变量）
        #   - `raw` 在 push 里是 shorthand（`raw,` 不带冒号），或 const raw = 局部变量
        #   - `start` / `end` 同样是 shorthand 或 const 局部变量
        # 故校验「字段名出现在 push({...}) 对象字面量内」——用正则匹配 push 对象字面量块。
        # 简化：parseCards 必有 out.push({...}) 且对象内含 json + raw + start + end 四字段（任一形）。
        # 捕获含闭合 `}` 的整块（`end` 作末字段时其后紧跟 `}`，需把 `}` 纳入块才能匹配 \bend\s*}）
        push_blocks = re.findall(r"out\.push\(\s*(\{[^}]*\})", pc_body, re.S)
        fields_seen: set[str] = set()
        for blk in push_blocks:
            for f in ("json", "raw", "start", "end"):
                # 字段名作为 key（`json:` / `json =`）或 shorthand（`json,` / `json}` / `json }`)
                if re.search(rf"\b{f}\s*[:,}}]", blk):
                    fields_seen.add(f)
        missing = {"json", "raw", "start", "end"} - fields_seen
        if missing:
            errs.append(
                f"[A3] parseCards out.push({{...}}) 缺字段 {missing}"
                "——应返回 {json, raw, start, end}"
            )
        else:
            print("[A3] OK  parseCards 返回 {json, raw, start, end}（合法 payload + 区间 + 非法块 raw）")
    # [4] splitContentByCards 切 text/card 段交替
    sc_body = _fn_body_ts(cs_src, "splitContentByCards")
    if not sc_body:
        errs.append("[A4] splitContentByCards 函数未找到（cardSegments.ts）")
    elif "type: 'text'" not in sc_body and "type:'text'" not in sc_body:
        errs.append("[A4] splitContentByCards 未产 text 段（应 text/card 交替）")
    elif "type: 'card'" not in sc_body and "type:'card'" not in sc_body:
        errs.append("[A4] splitContentByCards 未产 card 段（应 text/card 交替）")
    else:
        print("[A4] OK  splitContentByCards 产 text/card 段交替（按出现顺序）")
    # [5] 非法 JSON 块保留 raw（降级 code 块，不静默丢弃）
    if not pc_body:
        errs.append("[A5] parseCards 缺（无法校验非法 JSON 保留 raw）")
    elif "catch" not in pc_body and "JSON.parse" not in pc_body:
        errs.append("[A5] parseCards 缺 JSON.parse/catch（无法降级非法 JSON）")
    else:
        # 非法 JSON 块 json=null + 保留 raw（设计 §6：降级普通代码块，不静默丢弃）
        has_catch_fallback = "catch" in pc_body and ("raw" in pc_body)
        if not has_catch_fallback:
            errs.append("[A5] parseCards catch 分支未保留 raw（非法 JSON 应降级 code 块不丢弃）")
        else:
            print("[A5] OK  非法 JSON 块 catch 分支保留 raw（降级 code 块，不静默丢弃）")

    # ── B. 渲染子组件（StructuredCard · 三 kind 分支，定义在 ChatMessageBubble.tsx）──
    sc_card_body = _fn_body_ts(src, "StructuredCard")
    if not sc_card_body:
        errs.append("[B6] StructuredCard 组件未找到")
    else:
        print("[B6] OK  StructuredCard 组件存在（接 card prop）")
        sc_nc = _strip_ts_comments(sc_card_body)
        # [7] kv→Descriptions
        if "kind === 'kv'" not in sc_nc and "kind==='kv'" not in sc_nc:
            errs.append("[B7] StructuredCard 缺 kv 分支（应 Descriptions）")
        elif "<Descriptions" not in sc_nc and "Descriptions" not in sc_nc:
            errs.append("[B7] kv 分支未用 AntD Descriptions")
        else:
            print("[B7] OK  kv → AntD Descriptions")
        # [8] list→List
        if "kind === 'list'" not in sc_nc and "kind==='list'" not in sc_nc:
            errs.append("[B8] StructuredCard 缺 list 分支（应 List）")
        elif "<List" not in sc_nc and re.search(r"\bList\b", sc_nc) is None:
            errs.append("[B8] list 分支未用 AntD List")
        else:
            print("[B8] OK  list → AntD List")
        # [9] table→Table
        if "kind === 'table'" not in sc_nc and "kind==='table'" not in sc_nc:
            errs.append("[B9] StructuredCard 缺 table 分支（应 Table）")
        elif "<Table" not in sc_nc and re.search(r"\bTable\b", sc_nc) is None:
            errs.append("[B9] table 分支未用 AntD Table")
        else:
            print("[B9] OK  table → AntD Table")
        # [10] 未知 kind→降级普通代码块（chat-card-fallback）
        has_unknown_fallback = (
            ("else" in sc_nc or "else:" in sc_nc)
            and "chat-card-fallback" in sc_card_body
        )
        if not has_unknown_fallback:
            errs.append("[B10] StructuredCard 未知 kind 未降级 chat-card-fallback 代码块")
        else:
            print("[B10] OK  未知 kind → 降级普通代码块（chat-card-fallback，设计 §5）")
        # [11] 解析失败块→降级普通代码块（json=null 分支）
        if "chat-card-fallback" not in sc_card_body:
            errs.append("[B11] StructuredCard 缺 chat-card-fallback（解析失败/未知 kind 都应降级）")
        elif not re.search(r"if\s*\(\s*!\s*card\.json", sc_card_body):
            errs.append("[B11] StructuredCard 缺 !card.json 降级分支（解析失败块应降级 code 块）")
        else:
            print("[B11] OK  解析失败块（!card.json）→ 降级普通代码块（设计 §6）")
        # [12] 字段容错：items 非数组当空 + rows 截断补齐
        has_items_guard = "Array.isArray" in sc_nc and "items" in sc_nc
        has_rows_pad = "while" in sc_nc and "cells" in sc_nc and ("push" in sc_nc or "slice" in sc_nc)
        if not has_items_guard:
            errs.append("[B12] StructuredCard 缺 items 非数组容错（设计 §5：非数组当空）")
        elif not has_rows_pad:
            errs.append("[B12] StructuredCard 缺 rows 截断/补齐容错（设计 §5：行长≠columns 截断补空）")
        else:
            print("[B12] OK  字段容错（items 非数组当空 + rows 截断补齐 columns）")

    # ── C. contentRender 接入 ──
    # [13] contentRender 含 hasCards 分支
    if "hasCards" not in src:
        errs.append("[C13] ChatMessageBubble 缺 hasCards 判定（contentRender 无法分支）")
    elif "contentRender" not in src:
        errs.append("[C13] ChatMessageBubble 缺 contentRender")
    else:
        print("[C13] OK  contentRender 含 hasCards 分支（无卡片走原路径）")
    # [14] 有卡片时 contentSegments.map 按段渲染
    if "contentSegments" not in src or ".map(" not in src:
        errs.append("[C14] ChatMessageBubble 缺 contentSegments.map（有卡片时应按段渲染）")
    elif "StructuredCard" not in src:
        errs.append("[C14] contentRender 未用 StructuredCard 渲染卡片段")
    else:
        print("[C14] OK  有卡片时 contentSegments.map（text→原路径 + card→StructuredCard）")
    # [15] 流式光标保留（hasCards 分支也追光标）
    if "chat-streaming-cursor" not in src:
        errs.append("[C15] ChatMessageBubble 缺 chat-streaming-cursor（流式光标丢失）")
    else:
        print("[C15] OK  流式光标保留（hasCards 分支也追光标）")

    # ── D. AntD 组件用开源非手搓 ──
    # [16] import AntD Card/Descriptions/List/Table
    antd_import_match = re.search(r"from\s+'antd'", src)
    if not antd_import_match:
        errs.append("[D16] ChatMessageBubble 未 import from 'antd'")
    else:
        # 取 import 语句整行（含多行 import）
        imp_start = src.rfind("import {", 0, antd_import_match.start())
        imp_end = antd_import_match.end()
        imp_block = src[imp_start:imp_end] if imp_start >= 0 else src[:imp_end]
        for comp in ("Card", "Descriptions", "List", "Table"):
            if comp not in imp_block:
                errs.append(f"[D16] antd import 缺 {comp}（应 Card/Descriptions/List/Table 全有）")
        if not any(e.startswith("[D16]") for e in errs):
            print("[D16] OK  import AntD Card/Descriptions/List/Table（不手写 div 容器）")
    # [17] 三 kind 分别用 Descriptions/List/Table（已在 B7-B9 校验，此处聚合断言）
    if sc_card_body and all(
        f"kind === '{k}'" in _strip_ts_comments(sc_card_body) or f"kind==='{k}'" in _strip_ts_comments(sc_card_body)
        for k in ("kv", "list", "table")
    ):
        print("[D17] OK  三 kind 分别用 Descriptions/List/Table（非自绘 div 表格）")
    else:
        errs.append("[D17] 三 kind 分支不全（kv/list/table 各一分支）")

    # ── E. 与后端 byte-identical ──
    # [18] CARD_RE 模式字符串 == 后端 CARD_FRAGMENT_RE 模式字符串（前端真源 cardSegments.ts）
    backend_match = re.search(r'CARD_FRAGMENT_RE\s*=\s*re\.compile\(r"([^"]+)"\)', backend_cf)
    frontend_match = re.search(r"const CARD_RE\s*=\s*/([^/]+)/", cs_src)
    if not backend_match:
        errs.append("[E18] 后端 CARD_FRAGMENT_RE 未找到（无法对齐 byte-identical）")
    elif not frontend_match:
        errs.append("[E18] 前端 CARD_RE 未找到（cardSegments.ts，无法对齐 byte-identical）")
    elif backend_match.group(1) != frontend_match.group(1):
        errs.append(
            f"[E18] CARD_RE({frontend_match.group(1)!r}) != CARD_FRAGMENT_RE"
            f"({backend_match.group(1)!r})——前后端必须 byte-identical"
        )
    else:
        print(
            f"[E18] OK  CARD_RE == CARD_FRAGMENT_RE byte-identical"
            f"（模式 {frontend_match.group(1)!r}，前后端块数判定一致）"
        )

    # ── F. 行为零变 ──
    # [19] hasCards=false 走原 renderContent 路径（[任务2/55c6eca] fallback 从 String(c) 改 renderMarkdown(c)）
    if "renderContent ? renderContent(String(c)) : renderMarkdown(String(c))" not in src:
        errs.append("[F19] ChatMessageBubble 缺原 renderContent?...:renderMarkdown(String(c)) 路径（无卡片应走原路径）")
    else:
        print("[F19] OK  hasCards=false 走原 renderContent?...:renderMarkdown(String(c)) 路径（行为零变）")
    # [20] props 签名不变（content/renderContent/isStreaming 仍在）
    for prop in ("content:", "renderContent", "isStreaming"):
        if prop not in src:
            errs.append(f"[F20] ChatMessageBubble props 缺 {prop}（签名应不变）")
    if not any(e.startswith("[F20]") for e in errs):
        print("[F20] OK  ChatMessageBubble props 签名不变（content/renderContent/isStreaming 仍在）")

    # ── G. CSS 样式 ──
    if not css:
        errs.append("[G] ChatMessageBubble.css 不存在")
    else:
        # [21] .chat-card-block
        if ".chat-card-block" not in css:
            errs.append("[G21] ChatMessageBubble.css 缺 .chat-card-block（卡片外层样式）")
        else:
            print("[G21] OK  .chat-card-block 样式存在（卡片外层）")
        # [22] .chat-card-fallback
        if ".chat-card-fallback" not in css:
            errs.append("[G22] ChatMessageBubble.css 缺 .chat-card-fallback（降级 code 块样式）")
        else:
            print("[G22] OK  .chat-card-fallback 样式存在（降级 code 块）")

    return errs


def main() -> int:
    print("=== 需求2-前端 回归：ChatMessageBubble 结构化卡片渲染子组件契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "需求2-前端 契约锁定（卡片走 content 子串透传，前端解析渲染，不改 DB/事件）：\n"
        "  · A 解析器（CARD_RE 与后端 byte-identical + parseCards/splitContentByCards 切段）；\n"
        "  · B StructuredCard 三 kind 分支（kv→Descriptions/list→List/table→Table）+ 未知 kind 降级 code 块；\n"
        "  · C contentRender 按 hasCards 分支（无卡片走原路径，有卡片按段渲染）；\n"
        "  · D AntD Card/Descriptions/List/Table 开源组件（不手写 div 容器）；\n"
        "  · E CARD_RE 与后端 CARD_FRAGMENT_RE byte-identical（块数判定一致）；\n"
        "  · F 行为零变（无卡片回复走原 renderContent 路径 + props 签名不变）；\n"
        "  · G CSS .chat-card-block + .chat-card-fallback 样式存在。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
