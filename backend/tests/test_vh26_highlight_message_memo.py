"""VH26 回归：HighlightMessage memo + 成员名稳定集降重渲染（task B29）.

锁住 B29 优化——``src/components/ChatPanel.tsx HighlightMessage`` 从「每气泡每渲染都跑
mention 高亮」优化为 ``memo`` 包裹 + ``memberNames`` 稳定集两道防线，降流式期高频重渲染.

B29 优化结论（memo + useMemo 两道防线，互补，行为零变只降重渲染）：

  ── 问题：HighlightMessage 在 chatMessages.flatMap 里每条非用户消息渲染一次 ──
    ChatPanel 是大组件——``chatMessages`` / ``events`` / ``streaming`` / ``coordStreaming``
    / ``agentStatuses`` 任一变化（高频：task_token 流式逐字推送、stats ~200ms 节流、reasoning
    delta 攒批 flush）都触发整个 ChatPanel 重渲染，``flatMap`` 重跑，**每条历史消息的
    HighlightMessage 都重跑 ``content.split(regex)`` + ``members.some()``**——N 条消息 × 每次
    setState 全量重算. 长会话（几百条历史）+ 流式期高频重渲染，split+some 重复算 O(N×M) 是
    肉眼可见的卡顿源.

  ── 优化 1：``memo`` 包裹（props 浅比较短路历史气泡） ──
    props ``content``(string|null) + ``members``(GroupMember[]) 浅比较. ``content`` 是消息正文
    （持久化后不变，除非编辑——本项目无编辑），``members`` 是 ChatView state（切群时整体替换，
    平时稳定）. 故 memo 让「props 没变的历史气泡」直接跳过重渲染——流式期只有当前正在流式的那条
    气泡（content 在变）+ stats 行重渲染，其余历史气泡 memo 命中零开销.

  ── 优化 2：``memberNames`` 稳定集（O(M).some → O(1).has） ──
    原 ``members.some(m => m.agent_name===name || m.alias===name)`` 每个候选 mention 都 O(M)
    扫全部成员. 改 ``useMemo`` 把 members 投影成 ``Set<string>``（agent_name + alias 去空），查
    mention 成员身份从 O(M).some → O(1).has. ``memberNames`` deps=[members]——members 引用变
    （切群）才重算 Set，平时稳定引用复用.

  ── 为何 memo 浅比较够用（不需自定义 areEqual） ──
    ``content`` 是 string（值类型，=== 可靠）；``members`` 是数组引用（ChatView 切群才 setMembers
    新数组，平时同引用）. memo 默认 ``Object.is`` 浅比较这两类 props 正确——不需自定义 areEqual.
    ``members`` 投影成 Set 后，HighlightMessage 内部不再依赖 members 数组结构（只读 Set），故
    members 引用即使每帧变（不会，但假设）也不破 memo——memo 比 props 早短路.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh25 同款风格.

四段契约：

  A. memo 包裹（props 浅比较短路历史气泡）
    1. HighlightMessage 用 ``memo(...)`` 包裹（非裸 function）.
    2. props 仍是 content + members（签名不变，调用方零改）.
    3. memo 无自定义 areEqual（默认 Object.is 浅比较够用——content 是 string + members 是数组引用）.

  B. memberNames 稳定集（O(M).some → O(1).has）
    4. ``const memberNames = useMemo(...)`` 把 members 投影成 Set<string>.
    5. Set 含 agent_name + alias（去空——``if (m.agent_name)`` / ``if (m.alias)`` 守卫）.
    6. memberNames deps=[members]（members 引用变才重算 Set）.
    7. mention 成员身份查改 ``memberNames.has(name)``（非旧 ``members.some(...)``）.

  C. 行为零变（高亮逻辑 + split regex + Tag 渲染不变）
    8. split 正则 ``/(@[^\\s,，.。!！?？:：;；\\n]+)/g`` 不变（B21 已锁，B29 不动）.
    9. Tag 渲染 ``color="blue"`` + style 不变（视觉零变）.
   10. ``part.startsWith('@')`` 候选判定 + ``name = part.slice(1)`` 不变.

  D. 调用方零改 + 无回归（HighlightMessage 调用点不变）
   11. 调用方 ``<HighlightMessage content={msg.content} members={members} />`` 不变.
   12. 空消息兜底 ``<Text type="secondary" italic>（空消息）</Text>`` 不变.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHATPANEL_TSX = REPO / "src" / "components" / "ChatPanel.tsx"


def _fn_body_ts(src: str, fname: str) -> str:
    """抽 TS 函数完整定义（含签名 + 函数体）。用花括号配对从函数体的 `{` 起，但需返回
    含签名（props 解构）以便断言 content/members prop。故从 `const NAME = memo(function NAME(`
    起取整段到 memo 闭合。"""
    # 形1: const NAME = memo(function NAME(...) { ... })
    idx = src.find(f"const {fname} = memo(function {fname}(")
    if idx >= 0:
        # 跨花括号配对函数体，再吃掉 memo 的闭合 ')'
        pi = src.find(") {", idx)
        if pi < 0:
            return ""
        bi = src.find("{", pi)
        body = _brace_slice(src, bi)
        # 含签名：从 const 起
        return src[idx : idx + (bi - idx) + len(body)]
    # 形2: function NAME(...) { ... }
    idx = src.find(f"function {fname}(")
    if idx >= 0:
        pi = src.find(") {", idx)
        if pi < 0:
            return ""
        bi = src.find("{", pi)
        body = _brace_slice(src, bi)
        return src[idx : idx + (bi - idx) + len(body)]
    return ""


def _brace_slice(src: str, bi: int) -> str:
    """从 src[bi]=='{' 起，跨花括号配对到匹配的 `}`，返回含两端花括号在内的切片。"""
    depth = 0
    i = bi
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[bi : i + 1] if i < len(src) else ""


def _strip_ts_comments(src: str) -> str:
    """剔 // 单行注释（保留字符串字面量内的 //——粗剔够用于契约断言）。"""
    out = []
    for line in src.splitlines():
        # 去掉 // 后内容（不处理字符串内的 //，契约断言不依赖该精度）
        idx = line.find("//")
        if idx >= 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _highlight_memo_close(src: str) -> str:
    """从 `const HighlightMessage = memo(function HighlightMessage(` 起，
    跨花括号配对找到函数体闭合 `}`，再看其后是 `)`（memo 闭合无 areEqual）还是 `,`（带 areEqual）。
    返回闭合处一小段以供断言。"""
    idx = src.find("const HighlightMessage = memo(function HighlightMessage(")
    if idx < 0:
        return ""
    pi = src.find(") {", idx)
    if pi < 0:
        return ""
    bi = src.find("{", pi)
    if bi < 0:
        return ""
    depth = 0
    i = bi
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    # ts[i] == '}' closes function body; peek a few chars after (skip ws) to see memo close
    j = i + 1
    while j < len(src) and src[j] in " \t\n\r":
        j += 1
    return src[i : j + 2]


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = CHATPANEL_TSX.read_text(encoding="utf-8")

    body = _fn_body_ts(src, "HighlightMessage")
    if not body:
        errs.append("[setup] HighlightMessage 函数体未找到（memo(function…) 或 function 形）")
        return errs
    body_nc = _strip_ts_comments(body)

    # ── A. memo 包裹 ──
    # [1] memo(...) 包裹
    if not re.search(r"const HighlightMessage = memo\(\s*function HighlightMessage\(", src):
        errs.append("[A1] HighlightMessage 未用 memo(...) 包裹（每渲染都重跑 split+some）")
    else:
        print("[A1] OK  HighlightMessage = memo(function ...) 包裹（props 浅比较短路历史气泡）")
    # [2] props 仍是 content + members（签名含 content 与 members 两 prop）
    if "content," in body and "members," in body:
        print("[A2] OK  props 仍是 content + members（签名不变，调用方零改）")
    else:
        errs.append("[A2] HighlightMessage props 非 content+members（签名变，调用方需改）")
    # [3] 无自定义 areEqual（memo 第二参数）
    # 跨花括号配对找到函数体闭合 `}`，再看其后是 `)`（memo 闭合无 areEqual）还是 `,`（带 areEqual）。
    close_region = _highlight_memo_close(src)
    if close_region == "})" or close_region.startswith("})"):
        print("[A3] OK  memo 无自定义 areEqual（函数体 } 紧跟 memo ) 闭合，默认 Object.is 浅比较）")
    elif "," in close_region:
        errs.append(f"[A3] HighlightMessage memo 带自定义 areEqual（应默认 Object.is 浅比较）close={close_region!r}")
    else:
        errs.append(f"[A3] memo 闭合不匹配 close={close_region!r}")

    # ── B. memberNames 稳定集 ──
    # [4] const memberNames = useMemo(...)
    if not re.search(r"const memberNames = useMemo\(", body):
        errs.append("[B4] HighlightMessage 缺 const memberNames = useMemo(...)（无稳定集）")
    else:
        print("[B4] OK  const memberNames = useMemo(...) 把 members 投影成稳定集")
    # [5] Set 含 agent_name + alias（去空守卫）
    mn_body = re.search(r"const memberNames = useMemo\(\(\)\s*=>\s*\{(.*?)\},\s*\[members\]\)", body, re.S)
    if not mn_body:
        errs.append("[B5] memberNames useMemo 体未找到（deps=[members] 锚点失）")
    else:
        mn = mn_body.group(1)
        has_set = "new Set<string>()" in mn
        has_agent_name = "m.agent_name" in mn and re.search(r"if\s*\(m\.agent_name\)", mn)
        has_alias = "m.alias" in mn and re.search(r"if\s*\(m\.alias\)", mn)
        if not (has_set and has_agent_name and has_alias):
            errs.append(f"[B5] memberNames 投影缺 Set+agent_name+alias 去空守卫（set={has_set} an={has_agent_name} al={has_alias}）")
        else:
            print("[B5] OK  Set<string> 含 agent_name + alias（if 守卫去空）")
    # [6] deps=[members]——锁 memberNames useMemo 的 deps 数组是 [members]
    # 单行正则会跨注释/换行失败，改在 memberNames body 内找 `}, [members])` 闭合。
    mn_block = re.search(r"const memberNames = useMemo\(\(\)\s*=>\s*\{.*?\},\s*\[members\]\)", body, re.S)
    if not mn_block:
        errs.append("[B6] memberNames useMemo deps 非 [members]（引用变才重算 Set 破）")
    else:
        print("[B6] OK  memberNames deps=[members]（members 引用变才重算 Set）")
    # [7] mention 成员身份查改 memberNames.has(name)
    if "memberNames.has(name)" not in body_nc:
        errs.append("[B7] HighlightMessage 未用 memberNames.has(name)（仍是 members.some O(M) 扫）")
    else:
        print("[B7] OK  mention 成员身份查改 memberNames.has(name)（O(1).has 非 O(M).some）")
    # [7b] 不再有 members.some（O(M) 扫已替换）
    if re.search(r"members\.some\(", body_nc):
        errs.append("[B7b] HighlightMessage 仍有 members.some（O(M) 扫未替换成 has）")
    else:
        print("[B7b] OK  无 members.some（O(M) 扫已替换成 memberNames.has O(1)）")

    # ── C. 行为零变 ──
    # [8] split 正则不变（B21 已锁的 @mention 分割正则）
    # JS 源里正则字面量是 /(@[^\s,，.。!！?？:：;；\n]+)/g；读进 Python 字符串后反斜杠是单字符。
    # 用纯字符串包含判定（不依赖正则的转义歧义）。
    split_needle = "content.split(/(@[^\\s,，.。!！?？:：;；\\n]+)/g)"
    if split_needle not in body:
        errs.append("[C8] HighlightMessage split 正则变（B21 锁的 mention 分割破）")
    else:
        print("[C8] OK  split 正则不变（B21 锁的 @mention 分割）")
    # [9] Tag 渲染 color="blue" + style 不变
    if not re.search(r'<Tag key=\{i\} color="blue" style=\{\{ margin: 0, padding: \'0 4px\', lineHeight: \'18px\' \}\}>', body):
        errs.append("[C9] HighlightMessage Tag 渲染变（视觉零变破）")
    else:
        print("[C9] OK  Tag 渲染 color=blue + style 不变（视觉零变）")
    # [10] startsWith('@') + slice(1) 候选判定不变
    if "part.startsWith('@')" not in body or "part.slice(1)" not in body:
        errs.append("[C10] HighlightMessage 候选判定 startsWith+slice 变")
    else:
        print("[C10] OK  startsWith('@') + slice(1) 候选判定不变")

    # ── D. 调用方零改 + 无回归 ──
    # [11] 调用方 <HighlightMessage content={msg.content} members={members} /> 不变
    if not re.search(r"<HighlightMessage content=\{msg\.content\} members=\{members\} />", src):
        errs.append("[D11] HighlightMessage 调用点变（应 content={msg.content} members={members} 零改）")
    else:
        print("[D11] OK  调用方 <HighlightMessage content={msg.content} members={members} /> 不变")
    # [12] 空消息兜底不变
    if "（空消息）" not in body:
        errs.append("[D12] HighlightMessage 空消息兜底「（空消息）」变")
    else:
        print("[D12] OK  空消息兜底「（空消息）」不变")

    return errs


def main() -> int:
    print("=== VH26 回归：HighlightMessage memo + 成员名稳定集降重渲染（B29）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B29 HighlightMessage 重渲染优化锁定：\n"
        "  · A memo 包裹（props content+members 浅比较短路历史气泡，流式期仅当前气泡重渲染）；\n"
        "  · B memberNames 稳定集（useMemo Set<string> agent_name+alias 去空，O(M).some → O(1).has）；\n"
        "  · C 行为零变（split 正则 + Tag 渲染 + 候选判定不变）；\n"
        "  · D 调用方零改（<HighlightMessage content members /> 不变 + 空消息兜底不变）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
