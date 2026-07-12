"""验证B-1 自测：task_think 折叠块在气泡内可见且不与最终回复重复（task 26）.

回归「CodeBuddy-style 气泡过程」前端 ST-05 链路：
  task 17  CHAT_MESSAGE_TYPES 白名单放行 task_think（过渡方案，会成独立气泡）
  task 18  task_think 按 task_id 归并到对应流式/定稿气泡（thinkEventsByTask）
  task 19  ChatMessageBubble 加 task_think 折叠块（Collapse items）

但 task 17 的白名单放行后来被回滚——task_think 不再走 logs 桥接成独立气泡
（会与归并折叠重复 + 与 task_think(phase=final) 即最终答案成两条气泡），改走
TraceEvent 流（useBusEvent events，mapKind→'think'），由 thinkEventsByTask 归并到
对应流式/定稿气泡的 thinkEvents，由 ChatMessageBubble 渲染成气泡内折叠块。

本自测验证「不重复」契约的两个确定性条件（与前端渲染逻辑一一对应，非语义判断）：

  1. task_think 不走「独立气泡」路径——它在 useBusEvent 里被排除出 logs
     （d.type !== 'task_token' 之外，task_think 不进 setLogs：白名单不放行，
      且 task_think 的 content truthy 时若进 logs 会成 LogPanel 条目，但
      ChatPanel.logs effect 只桥接 CHAT_MESSAGE_TYPES 内的 type，task_think
      不在内 → 不落 chatMessages → 不成独立气泡）。本自测断言：
      CHAT_MESSAGE_TYPES 不含 'task_think'（task 17 放行被回滚）+ mapKind
      把 'task_think' 映成 'think'（TraceEvent 流归并路径）。

  2. task_think(phase=final) 的 content != 持久化 agent_reply 的 content
     （防「重复」的语义守卫）。后端 emit_task_think(phase=final) 在
     on_chat_model_end 的 final 分支推送 output[:200]（最终答案前 200 字），
     而 task 收尾后 _reply 推的是「任务完成 🎉\\n{snippet}」模板（snippet=
     output[:200]）。两者文本不同——final think 是裸答案，reply 是套了
     「任务完成 🎉」前缀的 announce。本自测断言二者文本不相等（不重复），
     但 reply.content 包含 final think.content（snippet 同源）。

为何 @后端工程师 直送 worker（不走 coordinator 全链路）：
  task_think 是 worker ReAct 循环里 on_chat_model_end 流出的中间推理 + 最终
  答案（registry.on_log think/answer→emit_task_think），只在 worker execute
  路径（_run_worker_task → run_agent_loop → create_react_agent）产生。协调者
  走 LLM 直调（非 create_react_agent），不推 task_think。故必须触发 worker
  execute（brain 判定 execute → push_task → _handle_task → _run_worker_task）。

  用单聊群「后端工程师」（group_e53545c...，single_chat=true，
  coordinator_id=agent_backend_1）最干净：单聊 worker 的 brain 用其自身
  system_prompt 主导行为，@自己 发「写文件」类明确动手指令 → brain 判 execute
  → 走 create_react_agent → 推 task_think(thinking) + task_think(final)。
  避开 coordinator chat/dispatch 的不确定性，单 worker 单任务内存占用低。

为何用 disk cross-check 而非前端 e2e：
  项目无前端测试运行器（package.json 无 vitest/jest/testing-library）。前端
  渲染逻辑的「不重复」契约用「后端事件 schema + 前端源码静态断言」覆盖：
    (a) 后端断言 task_think 事件确实产生（thinking + final 各 ≥1，且 final
        content 与 reply content 文本不同 → 不重复的语义守卫）；
    (b) 前端断言 CHAT_MESSAGE_TYPES 不含 task_think + mapKind 映 'think'
        + thinkEventsByTask 按 task_id 归并 + ChatMessageBubble hasThinks
        渲染 Collapse（静态读源码校验，与运行时渲染逻辑一致）。
  两者合起来证明：task_think 产生 → 走 events 归并到气泡 thinkEvents →
  渲染成折叠块（可见），且不进 logs 成独立气泡、final content 与 reply
  不同（不重复）。

校验项（确定性）：
  1. 前端契约：CHAT_MESSAGE_TYPES 不含 'task_think'（不桥接成独立气泡）。
  2. 前端契约：useBusEvent.mapKind('task_think')=='think'（归并到 TraceEvent）。
  3. 前端契约：ChatPanel.thinkEventsByTask 按 task_id 归并 kind=='think' 事件，
     且流式/定稿气泡都传 thinkEvents={thinkEventsByTask[taskId]}。
  4. 前端契约：ChatMessageBubble 有 hasThinks 守卫 + Collapse items 渲染 thinkRows
     （phase=thinking→「思考」/ final→「结论」标签 + 字符数）。
  5. 运行时：worker execute 路径产生 task_think 事件 ≥1（phase=thinking 或 final 任一），
     其中 final ≥1（最终答案流出——「结论」折叠项有内容，且供 check 6/7 比对 reply）。
     thinking ≥0 不强制（DeepSeek 等模型在工具调用前常只发 tool_calls 无文本，agent_loop
     的 ``if ai_content:`` 守卫会跳过空 think，属模型行为非 think 管道 bug）。
  6. 运行时「不重复」：task_think(final).content != 持久化 agent_reply.content
     （final 是裸答案 output[:200]，reply 套「任务完成 🎉\\n」前缀 → 文本不同 → 不重复）。
  7. 运行时「同源不丢」：agent_reply.content 包含 final.content（snippet 同源，
     reply = "任务完成 🎉\\n" + output[:200]，final = output[:200] → reply 含 final）。
  8. task_think 事件 task_id 与 task_complete 一致（同属 execute 路径的 tq_ 任务，
     thinkEventsByTask[tq_] 即定稿气泡 thinkEvents 取的 key → 折叠块挂对气泡）。
     注意 task_token 的 task_id 是混合的（brain reply_id 裸 hex + execute tq_，task 24/25
     双路径设计），不在本断言内（那是 task_token 归并的事，与 task_think 可见性无关）。
  9. task_complete(success) 收尾 + 磁盘产物落盘。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

BASE = "http://localhost:8000"
# 单聊群「后端工程师」——single_chat=true，coordinator_id=agent_backend_1，
# 故 agent_backend_1 走 worker 图（is_coordinator 但 single_chat→graph_kind=worker），
# @后端工程师 直送其 brain，execute 路径产生 task_think。
GROUP_ID = "group_e53545c71a8c4cf8ae5e69d06ef77952"
WS_URL = f"ws://localhost:8000/ws/bus/{GROUP_ID}"
WORKER_ID = "agent_backend_1"

# 多步骤写文件任务：强制 brain 判 execute（明确动手指令）→ create_react_agent
# 至少 1 轮 thinking（list_dir 前的中间推理）+ 1 轮 final（写完后的总结答案）。
# 用唯一前缀避免与历史产物冲突。
OUT_FILE = "vb1_think_visible_probe.md"
TASK_CONTENT = (
    f"@后端工程师 请直接动手执行以下任务，必须用你的工具完成：\n"
    f"1. 用 list_dir 看一下当前工作区有哪些文件；\n"
    f"2. 用 write_file 创建 {OUT_FILE}，写入 3-4 行工作区文件结构简要分析；\n"
    f"3. 完成后用一句话回复结论。"
)

WS_TIMEOUT = 240.0

# 前端源码路径（静态契约断言用）。
REPO = Path(__file__).resolve().parents[2]
CHAT_PANEL = REPO / "src" / "components" / "ChatPanel.tsx"
USE_BUS_EVENT = REPO / "src" / "hooks" / "useBusEvent.ts"
BUBBLE = REPO / "src" / "components" / "ChatMessageBubble.tsx"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def worker_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == WORKER_ID:
                return a["status"]
    return "unknown"


async def send_message(content: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": GROUP_ID,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json()


async def collect_until_done(timeout: float) -> list[dict]:
    """连 WS 收事件直到 task_complete/task_failed 或超时。返回全量事件（到达序）。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    finished = False
    async with websockets.connect(WS_URL) as ws:
        while time.time() < deadline and not finished:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") in ("task_complete", "task_failed"):
                # 收尾后再多收 3s（agent_reply 落地在 task_complete 之后）
                end = time.time() + 3.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                finished = True
    return events


