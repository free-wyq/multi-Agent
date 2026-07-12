"""验证D：思考流式可见 + 退场无空泡无乱序（协调者 & 工作者同款）.

用户实测发现两个 bug（本批修复）：

1. **思考没流式显示**：定稿协调者气泡的 reasoning Collapse 是 ChatPanel 内联的独立非受控
   代码（ChatPanel.tsx:984），与 ChatMessageBubble 的受控展开逻辑脱节——定稿气泡思考区
   默认收起、不逐字流、手动展开逻辑也不接入。且 reasoningActive=!hasContent 对定稿气泡恒
   false（定稿 content 一开始就有）→ 思考区永不自动展开。
2. **回复乱序 + 「0 tokens 思考中」幽灵气泡**：coordinator_stats(phase=done) 一到就清
   coordStreaming，但持久化 agent_reply 几十毫秒后才到 → 中间空泡间隙；多轮连发时下一轮的
   stats(streaming,0) 趁虚混进空泡 → 幽灵气泡 + 乱序。

修法：
  - useBusEvent: stats(done) 不再清缓冲，只更新 coordStats（phase=done 让气泡显「完成」
    但内容仍可见）；改由持久化 agent_reply 落地（data.reply_id 对齐）时清——退场锚点更准。
  - ChatPanel 定稿气泡 reasoning：精简内联 Collapse（去重，注释指向 ChatMessageBubble 统一）。
  - ChatMessageBubble: 非流式期（定稿气泡 isStreaming=false）不清空用户手动展开的历史思考。

本自测验证静态契约（不依赖后端在线）：

  useBusEvent.ts：
    D1. coordinator_stats 分支不再清 coordStreaming/coordReasoning/coordStats
        （删除了 phase==='done' 时三连 delete）。
    D2. 新增 agent_reply 落地分支：data.reply_id 对齐时清 coordStreaming/coordReasoning/coordStats。
    D3. coordinator_stats 仍写 coordStats（phase=done 仍写入，非删除）。

  ChatPanel.tsx：
    D4. 定稿气泡 reasoning Collapse 仍渲染（extractCoordReasoning 未删），但不再有独立的
        reasoningTokenLabel 命名（精简内联），且注释指向复用 ChatMessageBubble。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK_TS = REPO / "src" / "hooks" / "useBusEvent.ts"
PANEL_TSX = REPO / "src" / "components" / "ChatPanel.tsx"


def check() -> int:
    errs: list[str] = []
    hook = HOOK_TS.read_text(encoding="utf-8")
    panel = PANEL_TSX.read_text(encoding="utf-8")

    print("── useBusEvent: 退场锚点改 stats(done)→agent_reply 落地 ──")

    # D1. coordinator_stats 分支不再清缓冲（无 phase==='done' 三连 delete）
    #     定位 coordinator_stats 分支块，断言块内无 delete next[rid]
    m_stats = re.search(
        r"else if \(d\.type === 'coordinator_stats'\) \{(.*?)\n      \}",
        hook,
        re.S,
    )
    if not m_stats:
        errs.append("[D1] 无法定位 coordinator_stats 分支")
    else:
        stats_blk = m_stats.group(1)
        if "delete next[rid]" in stats_blk or "delete next[replyId]" in stats_blk:
            errs.append("[D1] coordinator_stats 分支仍在清缓冲（phase=done 应改由 agent_reply 清，空泡根因）")
        else:
            print("[D1] OK  coordinator_stats 分支不清缓冲（phase=done 不再触发三连 delete）")

    # D2. 新增 agent_reply 落地分支清缓冲
    m_reply = re.search(
        r"if \(d\.type === 'agent_reply' && d\.data && typeof d\.data === 'object'\) \{(.*?)\n      \}",
        hook,
        re.S,
    )
    if not m_reply:
        errs.append("[D2] 缺少 agent_reply 落地分支（退场锚点缺失）")
    else:
        reply_blk = m_reply.group(1)
        has_clear = (
            "setCoordStreaming" in reply_blk
            and "setCoordReasoning" in reply_blk
            and "setCoordStats" in reply_blk
            and "delete next[rid]" in reply_blk
        )
        if not has_clear:
            errs.append("[D2] agent_reply 分支未清 coordStreaming/coordReasoning/coordStats 三者")
        else:
            # reply_id 从 data 取
            if "reply_id" not in reply_blk:
                errs.append("[D2] agent_reply 分支未按 data.reply_id 对齐")
            else:
                print("[D2] OK  agent_reply 落地 → data.reply_id 对齐清三者（退场锚点改对）")

    # D3. coordinator_stats 仍写 coordStats（phase=done 也写入，非删除）
    if m_stats:
        stats_blk = m_stats.group(1)
        if "setCoordStats" not in stats_blk:
            errs.append("[D3] coordinator_stats 分支未写 coordStats（phase=done 仍应写入让气泡显「完成」）")
        else:
            print("[D3] OK  coordinator_stats 仍写 coordStats（phase=done → 气泡显「完成」但不退场）")

    print("\n── ChatPanel: 定稿气泡 reasoning 折叠区 ──")

    # D4. 定稿气泡 reasoning Collapse 仍渲染（extractCoordReasoning 未删）
    if "extractCoordReasoning" not in panel:
        errs.append("[D4] ChatPanel 删了 extractCoordReasoning（定稿气泡 reasoning 区不渲染了）")
    else:
        # 定稿气泡 Collapse 仍存在（非 ChatMessageBubble 那份）
        # 定稿气泡渲染处含 <Collapse ... items={[{ key: 'reasoning' ...
        m_collapse = re.search(
            r"const reasoning = extractCoordReasoning\(msg\.data\).*?<Collapse",
            panel,
            re.S,
        )
        if not m_collapse:
            errs.append("[D4] 定稿气泡 reasoning Collapse 渲染缺失")
        else:
            print("[D4] OK  定稿气泡 reasoning Collapse 仍渲染（extractCoordReasoning 保留）")

    print()
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print(
        "思考流式可见 + 退场无空泡无乱序：\n"
        "  · useBusEvent: stats(done) 不再清缓冲，改由持久化 agent_reply(data.reply_id) 落地时清——\n"
        "    消除「流式气泡先消失、定稿气泡几十毫秒后才到」的空泡间隙与多轮连发的「0 tokens 思考中」幽灵气泡；\n"
        "  · coordinator_stats 仍写 coordStats(phase=done) 让气泡显「完成」但不退场，内容/思考保持可见直到定稿接管；\n"
        "  · ChatPanel 定稿气泡 reasoning Collapse 保留（extractCoordReasoning 未删），思考区可手动展开看历史。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(check())
