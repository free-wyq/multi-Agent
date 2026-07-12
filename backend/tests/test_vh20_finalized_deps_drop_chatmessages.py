"""VH20 回归：finalizedBubbles deps 去 chatMessages——增量 ref 退场集合避每消息重算（task B23）.

锁住 B23 修复——``src/components/ChatPanel.tsx`` ``finalizedBubbles`` 原 useMemo deps
含 ``chatMessages``，每条新消息（含 task_token 流式期桥接的 task_log / 每条 agent_reply）
都换新 chatMessages 引用 → finalizedBubbles 全量重算（遍历 events + 对每个收尾事件
chatMessages.some 扫描）。高频聊天时 finalizedBubbles 几乎不变（只在 task 收尾 + reply
落地两个时刻变化），却被每条新消息拖着重算——浪费。

B23 改法（退场判定从「每次重算 chatMessages.some」改为「reply 落地 effect 增量回填 ref」）：
  - 新增 ``repliedTaskIdsRef = useRef<Set<string>>(new Set())``——「reply 已落地、定稿气泡
    已退场」的 task_id 集合（增量真源，ref 不触发渲染）。
  - logs 桥接 effect（line ~628 setChatMessages）每次新 agent_reply 落地后，若其 task_id
    非空就把 task_id 加入 ref（标记该 task 的定稿气泡可退场）：
      if (log.type === 'agent_reply' && log.taskId) repliedTaskIdsRef.current.add(log.taskId)
  - finalizedBubbles 退场判定改用 ``repliedTaskIds.has(e.taskId)``（O(1) 集合查），
    不再 ``chatMessages.some`` 扫描 → deps 去掉 chatMessages（chatMessages 变化不再触发
    本 memo 重算）。deps 从 ``[events, streaming, chatMessages, agentStatuses]`` 收窄到
    ``[events, streaming, agentStatuses]``。
  - 切群 effect（line ~711）清空 ref（``repliedTaskIdsRef.current = new Set()``），新群
    退场状态独立（旧群 task_id 不泄漏到新群）。

为何用 ref 不用 state：
  ref 变化不触发渲染（避免「回填 ref → finalizedBubbles 重算 → 渲染」链）。finalizedBubbles
  的重算时机回归「events/streaming/agentStatuses 变化时」——这是定稿气泡真正变化的时机
  （task 收尾事件入 events / 流式缓冲清空 / agent 名变）。ref 只是让 finalizedBubbles 在
  重算时能读到最新退场集合，不自己驱动重算。这复刻 logsLenRef（B17 增量桥接）+ spokenIdsRef
  （TTS 去重）+ lastDateRef（日期分组）的同款 ref-as-truth 模式。

为何保留 chatMessages 兜底分支（仍读一次）：
  chat 路径（coordinator/worker node_chat）的 agent_reply 不经 _reply（走 graph
  _unified_reply 不传 task_id）→ reply 无 task_id → 不入 repliedTaskIdsRef。但 chat 路径
  无 task_complete/failed 事件（非 execute 路径），finalizedBubbles 循环根本不会为 chat
  回复生成定稿气泡（kind 仅 complete/failed 进循环）——故兜底分支实际不命中，保留仅防御性
  （未来若 chat 路径也接 task_complete 收尾，兜底仍能退场）。chatMessages 仍读一次做兜底
  时间戳比较，但读它是「重算时顺带读最新 chatMessages」，非「chatMessages 变化驱动重算」
  ——chatMessages 不进 deps 不影响兜底正确性。

reload-safe（切群/重连回灌）：
  切群 effect 清空 ref 后，logs 桥接 effect 重扫历史 agent_reply（从 listByGroup 拉的历史
  经 logs 重建），历史 agent_reply 带 task_id 同样入集合——故 reload 后退场状态从历史重建，
  与 live 一致。finalizedBubbles 下次因 events 变化重算时读到完整退场集合。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh19 同款风格。

六段契约：

  A. repliedTaskIdsRef 真源声明（ChatPanel.tsx）
    1. ``repliedTaskIdsRef = useRef<Set<string>>(new Set())`` 声明（Set<string> 类型）。
    2. 注释说明用途（退场集合 + 为何用 ref 不用 state + 复刻 logsLenRef/spokenIdsRef/lastDateRef）。

  B. reply 落地 effect 增量回填 ref（logs 桥接 effect 内）
    3. logs 桥接 effect 内含 ``repliedTaskIdsRef.current.add(log.taskId)``（agent_reply 落地回填）。
    4. 回填守卫 ``log.type === 'agent_reply' && log.taskId``（仅 agent_reply + 有 task_id 才入集合）。

  C. finalizedBubbles 退场判定改读 ref（去 chatMessages.some 扫描）
    5. 退场判定含 ``repliedTaskIds.has(e.taskId)``（B23 主路径读 ref，O(1) 集合查）。
    6. 退场判定不再 ``chatMessages.some((m) => m.sender_id === e.agentId && (m.task_id === e.taskId || ...))``
       原全量扫描形态（B23 改读 ref，chatMessages.some 仅兜底时间戳分支用）。
    7. 兜底时间戳分支仍含 ``m.sender_id === e.agentId && new Date(m.created_at).getTime() >= e.timestamp``
       （task_id-less 路径防御性退场，实际不命中）。

  D. finalizedBubbles deps 去 chatMessages（核心——避每消息重算）
    8. finalizedBubbles useMemo deps 为 ``[events, streaming, agentStatuses]``（不含 chatMessages）。
    9. deps 注释说明为何去 chatMessages（退场改读 ref，chatMessages 变化不再驱动重算）。

  E. 切群 effect 清空 ref（reload-safe）
   10. 切群 effect 含 ``repliedTaskIdsRef.current = new Set()``（新群退场状态独立）。
   11. 切群 effect 注释说明为何清空（新群 task_id 独立 + 历史 agent_reply 经 logs 重扫回填）。

  F. 行为零变 + 无回归
   12. finalizedBubbles 循环骨架不动（kind complete/failed + taskId 去重 + streaming 未清才渲染）。
   13. vh19 [C6-C9] 退场判定断言前向兼容（接受 B22 m.task_id===e.taskId 或 B23 repliedTaskIds.has 两种形态）。
   14. logs 桥接 effect deps 仍含 logs（chatMessages 来源不变，B23 只在 effect 体内加回填 ref）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（B16/vh13 坑延续）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    panel = PANEL.read_text(encoding="utf-8")
    panel_nc = _strip_ts_comments(panel)

    # ── A. repliedTaskIdsRef 真源声明 ──
    # [1] useRef<Set<string>>(new Set()) 声明
    if not re.search(r"repliedTaskIdsRef\s*=\s*useRef<Set<string>>\(new Set\(\)\)", panel):
        errs.append("[A1] 缺 repliedTaskIdsRef = useRef<Set<string>>(new Set())（B23 退场集合真源未声明）")
    else:
        print("[A1] OK  repliedTaskIdsRef = useRef<Set<string>>(new Set())（退场集合真源）")
    # [2] 注释说明用途（退场集合 + ref-as-truth）
    # 取 repliedTaskIdsRef 声明上方的注释（// 单行注释块，非 /** */ 块注释）。
    # 声明是 ``const repliedTaskIdsRef = useRef...``，注释与 const 间可能有 ``const`` 关键字。
    m_decl = re.search(r"((?:[ \t]*//[^\n]*\n)+)[ \t]*const repliedTaskIdsRef\s*=\s*useRef", panel)
    if not m_decl:
        errs.append("[A2] repliedTaskIdsRef 声明缺注释（用途未文档化）")
    else:
        doc = m_decl.group(1)
        if "退场" not in doc and "replied" not in doc.lower() and "B23" not in doc:
            errs.append("[A2] repliedTaskIdsRef 注释未说明用途（退场集合 / B23）")
        else:
            print("[A2] OK  repliedTaskIdsRef 注释说明用途（退场集合 + B23 ref-as-truth）")

    # ── B. reply 落地 effect 增量回填 ref ──
    # [3] logs 桥接 effect 内含 repliedTaskIdsRef.current.add(log.taskId)
    if "repliedTaskIdsRef.current.add(log.taskId)" not in panel_nc:
        errs.append("[B3] logs 桥接 effect 缺 repliedTaskIdsRef.current.add(log.taskId)（reply 落地未回填 ref）")
    else:
        print("[B3] OK  logs effect 含 repliedTaskIdsRef.current.add(log.taskId)（reply 落地回填）")
    # [4] 回填守卫 log.type === 'agent_reply' && log.taskId
    # 守卫 + add 在同一 if 块内——查 add 上方最近的 if 守卫
    m_add = re.search(
        r"if\s*\(\s*log\.type === 'agent_reply'\s*&&\s*log\.taskId\s*\)\s*\{[^}]*repliedTaskIdsRef\.current\.add\(log\.taskId\)",
        panel_nc,
        re.S,
    )
    if not m_add:
        errs.append("[B4] 回填守卫缺 log.type === 'agent_reply' && log.taskId（应仅 agent_reply + 有 task_id 才入集合）")
    else:
        print("[B4] OK  回填守卫 log.type === 'agent_reply' && log.taskId（仅 agent_reply + 有 task_id 入集合）")

    # ── C. finalizedBubbles 退场判定改读 ref ──
    # 抽 finalizedBubbles useMemo 体
    m_fb = re.search(r"const finalizedBubbles = useMemo\(\(\) => \{(.*?)\n  \}, \[", panel, re.S)
    if not m_fb:
        errs.append("[setup] finalizedBubbles useMemo 体未找到")
    else:
        fb_nc = _strip_ts_comments(m_fb.group(1))
        # [5] 退场判定含 repliedTaskIds.has(e.taskId)
        if "repliedTaskIds.has(e.taskId)" not in fb_nc:
            errs.append("[C5] finalizedBubbles 退场判定缺 repliedTaskIds.has(e.taskId)（B23 读 ref 主路径未接线）")
        else:
            print("[C5] OK  退场判定含 repliedTaskIds.has(e.taskId)（B23 读 ref，O(1) 集合查）")
        # [6] 不再有 B22 原 chatMessages.some((m) => m.sender_id === e.agentId && (m.task_id === e.taskId || ...))
        # 全量扫描形态——B23 改读 ref 后，m.task_id === e.taskId 不应再在主路径 some 回调里
        # （兜底分支的 chatMessages.some 只做时间戳比较，不含 m.task_id === e.taskId）。
        # 断言：函数体不再含 m.task_id === e.taskId（B22 形态被 B23 取代）。
        if "m.task_id === e.taskId" in fb_nc:
            errs.append("[C6] finalizedBubbles 退场判定仍含 m.task_id === e.taskId（B22 全量扫描形态未消除，B23 应改读 ref）")
        else:
            print("[C6] OK  退场判定不再 m.task_id === e.taskId 全量扫描（B23 改读 ref）")
        # [7] 兜底时间戳分支仍含 m.sender_id === e.agentId && new Date(m.created_at)...
        # B23 保留兜底：repliedTaskIds 未命中时才 chatMessages.some 时间戳兜底
        has_sender = "m.sender_id === e.agentId" in fb_nc
        has_ts = "new Date(m.created_at).getTime() >= e.timestamp" in fb_nc
        if not (has_sender and has_ts):
            errs.append(f"[C7] 兜底时间戳分支不全（sender={has_sender} ts={has_ts}——应全，task_id-less 路径防御性退场）")
        else:
            print("[C7] OK  兜底时间戳分支 m.sender_id === e.agentId && new Date(m.created_at).getTime() >= e.timestamp（保留）")

    # ── D. finalizedBubbles deps 去 chatMessages ──
    # [8] deps 为 [events, streaming, agentStatuses]（不含 chatMessages）
    m_deps = re.search(r"const finalizedBubbles = useMemo\([\s\S]*?\}, \[([^\]]*)\]\)", panel)
    if not m_deps:
        errs.append("[D8] finalizedBubbles useMemo deps 数组未找到")
    else:
        deps = m_deps.group(1)
        deps_items = [d.strip() for d in deps.split(",") if d.strip()]
        has_chatMessages = "chatMessages" in deps_items
        expected_core = {"events", "streaming", "agentStatuses"}
        actual_core = set(deps_items) & expected_core
        if has_chatMessages:
            errs.append(f"[D8] finalizedBubbles deps 仍含 chatMessages（{deps_items}——B23 应去掉，chatMessages 变化不再驱动重算）")
        elif actual_core != expected_core:
            errs.append(f"[D8] finalizedBubbles deps 核心依赖不全（{actual_core}——应含 {expected_core}）")
        else:
            print(f"[D8] OK  deps = [{', '.join(deps_items)}]（去 chatMessages，核心 events/streaming/agentStatuses 齐全）")
    # [9] deps 注释说明为何去 chatMessages
    # B23 deps 注释在 deps 数组「之后」（line 612 起，}, [events, streaming, agentStatuses] 下一行），
    # 非之前。查 deps 数组之后紧跟的 // 注释块含「chatMessages」「去」语义。
    m_deps_cmt = re.search(
        r"\}, \[events, streaming, agentStatuses\]\)\s*\n((?:[ \t]*//[^\n]*\n)+)",
        panel,
    )
    if not m_deps_cmt:
        errs.append("[D9] finalizedBubbles deps 缺注释说明为何去 chatMessages（决策未文档化）")
    else:
        cmt = m_deps_cmt.group(1)
        if "chatMessages" not in cmt or "去" not in cmt:
            errs.append("[D9] deps 注释未说明去 chatMessages（决策理由缺失）")
        else:
            print("[D9] OK  deps 注释说明去 chatMessages（退场改读 ref，chatMessages 变化不再驱动重算）")

    # ── E. 切群 effect 清空 ref ──
    # [10] 切群 effect 含 repliedTaskIdsRef.current = new Set()
    # 切群 effect 是 [chatGroupId] deps 的 effect
    m_switch = re.search(
        r"useEffect\(\(\) => \{([\s\S]*?)\}, \[chatGroupId\]\)",
        panel,
    )
    if not m_switch:
        errs.append("[E10] 切群 effect（[chatGroupId] deps）未找到")
    else:
        switch_body = m_switch.group(1)
        if "repliedTaskIdsRef.current = new Set()" not in switch_body:
            errs.append("[E10] 切群 effect 缺 repliedTaskIdsRef.current = new Set()（新群退场状态未独立）")
        else:
            print("[E10] OK  切群 effect 清空 repliedTaskIdsRef.current = new Set()（新群退场状态独立）")
        # [11] 注释说明为何清空
        # 取清空行上方注释
        m_clr_cmt = re.search(
            r"// [^\n]*\n\s*// [^\n]*\n\s*repliedTaskIdsRef\.current = new Set\(\)",
            switch_body,
        )
        if not m_clr_cmt:
            # 退化：只查清空行上方一行注释
            m_clr_cmt = re.search(r"// [^\n]*\n\s*repliedTaskIdsRef\.current = new Set\(\)", switch_body)
        if not m_clr_cmt:
            errs.append("[E11] 切群 effect 清空 ref 缺注释（为何清空未文档化）")
        else:
            cmt = m_clr_cmt.group(0)
            if "新群" not in cmt and "B23" not in cmt:
                errs.append("[E11] 清空 ref 注释未说明新群独立 / B23（理由缺失）")
            else:
                print("[E11] OK  清空 ref 注释说明新群独立（B23）")

    # ── F. 行为零变 + 无回归 ──
    # [12] finalizedBubbles 循环骨架不动
    if m_fb:
        fb_full = m_fb.group(1)
        has_kind = "e.kind !== 'complete' && e.kind !== 'failed'" in fb_full
        has_seen = "seen.has(e.taskId)" in fb_full
        has_streaming = "streaming[e.taskId]" in fb_full
        if not (has_kind and has_seen and has_streaming):
            errs.append(f"[F12] finalizedBubbles 循环骨架破（kind={has_kind} seen={has_seen} streaming={has_streaming}——应全）")
        else:
            print("[F12] OK  finalizedBubbles 循环骨架不动（kind complete/failed + taskId 去重 + streaming 未清才渲染）")
    # [13] vh19 [C6-C9] 前向兼容（接受 B22 或 B23 形态）
    vh19 = (REPO / "backend" / "tests" / "test_vh19_finalized_taskid_retire.py").read_text(encoding="utf-8")
    if "repliedTaskIds.has(e.taskId)" not in vh19:
        errs.append("[F13] vh19 [C6] 未前向兼容 B23 repliedTaskIds.has(e.taskId) 形态（断言只认 B22 m.task_id===e.taskId 会误报）")
    else:
        print("[F13] OK  vh19 [C6] 前向兼容 B23 repliedTaskIds.has(e.taskId)（接受 B22/B23 两种退场判定形态）")
    # [14] logs 桥接 effect deps 仍含 logs（chatMessages 来源不变）
    m_logs_eff = re.search(
        r"const logsLenRef = useRef\(0\)\s*useEffect\(\(\) => \{[\s\S]*?\}, \[([^\]]*)\]\)",
        panel,
    )
    if not m_logs_eff:
        errs.append("[F14] logs 桥接 effect deps 数组未找到")
    else:
        logs_deps = m_logs_eff.group(1)
        if "logs" not in logs_deps:
            errs.append(f"[F14] logs 桥接 effect deps 缺 logs（{logs_deps}——chatMessages 来源不变）")
        else:
            print(f"[F14] OK  logs 桥接 effect deps 仍含 logs（B23 只在 effect 体内加回填 ref，不改 effect deps）")

    return errs


def main() -> int:
    print("=== VH20 回归：finalizedBubbles deps 去 chatMessages——增量 ref 退场集合避每消息重算（B23）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B23 finalizedBubbles deps 去 chatMessages 锁定：\n"
        "  · A repliedTaskIdsRef = useRef<Set<string>>(new Set())（退场集合真源，ref-as-truth）；\n"
        "  · B logs effect 增量回填 ref（agent_reply 落地 add(taskId)，仅 agent_reply+有 task_id 入集合）；\n"
        "  · C finalizedBubbles 退场判定改读 repliedTaskIds.has(e.taskId)（O(1) 集合查，去 chatMessages.some 全量扫描）+ 兜底时间戳保留；\n"
        "  · D deps 从 [events, streaming, chatMessages, agentStatuses] 收窄到 [events, streaming, agentStatuses]（chatMessages 变化不再驱动重算）；\n"
        "  · E 切群 effect 清空 ref（新群退场状态独立 + 历史 agent_reply 经 logs 重扫回填 reload-safe）；\n"
        "  · F 行为零变（循环骨架不动）+ 无回归（vh19 前向兼容 B23 形态 + logs effect deps 不变）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