def parse_ts(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ── 前端静态契约断言（task 17/18/19 渲染逻辑）──


def assert_frontend_contract() -> list[str]:
    """读前端源码断言 task_think 归并折叠链路 + 不走独立气泡。返回 errs 列表。"""
    errs: list[str] = []

    panel = CHAT_PANEL.read_text(encoding="utf-8")
    hook = USE_BUS_EVENT.read_text(encoding="utf-8")
    bubble = BUBBLE.read_text(encoding="utf-8")

    # [1] CHAT_MESSAGE_TYPES 不含 task_think（task 17 放行被回滚 → 不成独立气泡）。
    m = re.search(r"const CHAT_MESSAGE_TYPES = new Set\(\[(.*?)\]\)", panel, re.S)
    if not m:
        errs.append("[前端1] 未找到 CHAT_MESSAGE_TYPES 定义")
    else:
        whitelist = m.group(1)
        if "task_think" in whitelist:
            errs.append(
                "[前端1] CHAT_MESSAGE_TYPES 仍含 'task_think'——task 17 放行未回滚，"
                "task_think 会被桥接成独立气泡与归并折叠重复"
            )
        else:
            print("[前端1] OK  CHAT_MESSAGE_TYPES 不含 task_think（不桥接成独立气泡）")

    # [2] mapKind('task_think') == 'think'（归并到 TraceEvent 流）。
    m2 = re.search(r"case 'task_think':\s*return\s*'(\w+)'", hook)
    if not m2:
        errs.append("[前端2] useBusEvent.mapKind 未把 'task_think' 映成 kind")
    elif m2.group(1) != "think":
        errs.append(
            f"[前端2] mapKind('task_think')='{m2.group(1)}'（应为 'think' 才能被 thinkEventsByTask 归并）"
        )
    else:
        print("[前端2] OK  mapKind('task_think')=='think'（归并到 TraceEvent 流）")

    # [3] thinkEventsByTask 按 task_id 归并 kind==='think' 事件。
    if "thinkEventsByTask" not in panel:
        errs.append("[前端3] ChatPanel 未定义 thinkEventsByTask（task 18 归并未接线）")
    else:
        # 抽 thinkEventsByTask useMemo 块，确认它过滤 kind==='think'
        m3 = re.search(
            r"const thinkEventsByTask = useMemo\(\(\) => \{.*?return m\s*\}, \[events\]\)",
            panel,
            re.S,
        )
        if not m3:
            errs.append("[前端3] thinkEventsByTask useMemo 块结构不符")
        elif "e.kind !== 'think'" not in m3.group(0) and "kind === 'think'" not in m3.group(0):
            errs.append("[前端3] thinkEventsByTask 未按 kind==='think' 过滤")
        else:
            print("[前端3] OK  thinkEventsByTask 按 task_id 归并 kind=='think' 事件")

        # 流式 + 定稿气泡都传 thinkEvents
        n_stream = panel.count("thinkEvents={thinkEventsByTask[b.taskId] || []}")
        if n_stream < 2:
            errs.append(
                f"[前端3] 仅 {n_stream} 处气泡传 thinkEvents（应 ≥2：流式 + 定稿气泡）"
            )
        else:
            print(f"[前端3] OK  {n_stream} 处气泡传 thinkEvents={{thinkEventsByTask[b.taskId]}}")

    # [4] ChatMessageBubble 有 hasThinks 守卫 + Collapse items 渲染 thinkRows。
    if "hasThinks" not in bubble:
        errs.append("[前端4] ChatMessageBubble 无 hasThinks 守卫（task 19 未渲染折叠块）")
    elif "thinkRows" not in bubble:
        errs.append("[前端4] ChatMessageBubble 无 thinkRows（折叠块 items 未构造）")
    else:
        # 确认有 Collapse + thinkRows.map items + phase 标签
        has_collapse = "Collapse" in bubble and "thinkRows.map" in bubble
        has_phase_label = "'final' ? '结论' : '思考'" in bubble or "结论" in bubble
        if not has_collapse:
            errs.append("[前端4] ChatMessageBubble 未用 Collapse 渲染 thinkRows（折叠块缺失）")
        elif not has_phase_label:
            errs.append("[前端4] ChatMessageBubble 折叠块无 phase 标签（思考/结论）")
        else:
            print("[前端4] OK  ChatMessageBubble hasThinks 守卫 + Collapse items + phase 标签（思考/结论）")

    return errs


async def main() -> int:
    print("=== 验证B-1：task_think 折叠块在气泡内可见且不与最终回复重复 ===\n")

    # ── 阶段 A：前端静态契约（不依赖后端在线）──
    print("── 阶段 A：前端静态契约断言 ──")
    fe_errs = assert_frontend_contract()
    if fe_errs:
        print("\n[阶段A] FAIL:")
        for e in fe_errs:
            print(f"  - {e}")
        # 前端契约失败直接 FAIL（后端运行时不必再跑）
        print("\n=== 结果: FAIL（前端契约） ===")
        return 1
    print("[阶段A] PASS\n")

    # ── 阶段 B：后端运行时（worker execute 产生 task_think）──
    print("── 阶段 B：后端运行时（worker execute 产生 task_think）──")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 等 worker 空闲
    for _ in range(30):
        st = await worker_status()
        if st == "idle":
            break
        print(f"[wait] worker 状态={st}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print(f"[worker] {WORKER_ID} idle")

    # 清理残留产物
    data_dir = os.environ.get(
        "MULTI_AGENT_DATA_DIR", str(Path.home() / ".local" / "share" / "multi-agent")
    )
    out_path = Path(data_dir) / "workspaces" / GROUP_ID / OUT_FILE
    if out_path.exists():
        out_path.unlink()
        print(f"[cleanup] 删除残留产物 {out_path.name}")

    # 并发：连 WS + 发消息
    ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events = await ws_task

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    # ── 抽取各类事件 ──
    think_events = [e for e in events if e.get("type") == "task_think"]
    think_thinking = [e for e in think_events if (e.get("data") or {}).get("phase") == "thinking"]
    think_final = [e for e in think_events if (e.get("data") or {}).get("phase") == "final"]
    token_events = [e for e in events if e.get("type") == "task_token"]
    complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
    failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)
    # task 收尾后落地的持久化 agent_reply（sender=worker, type=agent_reply, 时间最晚）
    reply_ev = next(
        (e for e in reversed(events)
         if e.get("type") == "agent_reply" and e.get("sender_id") == WORKER_ID),
        None,
    )

    print(f"[check 5] task_think thinking={len(think_thinking)} final={len(think_final)} token={len(token_events)}")
    if think_thinking:
        print(f"  [thinking样本] {str(think_thinking[0].get('content') or '')[:80]!r}")
    if think_final:
        print(f"  [final样本] {str(think_final[-1].get('content') or '')[:80]!r}")
    if reply_ev:
        print(f"  [reply样本] {str(reply_ev.get('content') or '')[:80]!r}")

    # ── 校验 5：worker execute 产生 task_think（≥1，final ≥1）──
    # thinking 不强制——DeepSeek 在工具调用前常只发 tool_calls 无文本，agent_loop 的
    # ``if ai_content:`` 守卫跳过空 think（模型行为非管道 bug）。
    c5_errs: list[str] = []
    if not think_events:
        c5_errs.append("未捕获任何 task_think 事件——execute 路径未流出思考/答案（折叠块无内容）")
    if not think_final:
        c5_errs.append("未捕获 task_think(phase=final)——最终答案未流出（「结论」折叠项无内容）")
    if not c5_errs:
        print(
            f"[check 5] OK  worker execute 产生 task_think {len(think_events)} 条"
            f"（thinking={len(think_thinking)} final={len(think_final)}）——折叠块有内容可渲染"
        )
    else:
        for e in c5_errs:
            print(f"  [check 5] {e}")

    # ── 校验 6：「不重复」——final.content != reply.content ──
    c6_errs: list[str] = []
    final_text = str(think_final[-1].get("content") or "") if think_final else ""
    reply_text = str(reply_ev.get("content") or "") if reply_ev else ""
    if not final_text:
        c6_errs.append("无 task_think(final) 文本，无法比对重复")
    elif not reply_text:
        c6_errs.append("无持久化 agent_reply，无法比对重复（task 收尾后未落地回复）")
    elif final_text == reply_text:
        # final 是裸答案 output[:200]，reply 是「任务完成 🎉\n{snippet}」——
        # 仅当 output 为空（reply 退化为「任务完成 🎉」、final 也为空）才会相等，
        # 此时两者都空，非真实重复。故额外校验「非空相等」才算重复缺陷。
        if final_text.strip():
            c6_errs.append(
                f"task_think(final).content == agent_reply.content（文本完全相同 → 重复缺陷）："
                f"final={final_text[:60]!r} reply={reply_text[:60]!r}"
            )
        else:
            # 两者都空，跳过（非重复缺陷，但 final 空已在 check 5 判定）
            pass
    else:
        print(f"[check 6] OK  final≠reply（不重复）：final={final_text[:40]!r} reply={reply_text[:40]!r}")

    # ── 校验 7：「同源不丢」——reply.content 含 final.content（snippet 同源）──
    c7_errs: list[str] = []
    if final_text and reply_text:
        # reply = "任务完成 🎉\n" + output[:200]，final = output[:200]
        # 故 reply 应包含 final（除非 final 被截断到 reply snippet 之外——
        # 两者都是 output[:200]，理论上 final ⊆ reply 的 snippet 段）。
        # 容错：final 可能比 reply 的 snippet 段长（若 reply snippet 被进一步截断），
        # 降级为「reply 含 final 的前 N 字」或「final 含 reply 去前缀后的片段」。
        reply_snippet = reply_text
        # 去掉「任务完成 🎉\n」前缀（若存在）后比对
        prefix = "任务完成 🎉"
        if reply_snippet.startswith(prefix):
            reply_snippet = reply_snippet[len(prefix):].lstrip("\n")
        if final_text in reply_text or reply_snippet in final_text or final_text[:30] in reply_text:
            print(f"[check 7] OK  reply 含 final 内容（snippet 同源，答案不丢）")
        else:
            c7_errs.append(
                f"reply 不含 final 内容（snippet 不同源，答案可能丢失）："
                f"final={final_text[:60]!r} reply_snippet={reply_snippet[:60]!r}"
            )
    else:
        c7_errs.append("final 或 reply 为空，无法校验同源")

    # ── 校验 8：task_think 事件 task_id == task_complete.task_id（同属 execute 路径）──
    # task_think + task_tool + task_log + task_complete 共享 tq_ 任务 id（_run_worker_task
    # 传 task_id 给 on_log → emit_task_think/tool/log/completed）。task_token 的 task_id
    # 是混合的（brain reply_id 裸 hex + execute tq_，task 24/25 双路径设计），不在本断言内
    # （task_token 归并是 task_token 通路的事，与 task_think 折叠块可见性无关）。
    c8_errs: list[str] = []
    success = complete_ev is not None and failed_ev is None
    if not success:
        tail = (complete_ev or failed_ev or {}).get("content", "")
        c8_errs.append(f"任务未以 task_complete(success) 收尾 (tail={str(tail)[:80]!r})")

    # task_think 事件 task_id 与 task_complete 一致（thinkEventsByTask[tq_] 取的 key）
    think_tids = {e.get("task_id") for e in think_events if e.get("task_id")}
    complete_tid = complete_ev.get("task_id") if complete_ev else None
    if complete_tid and think_tids and len(think_tids) == 1 and complete_tid in think_tids:
        print(
            f"[check 8] OK  task_think task_id 一致：{complete_tid[:16]}... == task_complete"
            f"（thinkEventsByTask[{complete_tid[:16]}...] 取到 think 事件 → 折叠块挂对气泡）"
        )
    else:
        c8_errs.append(
            f"task_think task_id 与 task_complete 不一致：think_tids={think_tids} complete_tid={complete_tid}"
        )

    disk_ok = out_path.exists() and out_path.stat().st_size > 0
    if not disk_ok:
        c8_errs.append(f"磁盘产物 {OUT_FILE} 未落盘或为空——write_file 未真实执行")
    else:
        print(f"[check 8] OK  task_complete(success) + 磁盘产物落盘（size={out_path.stat().st_size}）")

    # ── 汇总 ──
    all_errs = c5_errs + c6_errs + c7_errs + c8_errs
    if all_errs:
        print("\n[阶段B] FAIL:")
        for e in all_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（运行时） ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(
        "task_think 折叠块在气泡内可见且不与最终回复重复：\n"
        "  · 前端契约：CHAT_MESSAGE_TYPES 不含 task_think（不成独立气泡）+ "
        "mapKind→'think' + thinkEventsByTask 按 task_id 归并 + "
        "ChatMessageBubble hasThinks 折叠块渲染（思考/结论标签）；\n"
        "  · 运行时：worker execute 产生 task_think"
        f"（thinking={len(think_thinking)} final={len(think_final)}），"
        f"final≠reply（不重复），reply 含 final（同源不丢），"
        f"task_think 与 task_complete 同属 task {(complete_tid or '')[:8]}...，"
        f"task_complete(success) + 磁盘产物落盘。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
