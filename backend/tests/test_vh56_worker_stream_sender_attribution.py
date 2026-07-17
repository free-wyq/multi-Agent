"""VH56 回归：worker 流式归属正确（Bug A：senderId 穿透到渲染层）.

锁住「群主(协调者) 思考过程」气泡里装 worker 推理的缺陷修复——worker 单聊/脑回路
流式（task_token reply_id 裸 hex）被错误渲染在硬编码「群主(协调者)」头像/名下。
根因：后端事件已带正确 sender_id（emit_task_token sender_id=agent_id；coordinator_token
sender_id=coordinator_id），但前端 coordStreaming 只存 content 字符串、渲染层硬编码
coordinator id/name/avatar——事件带的 sender_id 没用上。

修复（方案 A2 渲染层）：coordStreaming 从 ``Record<string, string>`` 改为
``Record<string, { content: string; senderId: string }>`，两处累积点（task_token /
coordinator_token）从 ``d.sender_id`` 穿 senderId，渲染层用 ``b.senderId`` 解析头像/名，
coordinator_id 回退「群主(协调者)」、其他查 agents 取 worker 名。

五段契约（纯静态，读 useBusEvent.ts + BusEventContext.tsx + ChatPanel.tsx 源码）：

  A. coordStreaming 类型带 senderId
    1. useBusEvent.ts coordStreaming useState 类型是 ``{ content: string; senderId: string }``
       （非裸 string）。
    2. BusEventContext.tsx coordStreaming 字段类型同样带 senderId（context 透传类型对齐）。

  B. 累积点穿 senderId
    3. task_token → coordStreaming 分支（裸 hex reply_id）从 ``d.sender_id`` 取 senderId.
    4. coordinator_token → coordStreaming 分支从 ``d.sender_id`` 取 senderId.

  C. 渲染层用 senderId（非硬编码 coordinator）
    5. coordinatorStreamingBubbles map 出来的条目含 senderId（非仅 content）.
    6. ``<ChatMessageBubble senderId={b.senderId}``（非 ``group?.coordinator_id ?? 'coordinator'``）.
    7. ``<ChatAvatar id={b.senderId}``（非硬编码 coordinator id）.
    8. senderName 解析用 ``b.senderId``（查 agents），coordinator_id 回退「群主(协调者)」.

  D. 不回归——分流逻辑/渲染结构不变（保 vb3 契约）
    9. task_token 仍按 ``task_`` 前缀分流（task_→streaming / else→coordStreaming）.
   10. coordinatorStreamingBubbles 仍遍历 coordStreaming 渲染 ChatMessageBubble（coord-streaming- key）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
USE_BUS_EVENT = SRC / "hooks" / "useBusEvent.ts"
BUS_CTX = SRC / "contexts" / "BusEventContext.tsx"
CHAT_PANEL = SRC / "components" / "ChatPanel.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def assert_contract() -> list[str]:
    errs: list[str] = []
    hook = _read(USE_BUS_EVENT)
    ctx = _read(BUS_CTX)
    panel = _read(CHAT_PANEL)

    # ── A. coordStreaming 类型带 senderId ───────────────────────
    # A1 useBusEvent coordStreaming useState 类型含 { content; senderId }
    m_state = re.search(
        r"coordStreaming.*?Record<string,\s*\{[^}]*content[^}]*senderId",
        hook,
        re.S,
    )
    if not m_state:
        errs.append("[A1] useBusEvent coordStreaming useState 类型未带 { content; senderId }（仍是裸 string）")
    else:
        print("[A1] OK  coordStreaming useState 类型带 { content; senderId }")

    # A2 BusEventContext coordStreaming 字段类型带 senderId
    m_ctx = re.search(
        r"coordStreaming:\s*Record<string,\s*\{[^}]*content[^}]*senderId[^}]*\}",
        ctx,
        re.S,
    )
    if not m_ctx:
        errs.append("[A2] BusEventContext coordStreaming 字段类型未带 senderId（透传类型未对齐）")
    else:
        print("[A2] OK  BusEventContext coordStreaming 字段类型带 senderId（透传对齐）")

    # ── B. 累积点穿 senderId ────────────────────────────────────
    # B3 task_token → coordStreaming 分支从 d.sender_id 取 senderId
    # 截取 task_token 分支块（从 'task_token' 到下一个同级 type 分支）
    tt_block = _extract_block(hook, "d.type === 'task_token'")
    if "d.sender_id" not in tt_block:
        errs.append("[B3] task_token→coordStreaming 分支未从 d.sender_id 取 senderId")
    elif "senderId" not in tt_block:
        errs.append("[B3] task_token→coordStreaming 分支未写 senderId 字段")
    else:
        print("[B3] OK  task_token→coordStreaming 从 d.sender_id 穿 senderId（worker agent_id）")

    # B4 coordinator_token → coordStreaming 分支从 d.sender_id 取 senderId
    ct_block = _extract_block(hook, "d.type === 'coordinator_token'")
    if "d.sender_id" not in ct_block:
        errs.append("[B4] coordinator_token→coordStreaming 分支未从 d.sender_id 取 senderId")
    elif "senderId" not in ct_block:
        errs.append("[B4] coordinator_token→coordStreaming 分支未写 senderId 字段")
    else:
        print("[B4] OK  coordinator_token→coordStreaming 从 d.sender_id 穿 senderId（coordinator_id）")

    # ── C. 渲染层用 senderId（非硬编码）──────────────────────────
    # C5 coordinatorStreamingBubbles map 条目含 senderId
    bubbles_block = _extract_block(panel, "coordinatorStreamingBubbles")
    if "senderId" not in bubbles_block:
        errs.append("[C5] coordinatorStreamingBubbles map 条目未含 senderId（仅 content）")
    else:
        print("[C5] OK  coordinatorStreamingBubbles 条目含 senderId")

    # C6 <ChatMessageBubble senderId={senderId}（来自 b.senderId，非硬编码 coordinator）
    # 渲染块：coordinatorStreamingBubbles.map((b) => { ... <ChatMessageBubble
    render_block = _extract_map_block(panel, "coordinatorStreamingBubbles")
    # senderId 经 const senderId = b.senderId 间接传入 senderId={senderId}
    has_sender_from_b = re.search(r"const\s+senderId\s*=\s*b\.senderId", render_block) is not None
    has_sender_prop = re.search(r"senderId=\{senderId\}", render_block) is not None
    has_hardcoded = "senderId={group?.coordinator_id ?? 'coordinator'}" in render_block
    if not (has_sender_from_b and has_sender_prop) or has_hardcoded:
        errs.append("[C6] ChatMessageBubble senderId 未用 b.senderId（仍硬编码 group?.coordinator_id）")
    else:
        print("[C6] OK  ChatMessageBubble senderId={senderId}（来自 b.senderId，非硬编码 coordinator）")

    # C7 <ChatAvatar id={senderId}（来自 b.senderId，非硬编码 coordinator）
    has_avatar_from_sender = re.search(r"<ChatAvatar\s+id=\{senderId\}", render_block) is not None
    if not has_avatar_from_sender:
        errs.append("[C7] ChatAvatar id 未用 b.senderId（仍硬编码 coordinator id）")
    else:
        print("[C7] OK  ChatAvatar id={senderId}（来自 b.senderId，非硬编码 coordinator id）")

    # C8 senderName 解析用 b.senderId（查 agents，coordinator_id 回退「群主(协调者)」）
    has_name_from_sender = re.search(r"senderName\s*=\s*senderId\s*===", render_block) is not None
    if not (has_name_from_sender and "agents.find" in render_block):
        errs.append("[C8] senderName 解析未用 b.senderId（仍硬编码「群主(协调者)」）")
    else:
        print("[C8] OK  senderName 用 b.senderId 查 agents（coordinator_id 回退「群主(协调者)」）")

    # ── D. 不回归（保 vb3 契约）──────────────────────────────────
    # D9 task_token 仍按 task_ 前缀分流
    if "key.startsWith('task_')" not in hook:
        errs.append("[D9] useBusEvent task_token 未按 'task_' 前缀分流（vb3 回归）")
    else:
        print("[D9] OK  task_token 仍按 task_ 前缀分流（vb3 不回归）")

    # D10 coordinatorStreamingBubbles 仍遍历 coordStreaming 渲染 ChatMessageBubble
    if "Object.entries(coordStreaming)" not in panel:
        errs.append("[D10] coordinatorStreamingBubbles 未遍历 coordStreaming（vb3 回归）")
    elif "coord-streaming-" not in panel:
        errs.append("[D10] 流式气泡未用 coord-streaming- key（vb3 回归）")
    else:
        print("[D10] OK  coordinatorStreamingBubbles 仍遍历 coordStreaming 渲染（vb3 不回归）")

    return errs


def _extract_block(src: str, marker: str) -> str:
    """粗略截取含 marker 的代码块（marker 行到下一个同级结构）."""
    idx = src.find(marker)
    if idx < 0:
        return ""
    # 取 marker 后 1200 字符作为块（足够覆盖单分支逻辑）
    return src[idx : idx + 1200]


def _extract_map_block(src: str, list_name: str) -> str:
    """截取 ``{list_name}.map((b) => { ... })`` 的渲染块."""
    m = re.search(rf"\{{{list_name}\.map\(\(b\) => \{{(.+?)(?:\}}\)\}}|\Z)", src, re.S)
    return m.group(1) if m else src[src.find(f"{list_name}.map") : src.find(f"{list_name}.map") + 2500]


def main() -> int:
    print("=== VH56 回归：worker 流式归属正确（Bug A：senderId 穿透到渲染层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "worker 流式归属锁定：\n"
        "  · A coordStreaming 类型带 { content; senderId }（useBusEvent + BusEventContext）；\n"
        "  · B task_token/coordinator_token 两累积点从 d.sender_id 穿 senderId；\n"
        "  · C 渲染层 senderId/avatar/senderName 用 b.senderId（非硬编码 coordinator）；\n"
        "  · D 分流逻辑/渲染结构不回归（vb3 契约保绿）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
