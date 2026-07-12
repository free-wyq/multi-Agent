"""验证C：思考过程逐字流式 + 自动展开/收起（协调者 & 工作者同款）.

用户要求：
  「思考也要流式输出，开始思考时 要 持续流式，思考结束 时主动关闭，
   然后流式输出正文」
  「不管是协调者还是工作者 都要这样，有呼吸有交互 用户体验 才真实」

两个缺口（本批修复）：
  1. **worker 单聊思考不流式**：worker.py node_brain_decide 的 reasoning_delta 分支
     原先只 append 进 reasoning_parts 落盘，不推 WS 事件——前端要等回复结束、定稿
     气泡落地才从 data.reasoning 一次性读出全文，思考过程是一整块、不逐字。
     对照协调者 coordinator.py L1424-1432 已在 reasoning_delta 推 emit_coordinator_reasoning。
  2. **思考折叠区默认收起**：ChatMessageBubble.tsx 的 reasoning Collapse 无 activeKey
     控制 → 默认收起，流式期思考逐字流出但用户看不见（折叠着）。需流式思考活跃时
     自动展开、思考结束（正文开始流/流式结束）自动收起。

本自测验证静态契约（不依赖后端在线，纯源码断言）：

  后端（worker.py）：
    W1. import emit_coordinator_reasoning（复用协调者同款通道）。
    W2. reasoning_delta 分支调 emit_coordinator_reasoning(group_id, agent_id, reply_id, delta)
        ——与可见正文 emit_task_token 同 reply_id 归并，前端同一流式气泡同时接收思考+正文。
    W3. best-effort（try/except 不阻断 brain 决策）——与 content_delta 推送同模式。

  后端（coordinator.py）对照：
    C1. _stream_coordinator_decision 已在 reasoning_delta 推 emit_coordinator_reasoning
        （既有实现，验证未回归）。

  前端（ChatMessageBubble.tsx）：
    F1. reasoning Collapse 受控（activeKey 由 state 派生，非默认收起的自管态）。
    F2. 流式思考活跃（isStreaming && hasReasoning）→ reasoningExpanded=true（自动展开）。
    F3. 思考结束（!reasoningActive）→ reasoningExpanded=false（自动收起，让位正文）。
    F4. 用户手动 toggle 后 5s 内不自动覆盖（尊重用户意图）。

  前端（useBusEvent.ts）对照：
    U1. coordinator_reasoning → coordReasoning[reply_id] 累加（既有，验证未回归）。
    U2. worker 单聊 task_token（reply_id）→ coordStreaming[reply_id]（既有，思考与正文同 reply_id）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKER_PY = REPO / "backend" / "engine" / "worker.py"
COORD_PY = REPO / "backend" / "engine" / "coordinator.py"
BUBBLE_TSX = REPO / "src" / "components" / "ChatMessageBubble.tsx"
HOOK_TS = REPO / "src" / "hooks" / "useBusEvent.ts"


def check() -> int:
    errs: list[str] = []
    worker = WORKER_PY.read_text(encoding="utf-8")
    coord = COORD_PY.read_text(encoding="utf-8")
    bubble = BUBBLE_TSX.read_text(encoding="utf-8")
    hook = HOOK_TS.read_text(encoding="utf-8")

    print("── 后端：worker 思考逐字流式 ──")

    # W1. import emit_coordinator_reasoning
    if "emit_coordinator_reasoning" not in worker:
        errs.append("[W1] worker.py 未 import emit_coordinator_reasoning（思考流式通道缺失）")
    else:
        print("[W1] OK  worker.py import emit_coordinator_reasoning（复用协调者同款通道）")

    # W2. reasoning_delta 分支调 emit_coordinator_reasoning(..., reply_id, reasoning_delta)
    #     B3 抽出 _stream_brain_decision 后参数走 group_id/agent_id 形参（镜像协调者
    #     _stream_coordinator_decision 用 group_id/coordinator_id），而非 state.get(...)。
    #     两种写法都接受，核心契约「reasoning_delta → emit_coordinator_reasoning(reply_id)」不变。
    m_w = re.search(
        r"if reasoning_delta:.*?await emit_coordinator_reasoning\(\s*(?:state\.get\(.group_id.*?|group_id).*?reply_id,.*?reasoning_delta",
        worker,
        re.S,
    )
    if not m_w:
        errs.append("[W2] worker.py reasoning_delta 分支未调 emit_coordinator_reasoning(..., reply_id, reasoning_delta)")
    else:
        print("[W2] OK  reasoning_delta → emit_coordinator_reasoning(..., reply_id, delta)（思考逐字推 WS）")

    # W3. best-effort（try/except 包 emit，失败不阻断）
    #     reasoning_delta 块内含 try: ... except Exception: logger.exception
    #     B3 抽出 _stream_brain_decision 后块缩进从 3 级变 2 级（类方法→模块级函数体），
    #     块边界也不再以 `if usage is not None` 为后界（_stream_brain_decision 内顺序为
    #     content/reasoning/usage，reasoning 块后界改用 `if usage is not None` 仍可，但缩进
    #     改变故放宽到下一个同级 if）。两种缩进都接受。
    w_block = re.search(
        r"if reasoning_delta:\s*\n(.*?)(?=\n        if usage is not None:|\n            if usage is not None:)",
        worker,
        re.S,
    )
    if not w_block:
        errs.append("[W3] 无法定位 worker reasoning_delta 块")
    else:
        blk = w_block.group(1)
        if "try:" not in blk or "except Exception" not in blk:
            errs.append("[W3] reasoning_delta 推送未 try/except（WS 推送失败会阻断 brain 决策）")
        else:
            print("[W3] OK  reasoning_delta 推送 best-effort（try/except 不阻断 brain 决策）")

    print("\n── 后端：coordinator 思考逐字流式（对照，验证未回归）──")

    # C1. coordinator _stream_coordinator_decision 已推 emit_coordinator_reasoning
    if "emit_coordinator_reasoning" not in coord:
        errs.append("[C1] coordinator.py 未推 emit_coordinator_reasoning（回归！原已实现）")
    else:
        # 定位 reasoning_delta 分支含 emit_coordinator_reasoning
        m_c = re.search(
            r"if reasoning_delta:.*?emit_coordinator_reasoning\(\s*group_id,\s*coordinator_id,\s*reply_id,\s*reasoning_delta\s*\)",
            coord,
            re.S,
        )
        if not m_c:
            errs.append("[C1] coordinator.py reasoning_delta 分支 emit_coordinator_reasoning 调用结构异常（回归？）")
        else:
            print("[C1] OK  coordinator.py reasoning_delta → emit_coordinator_reasoning（未回归）")

    print("\n── 前端：思考折叠区自动展开/收起 ──")

    # F1. reasoning Collapse 受控（activeKey 由 state 派生）
    if "activeKey={reasoningExpanded" not in bubble:
        errs.append("[F1] ChatMessageBubble reasoning Collapse 未受控 activeKey（仍是默认收起自管态）")
    else:
        print("[F1] OK  reasoning Collapse activeKey 受控（reasoningExpanded state 派生）")

    # F2. 流式思考活跃 → 展开（reasoningActive = isStreaming && hasReasoning && !hasContent）
    #     精确信号：思考活跃 = 流式中 + 有推理 + 正文尚未开始流。正文一开始流（hasContent）
    #     即标志思考结束 → 收起。非推理模型无 reasoning → reasoningActive 恒 false → 从不展开。
    if "reasoningActive" not in bubble:
        errs.append("[F2] 缺少 reasoningActive 派生信号（isStreaming && hasReasoning && !hasContent）")
    else:
        # 派生公式含三项
        m_f2 = re.search(
            r"reasoningActive\s*=\s*isStreaming\s*&&\s*hasReasoning\s*&&\s*!hasContent",
            bubble,
        )
        if not m_f2:
            errs.append("[F2] reasoningActive 派生不符（应为 isStreaming && hasReasoning && !hasContent）")
        else:
            # 展开逻辑：reasoningActive 时 setReasoningExpanded(true)
            m_f2b = re.search(
                r"if \(!reasoningActive\).*?setReasoningExpanded\(false\).*?return.*?setReasoningExpanded\(true\)",
                bubble,
                re.S,
            )
            if not m_f2b:
                errs.append("[F2] 流式思考活跃未自动展开（!reasoningActive→收起 / 否则→展开 逻辑缺失）")
            else:
                print("[F2] OK  思考活跃（isStreaming && hasReasoning && !hasContent）→ 自动展开")

    # F3. 思考结束 → 收起（!reasoningActive → setReasoningExpanded(false)）
    if "setReasoningExpanded(false)" not in bubble:
        errs.append("[F3] 缺少思考结束自动收起（!reasoningActive → setReasoningExpanded(false)）")
    else:
        print("[F3] OK  思考结束（!reasoningActive）→ 自动收起（让位正文）")

    # F4. 用户手动 toggle 后 5s 内不自动覆盖
    if "userToggledAt" not in bubble:
        errs.append("[F4] 缺少 userToggledAt 手动操作保护（用户 toggle 后会被自动覆盖）")
    elif "Date.now() - userToggledAt < 5000" not in bubble:
        errs.append("[F4] 手动操作保护窗口不符（应为 5s）")
    else:
        print("[F4] OK  用户手动 toggle 后 5s 内不自动覆盖（尊重用户意图）")

    print("\n── 前端：思考与正文同 reply_id 归并（对照，验证未回归）──")

    # U1. coordinator_reasoning → coordReasoning[reply_id]
    if "coordinator_reasoning" not in hook or "setCoordReasoning" not in hook:
        errs.append("[U1] useBusEvent 未处理 coordinator_reasoning → coordReasoning（回归）")
    else:
        print("[U1] OK  coordinator_reasoning → coordReasoning[reply_id] 累加（未回归）")

    # U2. worker 单聊 task_token（reply_id 无 task_ 前缀）→ coordStreaming[reply_id]
    if "key.startsWith('task_')" not in hook:
        errs.append("[U2] useBusEvent task_token 未按 task_ 前缀分流（回归）")
    else:
        print("[U2] OK  worker 单聊 task_token（裸 reply_id）→ coordStreaming[reply_id]（思考与正文同 reply_id）")

    print()
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print(
        "思考逐字流式 + 自动展开/收起（协调者 & 工作者同款）：\n"
        "  · 后端 worker.py: reasoning_delta → emit_coordinator_reasoning(reply_id) 推逐字增量（best-effort）；\n"
        "  · 后端 coordinator.py: 既有 reasoning_delta → emit_coordinator_reasoning（未回归）；\n"
        "  · 前端 ChatMessageBubble: 思考流式活跃→自动展开，思考结束→自动收起，手动 toggle 后 5s 不覆盖；\n"
        "  · 前端 useBusEvent: coordinator_reasoning→coordReasoning[reply_id] + worker task_token→coordStreaming[reply_id]，"
        "思考与正文同 reply_id 归并进同一流式气泡。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(check())
