"""VH14 回归：useBusEvent logs 批量 flush + ChatPanel 增量桥接（task B17）.

锁住 B17 修复——``src/hooks/useBusEvent.ts`` 原 ``setLogs((prev) =>
[...prev.slice(-200), entry])`` 每事件 O(n) 切片，同 B16 问题：

  - 高频场景：task_log（agent stdout）突发——chatty agent 跑构建脚本 1s 内打印数十行，
    每行 emit_task_log → setLogs O(200) 切片 + 触发 ChatPanel 桥接 effect（logs 是
    effect 依赖）。task_token/coordinator_token 等逐字 delta 虽经 VF/c32de07 源头
    排除出 logs，但 task_log 不在排除列表（它是真实日志，该进 LogPanel + 成气泡）。
  - 原 slice(-200) 每事件复制 200 元素数组，数十行突发 → 数十 × O(200) + 数十次
    setState + 数十次 ChatPanel 重渲染。

B17 改法（同 B16 批量 flush 模式）：
  - entry 攒进 ``logsBufRef``（ref，不触发渲染），~50ms flush 一次到 state。
  - flush 时统一 enforce cap 200：``prev.concat(buf)`` 超 200 取末尾 200。
  - 把数十次 setLogs 压到数次。定时器兜底 flush + effect 清理 flush 双保险，不丢日志。

配套 ChatPanel 桥接改造（B17 必须同步改，否则回归）：
  - 旧桥接 effect「只取 logs[logs.length-1]」是逐条触发时的旧契约——logs 每变一条
    桥接最后一条。批量 flush 后单次 logs 变化可能含多条新 entry（~50ms 聚合多条），
    旧「只取最后一条」会丢同批更早的 task_log/agent_reply 气泡（回归）。
  - 改为遍历新增尾部（logsLenRef 增量游标）：本次只处理 logs[prevLen..]，靠 wsMsgId
    去重（setChatMessages prev.some + spokenIdsRef 防 TTS 重读），已桥接过的 id 跳过。
  - prevLen > logs.length（重连回灌 logs 较短 / 切群重置）从 0 重扫更稳——重灌历史
    id 不变，wsMsgId 去重跳过已桥接的，不重复加气泡。
  - 切群时重置 logsLenRef.current = 0（新群 logs 与旧群无关，防漏桥接/错位）。

行为零变约束：
  - logs 仍是 LogEntry[] cap 200（cap 数不变，超 200 取末尾 200）。
  - 元素顺序不变（buf 内 push 顺序 = 到达顺序，concat 末尾保留时序）。
  - 桥接仍是「白名单 type + wsMsgId 去重 + spokenIdsRef 防 TTS 重读」——气泡数量不变，
    只是处理时机从逐条变增量批次。
  - ChatPanel 桥接 effect 仍依赖 logs（仍是 chatMessages 来源），LogPanel 仍 logs.filter。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh13 同款风格。

七段契约：

  A. logs 批量 flush 真源（ref + 定时器 + flush 函数）
    1. ``logsBufRef`` ref 定义（LogEntry[] 缓冲载体）。
    2. ``logsFlushTimer`` ref 定义（定时器句柄）。
    3. ``flushLogs`` useCallback 定义（节流 flush 函数）。

  B. entry 入队 ref 而非直推 state（原 slice(-200) 消失）
    4. LogEntry 构造后 ``logsBufRef.current.push(entry)``（入队 ref）。
    5. 不再有 ``setLogs((prev) => [...prev.slice(-200), entry])``（原 O(n) slice 消失）。
    6. 入队后起/复用 ``logsFlushTimer``（setTimeout ~50ms 调 flushLogs）。

  C. cap 200 在 flush 时统一 enforce（行为零变）
    7. flushLogs 内 ``prev.concat(buf)`` 合并。
    8. ``merged.length <= 200`` 分支返回 merged（未超 cap 直用）。
    9. 超分支返回 ``merged.slice(merged.length - 200)``（取末尾 200，等价 slice(-200)）。

  D. 定时器兜底 + effect 清理 flush（不丢日志）
   10. flushLogs 开头置 ``logsFlushTimer.current = null``（防重复触发）。
   11. effect 清理块含 ``clearTimeout(logsFlushTimer.current)`` + 置 null。
   12. effect 清理块含 ``if (logsBufRef.current.length > 0) flushLogs()``（残留兜底）。

  E. ChatPanel 桥接遍历新增尾部（替代「只取最后一条」旧契约）
   13. 不再有 ``const lastLog = logs[logs.length - 1]``（旧「只取最后一条」消失）。
   14. 桥接 effect 用 ``logsLenRef`` 增量游标（useRef<0> + prevLen/logs.length）。
   15. 遍历 ``for (let i = start; i < logs.length; i++)`` 处理新增尾部（非只取末尾）。
   16. prevLen > logs.length 时 ``start = 0``（重扫，重连回灌/切群重置兜底）。
   17. 切群 effect 重置 ``logsLenRef.current = 0``（新群游标重置防错位）。

  F. 行为零变（去重 + TTS 不回归）
   18. 桥接仍 ``wsMsgId`` 去重（setChatMessages prev.some(m.id === wsMsgId)）。
   19. 桥接仍 ``spokenIdsRef`` 防 TTS 重读（agent_reply 朗读前查集合）。
   20. 桥接仍 CHAT_MESSAGE_TYPES 白名单过滤（非白名单 type 跳过）。

  G. 无回归（B16 events 节流不破 + VF reasoning 节流不破 + 依赖数组含 flushLogs）
   21. ``flushEvents`` / ``eventsBufRef`` / ``eventsFlushTimer`` 仍存在（B16 不破）。
   22. ``flushReasoning`` / ``reasoningBufRef`` / ``reasoningFlushTimer`` 仍存在（VF 不破）。
   23. WS effect 依赖数组含 ``flushLogs``（与 flushEvents/flushReasoning 并列）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK_TS = REPO / "src" / "hooks" / "useBusEvent.ts"
PANEL_TS = REPO / "src" / "components" / "ChatPanel.tsx"


def _fn_body_ts(src: str, fname: str) -> str:
    """抽 TS useCallback 函数体（同 vh13）。"""
    m = re.search(
        rf"const {fname}\s*=\s*useCallback\(\(\)\s*=>\s*\{{(.*?)\n\s*\}},\s*\[\]\s*\)",
        src,
        re.S,
    )
    return m.group(1) if m else ""


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（防注释引用被误判为代码字面量，B16/vh13 坑延续）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    hook = HOOK_TS.read_text(encoding="utf-8")
    panel = PANEL_TS.read_text(encoding="utf-8")
    hook_nc = _strip_ts_comments(hook)
    panel_nc = _strip_ts_comments(panel)

    # ── A. logs 批量 flush 真源 ──
    # [1] logsBufRef ref 定义
    if not re.search(r"const\s+logsBufRef\s*=\s*useRef<LogEntry\[\]>", hook):
        errs.append("[A1] 缺 logsBufRef ref（LogEntry[] 缓冲载体缺失）")
    else:
        print("[A1] OK  logsBufRef ref 定义（LogEntry[] 缓冲载体）")
    # [2] logsFlushTimer ref 定义
    if not re.search(r"const\s+logsFlushTimer\s*=\s*useRef<ReturnType<typeof setTimeout>\s*\|\s*null>", hook):
        errs.append("[A2] 缺 logsFlushTimer ref（定时器句柄缺失）")
    else:
        print("[A2] OK  logsFlushTimer ref 定义（定时器句柄）")
    # [3] flushLogs useCallback 定义
    if not re.search(r"const\s+flushLogs\s*=\s*useCallback", hook):
        errs.append("[A3] 缺 flushLogs useCallback（节流 flush 函数缺失）")
    else:
        print("[A3] OK  flushLogs useCallback 定义（节流 flush 函数）")

    # ── B. entry 入队 ref 而非直推 state ──
    # [4] LogEntry 构造后 logsBufRef.current.push(entry)
    if "logsBufRef.current.push(entry)" not in hook:
        errs.append("[B4] entry 未入队 logsBufRef.current.push(entry)（B17 未改批量缓冲）")
    else:
        print("[B4] OK  entry 入队 logsBufRef.current.push(entry)（ref 缓冲，不直推 state）")
    # [5] 不再有 setLogs((prev) => [...prev.slice(-200), entry])（剔注释后断言）
    if re.search(r"setLogs\(\(prev\)\s*=>\s*\[\.\.\.prev\.slice\(-200\),\s*entry\]\)", hook_nc):
        errs.append("[B5] 仍 setLogs((prev) => [...prev.slice(-200), entry])（原 O(n) slice 未消除）")
    else:
        print("[B5] OK  原 setLogs((prev) => [...prev.slice(-200), entry]) 已消除（O(n) slice 不再每事件触发）")
    # [6] 入队后起/复用 logsFlushTimer（setTimeout ~50ms 调 flushLogs）
    m_timer = re.search(
        r"logsBufRef\.current\.push\(entry\)\s*\n\s*if\s*\(!logsFlushTimer\.current\)\s*\{\s*"
        r"logsFlushTimer\.current\s*=\s*window\.setTimeout\(\(\)\s*=>\s*\{\s*flushLogs\(\)\s*\},\s*\d+\s*\)",
        hook,
        re.S,
    )
    if not m_timer:
        errs.append("[B6] entry 入队后未起/复用 logsFlushTimer（setTimeout flush 链断）")
    else:
        delay = re.search(r"\},\s*(\d+)\s*\)", m_timer.group(0))
        dval = delay.group(1) if delay else "?"
        print(f"[B6] OK  entry 入队 → setTimeout(~{dval}ms) → flushLogs（节流链完整）")

    # ── C. cap 200 在 flush 时统一 enforce ──
    flush_body = _fn_body_ts(hook, "flushLogs")
    if not flush_body:
        errs.append("[setup] flushLogs 函数体未找到")
    else:
        # [7] prev.concat(buf) 合并
        if "concat(buf)" not in flush_body:
            errs.append("[C7] flushLogs 未 prev.concat(buf)（ref 批次未追加到 state 末尾）")
        else:
            print("[C7] OK  prev.concat(buf) 合并（ref 批次追加到 state 末尾）")
        # [8] merged.length <= 200 分支
        if not re.search(r"merged\.length\s*<=\s*200", flush_body):
            errs.append("[C8] flushLogs 缺 merged.length <= 200 分支（未超 cap 直用）")
        else:
            print("[C8] OK  merged.length <= 200 分支（未超 cap 直用 merged）")
        # [9] 超分支返回 merged.slice(merged.length - 200)
        if not re.search(r"merged\.slice\(merged\.length\s*-\s*200\)", flush_body):
            errs.append("[C9] flushLogs 缺 merged.slice(merged.length - 200)（超 cap 未取末尾 200）")
        else:
            print("[C9] OK  merged.slice(merged.length - 200)（超 cap 取末尾 200，等价 slice(-200)）")

    # ── D. 定时器兜底 + effect 清理 flush ──
    # [10] flushLogs 开头置 logsFlushTimer.current = null
    if not re.search(r"flushLogs\s*=\s*useCallback\(\(\)\s*=>\s*\{\s*logsFlushTimer\.current\s*=\s*null", hook):
        errs.append("[D10] flushLogs 开头未置 logsFlushTimer.current = null（防重复触发缺失）")
    else:
        print("[D10] OK  flushLogs 开头置 logsFlushTimer.current = null（防重复触发）")
    cleanup_blk = hook.split("return () =>", 1)[-1] if "return () =>" in hook else ""
    # [11] effect 清理块含 clearTimeout(logsFlushTimer.current)
    if "clearTimeout(logsFlushTimer.current)" not in cleanup_blk:
        errs.append("[D11] effect 清理未 clearTimeout(logsFlushTimer.current)（切群定时器残留）")
    else:
        print("[D11] OK  effect 清理 clearTimeout(logsFlushTimer.current)（切群清定时器）")
    # [12] effect 清理块含 if (logsBufRef.current.length > 0) flushLogs()
    if "logsBufRef.current.length > 0" not in cleanup_blk or "flushLogs()" not in cleanup_blk:
        errs.append("[D12] effect 清理未兜底 flushLogs（切群残留 entry 可能丢）")
    else:
        print("[D12] OK  effect 清理 if (logsBufRef.current.length > 0) flushLogs()（残留兜底）")

    # ── E. ChatPanel 桥接遍历新增尾部 ──
    # [13] 不再有 const lastLog = logs[logs.length - 1]（剔注释后断言）
    if re.search(r"const\s+lastLog\s*=\s*logs\[logs\.length\s*-\s*1\]", panel_nc):
        errs.append("[E13] ChatPanel 仍 const lastLog = logs[logs.length-1]（旧「只取最后一条」未改）")
    else:
        print("[E13] OK  旧「const lastLog = logs[logs.length-1]」已消除（不再只取最后一条）")
    # [14] 桥接 effect 用 logsLenRef 增量游标
    if not re.search(r"const\s+logsLenRef\s*=\s*useRef<number>\(0\)|const\s+logsLenRef\s*=\s*useRef\(0\)", panel):
        errs.append("[E14] ChatPanel 缺 logsLenRef 增量游标（B17 桥接未用增量）")
    else:
        print("[E14] OK  logsLenRef 增量游标定义（替代「只取最后一条」）")
    # [15] 遍历 for (let i = start; i < logs.length; i++)
    if not re.search(r"for\s*\(\s*let\s+i\s*=\s*start\s*;\s*i\s*<\s*logs\.length\s*;\s*i\+\+\s*\)", panel):
        errs.append("[E15] ChatPanel 桥接未遍历新增尾部 for (let i = start; i < logs.length; i++)")
    else:
        print("[E15] OK  遍历新增尾部 for (let i = start; i < logs.length; i++)（批量处理新增 entry）")
    # [16] prevLen > logs.length 时 start = 0（重扫兜底）
    if not re.search(r"prevLen\s*>\s*logs\.length\s*\?\s*0\s*:\s*prevLen", panel):
        errs.append("[E16] ChatPanel 缺 prevLen > logs.length ? 0 : prevLen（重连回灌/切群重置兜底缺失）")
    else:
        print("[E16] OK  prevLen > logs.length ? 0 : prevLen（重连回灌/切群重置从 0 重扫）")
    # [17] 切群 effect 重置 logsLenRef.current = 0
    if not re.search(r"logsLenRef\.current\s*=\s*0", panel):
        errs.append("[E17] ChatPanel 切群 effect 未重置 logsLenRef.current = 0（新群游标错位）")
    else:
        print("[E17] OK  切群 effect 重置 logsLenRef.current = 0（新群游标重置）")

    # ── F. 行为零变（去重 + TTS 不回归）──
    # [18] 桥接仍 wsMsgId 去重（prev.some(m.id === wsMsgId)）
    if not re.search(r"prev\.some\(\(m\)\s*=>\s*m\.id\s*===\s*wsMsgId\)", panel):
        errs.append("[F18] ChatPanel 桥接丢 wsMsgId 去重（setChatMessages prev.some 缺失，会重复加气泡）")
    else:
        print("[F18] OK  wsMsgId 去重保留（setChatMessages prev.some(m.id === wsMsgId)，不重复加气泡）")
    # [19] 桥接仍 spokenIdsRef 防 TTS 重读
    if "spokenIdsRef" not in panel or "ttsSpeak" not in panel:
        errs.append("[F19] ChatPanel 桥接丢 spokenIdsRef / ttsSpeak（TTS 防重读缺失）")
    else:
        print("[F19] OK  spokenIdsRef + ttsSpeak 保留（agent_reply 朗读前查集合防重读）")
    # [20] 桥接仍 CHAT_MESSAGE_TYPES 白名单过滤
    if not re.search(r"CHAT_MESSAGE_TYPES\.has\(log\.type\)", panel):
        errs.append("[F20] ChatPanel 桥接丢 CHAT_MESSAGE_TYPES.has(log.type)（白名单过滤缺失）")
    else:
        print("[F20] OK  CHAT_MESSAGE_TYPES.has(log.type) 保留（非白名单 type 跳过）")

    # ── G. 无回归 ──
    # [21] B16 events 节流真源完整
    if "flushEvents" not in hook or "eventsBufRef" not in hook or "eventsFlushTimer" not in hook:
        errs.append("[G21] B16 events 节流真源缺失（flushEvents/eventsBufRef/eventsFlushTimer 之一缺）")
    else:
        print("[G21] OK  B16 events 节流真源完整（B17 不破 B16）")
    # [22] VF reasoning 节流真源完整
    if "flushReasoning" not in hook or "reasoningBufRef" not in hook or "reasoningFlushTimer" not in hook:
        errs.append("[G22] VF reasoning 节流真源缺失")
    else:
        print("[G22] OK  VF reasoning 节流真源完整（B17 不破 VF）")
    # [23] WS effect 依赖数组含 flushLogs
    m_deps = re.search(
        r"\},\s*\[groupId,\s*handleReconnect,\s*refreshPlan,\s*flushReasoning,\s*flushEvents,\s*flushLogs\]\)",
        hook,
    )
    if not m_deps:
        errs.append("[G23] WS effect 依赖数组缺 flushLogs（hook lint 缺依赖）")
    else:
        print("[G23] OK  WS effect 依赖数组含 flushLogs（与 flushEvents/flushReasoning 并列）")

    return errs


def main() -> int:
    print("=== VH14 回归：useBusEvent logs 批量 flush + ChatPanel 增量桥接（B17）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B17 logs 批量 flush + ChatPanel 增量桥接锁定：\n"
        "  · A 真源：logsBufRef + logsFlushTimer + flushLogs useCallback（镜像 reasoningBufRef/eventsBufRef 模式）；\n"
        "  · B entry 入队 ref（logsBufRef.current.push(entry)）而非直推 state——原 setLogs(...slice(-200), entry) O(n) slice 消除；\n"
        "  · C cap 200 在 flush 时统一 enforce（prev.concat(buf) + merged.length <= 200 直用 / 超取末尾 200，等价 slice(-200)）；\n"
        "  · D 定时器兜底（flushLogs 开头置 null）+ effect 清理 flush（clearTimeout + 残留 flush，不丢日志）；\n"
        "  · E ChatPanel 桥接遍历新增尾部（logsLenRef 增量游标 + for 遍历 + prevLen>length 重扫兜底 + 切群重置游标）——替代旧「只取最后一条」防批量 flush 后同批气泡丢失；\n"
        "  · F 行为零变：wsMsgId 去重 + spokenIdsRef 防 TTS 重读 + CHAT_MESSAGE_TYPES 白名单（气泡数量/朗读行为不变）；\n"
        "  · G 无回归：B16 events 节流 + VF reasoning 节流真源完整 + WS effect 依赖数组含 flushLogs。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
