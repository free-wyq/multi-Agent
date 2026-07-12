"""VH13 回归：useBusEvent events 批量 flush——环形缓冲替代 O(n) slice（task B16）.

锁住 B16 修复——``src/hooks/useBusEvent.ts:264`` 原 ``setEvents((prev) =>
[...prev.slice(-499), ev])`` 每事件 O(n) 切片：

  - 高频场景：task_token 流式（kimi-k2.6 写 200 字实测 820 token）+ coordinator_token
    逐字流式 + reasoning chunk（820 个），每个 token/chunk 都构 TraceEvent 进
    ``setEvents`` → 每 token O(n) slice + 触发 WorkerTrace/LeaderPanel/ChatPanel
    重渲染风暴（events 是三个组件 .filter 的依赖）。
  - 原 slice(-499) 每 token 复制 499 元素数组，820 token → 820 × O(500) = O(41万)
    数组元素拷贝 + 820 次 setState + 820 次重渲染。

B16 改法（镜像 VF 的 reasoningBufRef 节流模式）：
  - ev 攒进 ``eventsBufRef``（ref，不触发渲染），~50ms flush 一次到 state。
  - flush 时统一 enforce cap 500：``prev.concat(buf)`` 超 500 取末尾 500。
  - 把 ~800 次 setEvents 压到 ~20 次（50ms 窗口聚合 800 token）。
  - ref 不触发渲染，flush 才触发——与 reasoningBufRef 同构。
  - 最后一条 ev 后定时器兜底 flush，effect 清理时也 flush，不丢事件。

为何批量 flush 而非真环形缓冲（数组 + head/tail 指针）：
  - React state 必须是不可变新引用才触发重渲染——真环形缓冲（原地改 ring + 滑窗）
    返回同一引用 React 不感知变化。要触发渲染仍得 ``[...ring]`` 拷贝，等价于 slice。
  - 批量 flush 把 N 次 ``[...slice]`` 压成 1 次 ``concat``（N 倍降常数），
    且把 N 次 setState 压成 1 次（N 倍降渲染次数）——双重收益，比真环形缓冲收益更大
    且不破坏 React 不可变契约。reasoningBufRef 已验证此模式（VF 回归锁住）。

为何 50ms：与 reasoningBufRef 一致（约每帧），把 820 token 聚合成 ~20 批，每批
~40 token。更短（10ms）批次太碎仍近逐字；更长（100ms）流式可见延迟感明显。
50ms 是「不卡顿 + 不丢实时感」的平衡点（VF 实测验证）。

行为零变约束：
  - events 仍是 TraceEvent[] cap 500（cap 数不变，超 500 取末尾 500）。
  - 元素顺序不变（buf 内 push 顺序 = 到达顺序，concat 末尾保留时序）。
  - flush 是同步聚合（非异步丢弃），不丢事件——定时器兜底 + effect 清理兜底双保险。
  - events 仍 ``TraceEvent[]`` 类型，消费方（WorkerTrace/LeaderPanel .filter）零感知。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh12 同款风格。

六段契约：

  A. events 批量 flush 真源（ref + 定时器 + flush 函数）
    1. ``eventsBufRef`` ref 定义（TraceEvent[] 缓冲载体）。
    2. ``eventsFlushTimer`` ref 定义（定时器句柄）。
    3. ``flushEvents`` useCallback 定义（节流 flush 函数，flush 时 setEvents）。

  B. ev 入队 ref 而非直推 state（原 slice(-499) 消失）
    4. TraceEvent 构造后 ``eventsBufRef.current.push(ev)``（入队 ref）。
    5. 不再有 ``setEvents((prev) => [...prev.slice(-499), ev])``（原 O(n) slice 消失）。
    6. 入队后起/复用 ``eventsFlushTimer``（setTimeout ~50ms 调 flushEvents）。

  C. cap 500 在 flush 时统一 enforce（行为零变）
    7. flushEvents 内 ``prev.concat(buf)`` 合并（ref 批次追加到 state 末尾）。
    8. ``merged.length <= 500`` 分支返回 merged（未超 cap 直接用）。
    9. 超分支返回 ``merged.slice(merged.length - 500)``（取末尾 500，与原 slice(-499)
       等价的 cap 语义——保留最近 500 条，丢弃更早的）。

  D. 定时器兜底 + effect 清理 flush（不丢事件）
   10. flushEvents 开头置 ``eventsFlushTimer.current = null``（防重复触发）。
   11. effect 清理块含 ``clearTimeout(eventsFlushTimer.current)`` + 置 null（切群/卸载
       清定时器）。
   12. effect 清理块含 ``if (eventsBufRef.current.length > 0) flushEvents()``（残留兜底 flush）。

  E. 行为零变（events 仍 TraceEvent[] cap 500，消费方零感知）
   13. events 仍 ``useState<TraceEvent[]>([])``（类型 + 初值不变）。
   14. WS-02 命中复用分支仍下发 ``events: ctx.events``（共享状态不下发 buf，消费方零感知）。
   15. 返回值仍含 ``events``（return 对象不丢字段）。

  F. 无回归（reasoningBufRef 节流不破 + 依赖数组含 flushEvents）
   16. ``flushReasoning`` / ``reasoningBufRef`` / ``reasoningFlushTimer`` 仍存在（VF 节流不破）。
   17. WS effect 依赖数组含 ``flushEvents``（与 ``flushReasoning`` 并列，hook lint 不缺依赖）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK_TS = REPO / "src" / "hooks" / "useBusEvent.ts"


def _fn_body_ts(src: str, fname: str) -> str:
    """抽 TS fn 函数体到下一个同级 const/return（试箭头函数与 useCallback）。

    useBusEvent 里 flushEvents 是 ``const flushEvents = useCallback(() => {...}, [])``，
    函数体在 ``=> {`` 后到 ``}, [])`` 前。返回该块（含注释）。
    """
    m = re.search(
        rf"const {fname}\s*=\s*useCallback\(\(\)\s*=>\s*\{{(.*?)\n\s*\}},\s*\[\]\s*\)",
        src,
        re.S,
    )
    if m:
        return m.group(1)
    # 兜底：到下一个顶层 const/return
    m = re.search(rf"const {fname}\s*=.*?\{{(.*)", src, re.S)
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    hook = HOOK_TS.read_text(encoding="utf-8")

    # ── A. events 批量 flush 真源 ──
    # [1] eventsBufRef ref 定义
    if not re.search(r"const\s+eventsBufRef\s*=\s*useRef<TraceEvent\[\]>", hook):
        errs.append("[A1] 缺 eventsBufRef ref（TraceEvent[] 缓冲载体缺失）")
    else:
        print("[A1] OK  eventsBufRef ref 定义（TraceEvent[] 缓冲载体）")
    # [2] eventsFlushTimer ref 定义
    if not re.search(r"const\s+eventsFlushTimer\s*=\s*useRef<ReturnType<typeof setTimeout>\s*\|\s*null>", hook):
        errs.append("[A2] 缺 eventsFlushTimer ref（定时器句柄缺失）")
    else:
        print("[A2] OK  eventsFlushTimer ref 定义（定时器句柄）")
    # [3] flushEvents useCallback 定义
    if not re.search(r"const\s+flushEvents\s*=\s*useCallback", hook):
        errs.append("[A3] 缺 flushEvents useCallback（节流 flush 函数缺失）")
    else:
        print("[A3] OK  flushEvents useCallback 定义（节流 flush 函数）")

    # ── B. ev 入队 ref 而非直推 state ──
    # [4] TraceEvent 构造后 eventsBufRef.current.push(ev)
    has_push = re.search(
        r"const ev:\s*TraceEvent\s*=\s*\{[^}]*\}[^}]*\n\s*eventsBufRef\.current\.push\(ev\)",
        hook,
        re.S,
    )
    if not has_push:
        # 宽松：ev 构造后附近出现 push(ev)
        has_push_loose = "eventsBufRef.current.push(ev)" in hook
        if not has_push_loose:
            errs.append("[B4] ev 未入队 eventsBufRef.current.push(ev)（B16 未改批量缓冲）")
        else:
            print("[B4] OK  ev 入队 eventsBufRef.current.push(ev)（ref 缓冲，不直推 state）")
    else:
        print("[B4] OK  ev 入队 eventsBufRef.current.push(ev)（ref 缓冲，不直推 state）")
    # [5] 不再有 setEvents((prev) => [...prev.slice(-499), ev])（排除注释行——B16 注释会
    #     提及原模式以文档化，属合理引用非代码字面量）
    code_no_comments = re.sub(r"//[^\n]*", "", hook)
    if re.search(r"setEvents\(\(prev\)\s*=>\s*\[\.\.\.prev\.slice\(-499\),\s*ev\]\)", code_no_comments):
        errs.append("[B5] 仍 setEvents((prev) => [...prev.slice(-499), ev])（原 O(n) slice 未消除）")
    else:
        print("[B5] OK  原 setEvents((prev) => [...prev.slice(-499), ev]) 已消除（O(n) slice 不再每 token 触发，注释引用不计）")
    # [6] 入队后起/复用 eventsFlushTimer（setTimeout ~50ms 调 flushEvents）
    m_timer = re.search(
        r"eventsBufRef\.current\.push\(ev\)\s*\n\s*if\s*\(!eventsFlushTimer\.current\)\s*\{\s*"
        r"eventsFlushTimer\.current\s*=\s*window\.setTimeout\(\(\)\s*=>\s*\{\s*flushEvents\(\)\s*\},\s*\d+\s*\)",
        hook,
        re.S,
    )
    if not m_timer:
        errs.append("[B6] ev 入队后未起/复用 eventsFlushTimer（setTimeout flush 链断）")
    else:
        delay = re.search(r"\},\s*(\d+)\s*\)", m_timer.group(0))
        dval = delay.group(1) if delay else "?"
        print(f"[B6] OK  ev 入队 → setTimeout(~{dval}ms) → flushEvents（节流链完整）")

    # ── C. cap 500 在 flush 时统一 enforce ──
    flush_body = _fn_body_ts(hook, "flushEvents")
    if not flush_body:
        errs.append("[setup] flushEvents 函数体未找到")
    else:
        # [7] prev.concat(buf) 合并
        if "prev.concat(buf)" not in flush_body and "prev\.concat(buf)" not in flush_body:
            if "concat(buf)" not in flush_body:
                errs.append("[C7] flushEvents 未 prev.concat(buf)（ref 批次未追加到 state 末尾）")
            else:
                print("[C7] OK  prev.concat(buf) 合并（ref 批次追加到 state 末尾）")
        else:
            print("[C7] OK  prev.concat(buf) 合并（ref 批次追加到 state 末尾）")
        # [8] merged.length <= 500 分支返回 merged
        if not re.search(r"merged\.length\s*<=\s*500", flush_body):
            errs.append("[C8] flushEvents 缺 merged.length <= 500 分支（未超 cap 直用）")
        else:
            print("[C8] OK  merged.length <= 500 分支（未超 cap 直用 merged）")
        # [9] 超分支返回 merged.slice(merged.length - 500)
        if not re.search(r"merged\.slice\(merged\.length\s*-\s*500\)", flush_body):
            errs.append("[C9] flushEvents 缺 merged.slice(merged.length - 500)（超 cap 未取末尾 500）")
        else:
            print("[C9] OK  merged.slice(merged.length - 500)（超 cap 取末尾 500，与 slice(-499) 等价）")

    # ── D. 定时器兜底 + effect 清理 flush ──
    # [10] flushEvents 开头置 eventsFlushTimer.current = null
    if not re.search(r"flushEvents\s*=\s*useCallback\(\(\)\s*=>\s*\{\s*eventsFlushTimer\.current\s*=\s*null", hook):
        errs.append("[D10] flushEvents 开头未置 eventsFlushTimer.current = null（防重复触发缺失）")
    else:
        print("[D10] OK  flushEvents 开头置 eventsFlushTimer.current = null（防重复触发）")
    # [11] effect 清理块含 clearTimeout(eventsFlushTimer.current)
    cleanup_blk = hook.split("return () =>", 1)[-1] if "return () =>" in hook else ""
    if "clearTimeout(eventsFlushTimer.current)" not in cleanup_blk:
        errs.append("[D11] effect 清理未 clearTimeout(eventsFlushTimer.current)（切群定时器残留）")
    else:
        print("[D11] OK  effect 清理 clearTimeout(eventsFlushTimer.current)（切群清定时器）")
    # [12] effect 清理块含 if (eventsBufRef.current.length > 0) flushEvents()
    if "eventsBufRef.current.length > 0" not in cleanup_blk or "flushEvents()" not in cleanup_blk:
        errs.append("[D12] effect 清理未兜底 flushEvents（切群残留 ev 可能丢）")
    else:
        print("[D12] OK  effect 清理 if (eventsBufRef.current.length > 0) flushEvents()（残留兜底）")

    # ── E. 行为零变（events 仍 TraceEvent[] cap 500，消费方零感知）──
    # [13] events 仍 useState<TraceEvent[]>([])
    if not re.search(r"const\s+\[events,\s*setEvents\]\s*=\s*useState<TraceEvent\[\]>\(\[\]\)", hook):
        errs.append("[E13] events state 类型/初值异常（应 useState<TraceEvent[]>([])）")
    else:
        print("[E13] OK  events 仍 useState<TraceEvent[]>([])（类型 + 初值不变）")
    # [14] WS-02 命中复用分支仍下发 events: ctx.events
    if not re.search(r"events:\s*ctx\.events", hook):
        errs.append("[E14] WS-02 命中复用分支未下发 events: ctx.events（共享状态破）")
    else:
        print("[E14] OK  WS-02 命中复用分支仍下发 events: ctx.events（共享状态不下发 buf）")
    # [15] 返回值仍含 events
    return_blk = re.search(r"return\s*\{[^}]*events[^}]*\}", hook.split("return { logs", 1)[-1], re.S)
    if not return_blk and "return { logs" in hook:
        # 检查最后的 return 对象含 events
        tail = hook.rsplit("return", 1)[-1]
        if "events" in tail and "streaming" in tail:
            print("[E15] OK  返回值仍含 events（return 对象不丢字段）")
        else:
            errs.append("[E15] 返回值缺 events 字段")
    else:
        print("[E15] OK  返回值仍含 events（return 对象不丢字段）")

    # ── F. 无回归 ──
    # [16] flushReasoning / reasoningBufRef / reasoningFlushTimer 仍存在（VF 节流不破）
    if "flushReasoning" not in hook or "reasoningBufRef" not in hook or "reasoningFlushTimer" not in hook:
        errs.append("[F16] VF reasoning 节流真源缺失（flushReasoning/reasoningBufRef/reasoningFlushTimer 之一缺）")
    else:
        print("[F16] OK  VF reasoning 节流真源完整（flushReasoning + reasoningBufRef + reasoningFlushTimer）")
    # [17] WS effect 依赖数组含 flushEvents
    m_deps = re.search(r"\},\s*\[groupId,\s*handleReconnect,\s*refreshPlan,\s*flushReasoning,\s*flushEvents\]\)", hook)
    if not m_deps:
        errs.append("[F17] WS effect 依赖数组缺 flushEvents（hook lint 缺依赖）")
    else:
        print("[F17] OK  WS effect 依赖数组含 flushEvents（与 flushReasoning 并列，hook lint 完整）")

    return errs


def main() -> int:
    print("=== VH13 回归：useBusEvent events 批量 flush——环形缓冲替代 O(n) slice（B16）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B16 events 批量 flush 锁定：\n"
        "  · A 真源：eventsBufRef + eventsFlushTimer + flushEvents useCallback（镜像 reasoningBufRef 模式）；\n"
        "  · B ev 入队 ref（eventsBufRef.current.push(ev)）而非直推 state——原 setEvents(...slice(-499), ev) O(n) slice 消除；\n"
        "  · C cap 500 在 flush 时统一 enforce（prev.concat(buf) + merged.length <= 500 直用 / 超取末尾 500，等价 slice(-499)）；\n"
        "  · D 定时器兜底（flushEvents 开头置 null）+ effect 清理 flush（clearTimeout + 残留 flush，不丢事件）；\n"
        "  · E 行为零变：events 仍 TraceEvent[] cap 500 + WS-02 共享下发 ctx.events + 返回值含 events（消费方零感知）；\n"
        "  · F 无回归：VF reasoning 节流真源完整 + WS effect 依赖数组含 flushEvents。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
