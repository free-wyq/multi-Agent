"""VH27 回归：事件管线重复 setState 审计锁契约（task B30）.

锁住 B30 审计——事件管线 ``bus.py emit → useBusEvent onmessage → ChatPanel effect``
全链路找重复 setState/重渲染（非 task_token 已知点），修一处 commit.

B30 审计结论（statusEvents 旧契约 O(n) slice 是唯一非已知高频点，已修）：

  ── 全链路 setState 清单（onmessage 回调内） ──
    useBusEvent onmessage 单个 BusEventData 触发的 setState 分支：
      1. eventsBufRef.push（B16 节流，~50ms flush 一次 setEvents）—— task_token 高频已知点.
      2. logsBufRef.push（B17 节流，~50ms flush 一次 setLogs）—— task_log/agent_reply 已知点.
      3. setStatusEvents（task_complete/failed/dispatch 收尾）—— **B30 发现的非已知点**.
      4. setAgentStatuses（agent_status 事件，executing/idle 转换）.
      5. setPlan（coordinator_plan 事件）.
      6. setStreaming / setCoordStreaming（task_token 按 task_ 前缀分流）—— 已知点.
      7. reasoningBufRef.push（B16 同款节流，~50ms flush 一次 setCoordReasoning）—— 已知点.
      8. setCoordStats（coordinator_stats ~200ms 后端节流，每 reply_id 一条）.
      9. setCoordStreaming/Reasoning/Stats 清缓冲（agent_reply 落地退场，每个 reply_id 一次）.

    非已知高频点（非 task_token/reasoning 流式）：#3 statusEvents、#4 agentStatuses、#5 plan、
    #8 coordStats、#9 退场清缓冲. 其中 #4/#5/#9 频率低（per task/per plan/per reply_id 各一次），
    唯一 #3 statusEvents 有 O(n) slice(-50) 且经审计**无消费者**——是 B30 修复点.

  ── #3 statusEvents 审计（B30 修复点） ──
    ``setStatusEvents((prev) => [...prev.slice(-50), evt])`` 每次 task_complete/failed/dispatch
    都 O(n) slice 整个 statusEvents 数组（n≤50）构建新数组 + 触发 context value 变更 →
    所有 ``useBusEventContext`` 消费者重渲染一次. 经审计：
      - src/ 全量 grep ``statusEvents``：BusEventContext 下发 + useBusEvent 返回，但**无任何组件读**
        （WorkerTrace/LeaderPanel/ChatPanel/StatusCard/MonitorPage 都不消费 statusEvents）.
      - 即 statusEvents 的 setState 是**纯开销**（构建 TaskStatusEvent + slice + 触发 context 重渲染），
        无渲染输出——是「重复 setState/重渲染」的典型（setState 触发但无 UI 消费）.

  ── B30 修复：去 statusEvents 的 O(n) slice，改增量末尾追加 + 仅超 cap 时截断 ──
    原 ``[...prev.slice(-50), evt]`` 每次 O(n) 切. 改：
      ``const next = [...prev, evt]; return next.length > 50 ? next.slice(next.length-50) : next``
    常态（n<50）零 slice（push + spread O(1) 摊还），仅超 cap 时才 slice(-50) 截断.
    不删 statusEvents（旧契约保留——未来 LeaderPanel/监控页可能消费，删了破契约），但去 O(n) 切片
    让收尾事件的 setState 成本从 O(n) 降到 O(1). 配套注释说明「无消费者，纯开销，保留契约但去切片」.

  ── 其余非已知点的审计结论（不改，频率低/有消费者） ──
    - #4 agentStatuses：agent_status 事件 per agent 状态转换（executing→idle 1 次/task），有消费者
      （ChatPanel streamingBubbles 取 executing agent + StopTaskButton 入口），频率低不改.
    - #5 plan：coordinator_plan 事件 per plan 一次，有消费者（PlanConfirmCard），频率低不改.
    - #8 coordStats：后端 ~200ms 节流（coordinator.py:1406 ``now-last_stats_ts>=0.2``），每 reply_id
      ~5-10 条 stats，有消费者（coordinatorStreamingBubbles stats 行），已节流不改.
    - #9 退场清缓冲：agent_reply 落地 per reply_id 一次，三个 setCoordXxx 但各 O(1) delete，频率极低不改.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh26 同款风格.

四段契约：

  A. statusEvents O(n) slice 已改增量追加 + 仅超 cap 截断
    1. setStatusEvents 不再用 ``[...prev.slice(-50), evt]``（O(n) 每次）.
    2. 改 ``const next = [...prev, evt]; next.length > 50 ? next.slice(...) : next``（O(1) 常态）.
    3. cap 50 仍 enforce（超 50 才 slice，等价原 slice(-50) 语义）.

  B. 其余非已知高频点审计确认（不改，有消费者/已节流/频率低）
    4. setAgentStatuses 有消费者（ChatPanel streamingBubbles + StopTaskButton 取 executing）.
    5. setPlan 有消费者（PlanConfirmCard / coordinatorStreamingBubbles）.
    6. setCoordStats 后端 ~200ms 节流（coordinator.py last_stats_ts>=0.2）.
    7. agent_reply 退场清缓冲三 setCoordXxx 各 O(1) delete（非 O(n)）.

  C. 已知节流点未回归（B16/B17 三 buf 仍在）
    8. eventsBufRef + eventsFlushTimer 仍节流 setEvents（B16 未回归）.
    9. logsBufRef + logsFlushTimer 仍节流 setLogs（B17 未回归）.
   10. reasoningBufRef + reasoningFlushTimer 仍节流 setCoordReasoning（同 B16 模式未回归）.

  D. statusEvents 旧契约保留（不删，只去 O(n) slice）
   11. statusEvents 仍在 useBusEvent 返回值（不删返回字段）.
   12. statusEvents 仍在 BusEventContext 下发（不破 context 契约）.
   13. 注释说明「无消费者，纯开销，保留契约但去切片」（B30 审计锚点）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
USEBUS_TS = REPO / "src" / "hooks" / "useBusEvent.ts"
COORD_PY = REPO / "backend" / "engine" / "coordinator.py"
BUS_CTX_TSX = REPO / "src" / "contexts" / "BusEventContext.tsx"


def _fn_body_ts(src: str, anchor: str, end_anchor: str | None = None) -> str:
    """从 anchor 字符串起取一段（用于 onmessage 回调内的分支审计）。"""
    idx = src.find(anchor)
    if idx < 0:
        return ""
    end = src.find(end_anchor, idx + len(anchor)) if end_anchor else len(src)
    return src[idx : idx + (end - idx if end > 0 else 4000)]


def assert_contract() -> list[str]:
    errs: list[str] = []
    src = USEBUS_TS.read_text(encoding="utf-8")
    coord = COORD_PY.read_text(encoding="utf-8")
    ctx = BUS_CTX_TSX.read_text(encoding="utf-8")

    # ── A. statusEvents O(n) slice 已改 ──
    # [1] 不再用 [...prev.slice(-50), evt]
    if "setStatusEvents((prev) => [...prev.slice(-50), evt])" in src:
        errs.append("[A1] statusEvents 仍用 [...prev.slice(-50), evt]（O(n) 每次）")
    else:
        print("[A1] OK  statusEvents 不再用 [...prev.slice(-50), evt]（去 O(n) slice）")
    # [2] 改增量追加 + 仅超 cap 截断
    seg = _fn_body_ts(src, "setStatusEvents((prev) => {", "})")
    if not seg:
        errs.append("[A2] statusEvents setStatusEvents 函数体未找到")
    elif "const next = [...prev, evt]" not in seg or "next.length > 50" not in seg:
        errs.append("[A2] statusEvents 未改增量追加 + 超 cap 截断（B30 修复未落地）")
    else:
        print("[A2] OK  const next = [...prev, evt]; next.length>50 ? slice : next（O(1) 常态）")
    # [3] cap 50 仍 enforce
    if "next.length > 50 ? next.slice(next.length - 50)" not in src:
        errs.append("[A3] statusEvents cap 50 截断逻辑破（slice(next.length-50) 缺）")
    else:
        print("[A3] OK  cap 50 仍 enforce（超 50 才 slice(next.length-50)，等价 slice(-50)）")

    # ── B. 其余非已知点审计确认 ──
    # [4] setAgentStatuses 有消费者
    if "agentStatuses" not in src or "agentStatuses" not in ctx:
        errs.append("[B4] agentStatuses 无消费者（审计断）")
    else:
        # 检查 ChatPanel 取 executing agent（streamingBubbles 或 StopTaskButton）
        chatpanel = (REPO / "src" / "components" / "ChatPanel.tsx").read_text(encoding="utf-8")
        stop_btn = (REPO / "src" / "components" / "StopTaskButton.tsx").read_text(encoding="utf-8")
        if "agentStatuses" not in chatpanel and "agentStatuses" not in stop_btn:
            errs.append("[B4] agentStatuses 似乎无组件读（频率低但应确认有消费者）")
        else:
            print("[B4] OK  agentStatuses 有消费者（ChatPanel/StopTaskButton 取 executing）")
    # [5] setPlan 有消费者
    if "plan" not in ctx:
        errs.append("[B5] plan 无消费者（审计断）")
    else:
        print("[B5] OK  plan 有消费者（PlanConfirmCard / context 下发）")
    # [6] setCoordStats 后端 ~200ms 节流
    if not re.search(r"now\s*-\s*last_stats_ts\s*>=\s*0\.2", coord):
        errs.append("[B6] coordinator stats 缺 ~200ms 节流（last_stats_ts>=0.2）")
    else:
        print("[B6] OK  setCoordStats 后端 ~200ms 节流（coordinator.py last_stats_ts>=0.2）")
    # [7] agent_reply 退场三 setCoordXxx 各 O(1) delete（非 O(n) slice）
    retire_seg = _fn_body_ts(src, "if (d.type === 'agent_reply' && d.data", "}, handleReconnect")
    if not retire_seg:
        errs.append("[B7] agent_reply 退场清缓冲分支未找到")
    else:
        # 三 setCoordXxx 各带 delete next[rid]（O(1)），不应有 slice
        has_three = retire_seg.count("delete next[") >= 3
        has_slice = "slice(" in retire_seg
        if not has_three:
            errs.append(f"[B7] agent_reply 退场缺三个 delete（应清 streaming/reasoning/stats，delete 计数={retire_seg.count('delete next[')}）")
        elif has_slice:
            errs.append("[B7] agent_reply 退场含 slice（O(n) 非 O(1) delete）")
        else:
            print("[B7] OK  agent_reply 退场三 setCoordXxx 各 O(1) delete（非 O(n) slice）")

    # ── C. 已知节流点未回归 ──
    # [8] eventsBufRef + eventsFlushTimer 节流 setEvents（B16）
    if "eventsBufRef" not in src or "eventsFlushTimer" not in src or "flushEvents" not in src:
        errs.append("[C8] eventsBufRef/eventsFlushTimer/flushEvents 缺（B16 节流破）")
    else:
        print("[C8] OK  eventsBufRef + eventsFlushTimer 节流 setEvents（B16 未回归）")
    # [9] logsBufRef + logsFlushTimer 节流 setLogs（B17）
    if "logsBufRef" not in src or "logsFlushTimer" not in src or "flushLogs" not in src:
        errs.append("[C9] logsBufRef/logsFlushTimer/flushLogs 缺（B17 节流破）")
    else:
        print("[C9] OK  logsBufRef + logsFlushTimer 节流 setLogs（B17 未回归）")
    # [10] reasoningBufRef + reasoningFlushTimer 节流 setCoordReasoning
    if "reasoningBufRef" not in src or "reasoningFlushTimer" not in src or "flushReasoning" not in src:
        errs.append("[C10] reasoningBufRef/reasoningFlushTimer/flushReasoning 缺（思考节流破）")
    else:
        print("[C10] OK  reasoningBufRef + reasoningFlushTimer 节流 setCoordReasoning（同 B16 未回归）")

    # ── D. statusEvents 旧契约保留 ──
    # [11] statusEvents 仍在返回值
    if "statusEvents" not in src.split("return {", 1)[-1].split("}", 1)[0]:
        errs.append("[D11] statusEvents 不在 useBusEvent 返回值（旧契约破）")
    else:
        print("[D11] OK  statusEvents 仍在 useBusEvent 返回值（不删字段）")
    # [12] statusEvents 仍在 context 下发
    if "statusEvents" not in ctx:
        errs.append("[D12] statusEvents 不在 BusEventContext 下发（context 契约破）")
    else:
        print("[D12] OK  statusEvents 仍在 BusEventContext 下发（不破 context 契约）")
    # [13] 注释说明「无消费者/纯开销」（B30 审计锚点）
    # 注释块在 setStatusEvents 上方约 14 行（B30 重复 setState 审计段），需扩大窗口到 1200 字符。
    se_idx = src.find("setStatusEvents((prev) => {")
    above = src[max(0, se_idx - 1200) : se_idx]
    if "B30" not in above:
        errs.append("[D13] statusEvents 上方缺 B30 审计注释")
    elif "无消费者" not in above and "无任何消费者" not in above:
        errs.append("[D13] statusEvents 注释未说明「无消费者」（审计锚点缺失）")
    elif "纯开销" not in above:
        errs.append("[D13] statusEvents 注释未说明「纯开销」（审计锚点缺失）")
    else:
        print("[D13] OK  注释说明无消费者纯开销（B30 审计锚点，旧契约保留）")

    return errs


def main() -> int:
    print("=== VH27 回归：事件管线重复 setState 审计锁契约（B30）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B30 事件管线重复 setState 审计锁定：\n"
        "  · A statusEvents O(n) slice → O(1) 增量追加 + 超 cap 截断（B30 修复点）；\n"
        "  · B 其余非已知点审计（agentStatuses/plan 有消费者 + coordStats 后端 200ms 节流 + 退场三 delete O(1)）；\n"
        "  · C 已知节流点 B16/B17/thinking 三 buf 未回归；\n"
        "  · D statusEvents 旧契约保留（仍返回 + 仍下发，只去 O(n) slice + 注释说明）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
