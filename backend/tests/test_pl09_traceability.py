"""PL-09 自测：验证每步思考+工具调用+状态全程可追溯（LeaderPanel/WorkerTrace）。

不依赖 pytest，直接 asyncio 跑。沿用 PL-05/08 自测模式（WS 抓事件 + httpx 发请求 +
磁盘交叉验证）。PL-08 验证了「流式 token 逐字」单点；PL-09 验证的是「整条任务生命周期
的每一步都能被前端追溯」——即 WS 事件流（LeaderPanel/WorkerTrace 的唯一渲染数据源）
完整覆盖并有序串起：状态迁移 → 开始日志 → 逐轮思考 → 工具调用(start/end 配对) →
逐 token 流式 → 最终答案 → 完成 → 回 idle。

为何 @mention 直送 worker（不走 coordinator 全链路）：
  WorkerTrace 是事件类别最丰富的面板（status/think/tool/token/log/complete 五类齐备），
  一次多工具 worker 任务即可覆盖全部「每步」维度；LeaderPanel 的协调者侧（plan/dispatch/
  coord_think）已在 PL-01 验证。两个面板共用同一条 WS 事件流（useBusEvent 单源），
  故校验事件流完备有序即证明两面板均可追溯。直接 @worker 避开 coordinator chat/dispatch
  的不确定性，且单 worker 单任务内存占用低（规避 M12 自测的 exit 137 OOM）。

校验项（确定性，非语义判断）：
  1. 状态全程可追溯：agent_status 出现 executing(current_task_id 非空)→idle(current_task_id
     空)，且 executing 在 idle 之前——证明 WorkerTrace 状态徽标 idle→executing→idle 全程
     由 WS 事件驱动。
  2. 思考可追溯：task_think 出现 ≥1 条 phase=thinking（工具调用前的中间推理）且 ≥1 条
     phase=final（最终答案）——证明 WorkerTrace 思考链 + 最终答案块均从真实事件渲染。
  3. 工具调用可追溯且 start/end 配对：≥2 个不同工具被调用，每个工具的 start 事件先于其
     end 事件——证明 WorkerTrace 工具卡片（调用/返回徽标）从配对事件渲染。
  4. 流式可追溯：task_token ≥5 条——证明 WorkerTrace「正在生成」块在全生命周期中仍有
     逐 token 数据（PL-08 能力未回归）。
  5. 时序有序：executing.ts < 首个 task_tool.ts < 末个 task_tool.ts < task_complete.ts <
     idle.ts——证明事件是一条可重放的有序生命周期，而非乱序碎片。
  6. 同任务一致性：所有 task_* 事件共享同一 task_id，且 == agent_status(executing) 的
     current_task_id——证明整条 trace 属于同一次任务，可被前端按 task 串起。
  7. 前端可渲染：每个收到的 WS 事件 type 都在 useBusEvent 处理集合内（mapKind 映射或
     task_token 流式分支）——证明无「孤儿事件」漏渲染。
  8. task_complete(success) 收尾 + 磁盘交叉验证 write_file 产物真实落盘。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"
WORKER_ID = "agent_backend_1"

DATA_DIR = str(Path.home() / ".local" / "share" / "multi-agent")

# 产物文件名（worker 应通过 write_file 创建）。自测专属前缀避免与历史产物冲突。
OUT_FILE = "pl09_trace_report.md"
# PL-05 遗留产物，本任务要求 worker read_file 读它（确认工作区有此文件）。
READ_TARGET = "pl05_selftest_note.md"

# 多步骤任务：强制 worker 走「思考→工具」多轮，覆盖 list_dir/read_file/write_file/
# run_command 四件套 + 最终答案轮。每步都要求用工具，确保 brain 判定 execute。
TASK_CONTENT = (
    f"@后端工程师 请直接动手执行以下多步骤调查任务，必须使用你的工具完成，不要只是口头描述：\n"
    f"1. 用 list_dir 工具看一下当前工作区有哪些文件；\n"
    f"2. 用 read_file 工具读取 {READ_TARGET} 的内容；\n"
    f"3. 用 write_file 工具创建 {OUT_FILE}，写入你看到的工作区概况（3-4 行即可）；\n"
    f"4. 用 run_command 工具执行 `ls -1 {OUT_FILE}` 确认文件已创建；\n"
    f"5. 完成后用一句话回复结论。"
)

WS_TIMEOUT = 240.0  # 多轮工具调用，给足时间
MIN_TOKEN_EVENTS = 5
MIN_DISTINCT_TOOLS = 2

# useBusEvent 能处理（渲染或状态追踪）的事件 type 全集——超出此集合即为「孤儿」漏渲染。
HANDLED_TYPES = {
    "task_tool", "task_think", "task_log", "task_dispatch", "task_complete",
    "task_failed", "agent_status", "coordinator_plan", "coordinator_think",
    "agent_reply", "user_input", "task_token",
}


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
    """ISO 时间戳 → epoch 秒。解析失败用 0（仅用于排序，不影响断言）。"""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def workspace_file(rel: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / GROUP_ID / rel


async def main() -> int:
    print("=== PL-09 自测：每步思考+工具调用+状态全程可追溯 ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 等 worker 空闲（避免上一轮遗留 executing 导致任务进 backlog 延迟）
    for _ in range(30):
        st = await worker_status()
        if st == "idle":
            break
        print(f"[wait] worker 状态={st}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print(f"[worker] {WORKER_ID} idle")

    # 清理上一次可能残留的产物文件，确保本次 write_file 是真实新建
    out_path = workspace_file(OUT_FILE)
    if out_path.exists():
        out_path.unlink()
        print(f"[cleanup] 删除残留产物 {out_path.name}")

    # 确认 read_file 目标存在（PL-05 产物）；不存在则提示但不阻断（worker 可能改读别的）
    read_target = workspace_file(READ_TARGET)
    print(f"[precheck] read_file 目标 {READ_TARGET} 存在={read_target.exists()}")

    # 并发：连 WS + 发消息（先连 WS 确保不漏首批事件）
    ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id', '')[:16]}...")

    events = await ws_task

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    # ── 抽取各类事件 ──
    status_events = [e for e in events if e.get("type") == "agent_status" and e.get("sender_id") == WORKER_ID]
    think_events = [e for e in events if e.get("type") == "task_think"]
    tool_events = [e for e in events if e.get("type") == "task_tool"]
    token_events = [e for e in events if e.get("type") == "task_token"]
    complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
    failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)

    # ── 校验 1：状态全程可追溯 executing→idle ──
    exec_ev = next((e for e in status_events
                    if (e.get("data") or {}).get("status") == "executing"), None)
    idle_ev = next((e for e in status_events
                   if (e.get("data") or {}).get("status") == "idle"), None)
    exec_tid = (exec_ev.get("data") or {}).get("current_task_id") if exec_ev else None
    idle_tid = (idle_ev.get("data") or {}).get("current_task_id") if idle_ev else None
    status_ordered = (exec_ev and idle_ev and exec_tid
                      and parse_ts(exec_ev.get("timestamp")) < parse_ts(idle_ev.get("timestamp")))
    print(f"[check 1] 状态迁移 executing(tid={exec_tid})→idle(tid={idle_tid}) 有序={bool(status_ordered)}")

    # ── 校验 2：思考可追溯 thinking + final ──
    think_thinking = [e for e in think_events if (e.get("data") or {}).get("phase") == "thinking"]
    think_final = [e for e in think_events if (e.get("data") or {}).get("phase") == "final"]
    print(f"[check 2] task_think thinking={len(think_thinking)} final={len(think_final)}")
    if think_final:
        print(f"[final] 最终答案预览: {str(think_final[-1].get('content') or '')[:80]!r}")

    # ── 校验 3：工具调用可追溯 + start/end 配对 ──
    tools_used: dict[str, list[tuple[str, float]]] = {}  # name -> [(phase, ts)]
    for e in tool_events:
        d = e.get("data") or {}
        name = d.get("name", "?")
        phase = d.get("phase", "?")
        tools_used.setdefault(name, []).append((phase, parse_ts(e.get("timestamp"))))
    distinct_tools = list(tools_used.keys())
    # 每个工具 start 先于 end
    paired_ok = True
    for name, phases in tools_used.items():
        starts = [t for p, t in phases if p == "start"]
        ends = [t for p, t in phases if p == "end"]
        has_pair = bool(starts) and bool(ends)
        order_ok = has_pair and min(starts) < max(ends)
        paired_ok = paired_ok and has_pair and order_ok
        print(f"[check 3] 工具 {name}: start={len(starts)} end={len(ends)} 配对有序={has_pair and order_ok}")
    print(f"[check 3] 不同工具数={len(distinct_tools)} (要求 ≥{MIN_DISTINCT_TOOLS}) 全部配对={paired_ok}")

    # ── 校验 4：流式可追溯 ──
    n_tokens = len(token_events)
    print(f"[check 4] task_token 事件数={n_tokens} (要求 ≥{MIN_TOKEN_EVENTS})")

    # ── 校验 5：时序有序 executing < first_tool < last_tool < complete < idle ──
    first_tool_ts = min((parse_ts(e.get("timestamp")) for e in tool_events), default=0.0)
    last_tool_ts = max((parse_ts(e.get("timestamp")) for e in tool_events), default=0.0)
    exec_ts = parse_ts(exec_ev.get("timestamp")) if exec_ev else 0.0
    idle_ts_v = parse_ts(idle_ev.get("timestamp")) if idle_ev else 0.0
    complete_ts = parse_ts(complete_ev.get("timestamp")) if complete_ev else 0.0
    timeline_ok = (exec_ts and first_tool_ts and complete_ts and idle_ts_v
                   and exec_ts < first_tool_ts <= last_tool_ts < complete_ts < idle_ts_v)
    print(f"[check 5] 时序链 executing<{first_tool_ts:.2f}<tool<complete<{idle_ts_v:.2f} 有序={timeline_ok}")

    # ── 校验 6：同任务一致性（所有 task_* 事件 task_id 相同且 == exec current_task_id）──
    task_ids = {e.get("task_id") for e in (think_events + tool_events + token_events)
                if e.get("task_id")}
    same_task = len(task_ids) == 1
    task_id_matches_exec = exec_tid in task_ids if exec_tid else False
    print(f"[check 6] task_* 事件 task_id 集合={task_ids} 唯一={same_task} ==exec_tid={task_id_matches_exec}")

    # ── 校验 7：前端可渲染（无孤儿事件）──
    observed_types = set(type_counts.keys())
    orphan_types = observed_types - HANDLED_TYPES
    print(f"[check 7] 观察到 {len(observed_types)} 种事件类型, 孤儿类型={orphan_types or '无'}")

    # ── 校验 8：task_complete(success) + 磁盘交叉验证 ──
    success = complete_ev is not None and failed_ev is None
    disk_ok = out_path.exists() and out_path.stat().st_size > 0
    print(f"[check 8] task_complete(success)={success} 磁盘产物存在={disk_ok}"
          f" (size={out_path.stat().st_size if disk_ok else 0})")
    if disk_ok:
        print(f"[disk] 产物预览: {out_path.read_text(encoding='utf-8', errors='replace')[:120]!r}")

    # ── 汇总 ──
    errs = []
    if not status_ordered:
        errs.append(f"状态迁移未全程可追溯（executing→idle 有序失败: exec={bool(exec_ev)} idle={bool(idle_ev)} exec_tid={exec_tid}）")
    if not think_thinking:
        errs.append("未捕获 task_think(phase=thinking)——中间思考不可追溯")
    if not think_final:
        errs.append("未捕获 task_think(phase=final)——最终答案不可追溯")
    if len(distinct_tools) < MIN_DISTINCT_TOOLS:
        errs.append(f"仅调用 {len(distinct_tools)} 个工具（要求 ≥{MIN_DISTINCT_TOOLS}）——工具覆盖不足")
    if not paired_ok:
        errs.append("存在工具 start/end 未配对或顺序错乱——工具卡片不可完整追溯")
    if n_tokens < MIN_TOKEN_EVENTS:
        errs.append(f"task_token 仅 {n_tokens} 条（要求 ≥{MIN_TOKEN_EVENTS}）——流式在全生命周期中丢失")
    if not timeline_ok:
        errs.append("事件时序链不满足 executing<tool<complete<idle——生命周期不可有序重放")
    if not same_task:
        errs.append(f"task_* 事件 task_id 不一致（{task_ids}）——trace 非同一任务")
    if not task_id_matches_exec:
        errs.append("task_* 的 task_id 与 agent_status(executing).current_task_id 不符——trace 与状态未对齐")
    if orphan_types:
        errs.append(f"存在前端未处理的事件类型 {orphan_types}——孤儿事件漏渲染")
    if not success:
        tail = (complete_ev or failed_ev or {}).get("content", "")
        errs.append(f"任务未以 task_complete(success) 收尾 (tail={str(tail)[:80]!r})")
    if not disk_ok:
        errs.append(f"磁盘产物 {OUT_FILE} 未落盘或为空——write_file 未真实执行")

    if errs:
        print("\n[结果] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(f"一次多工具任务全生命周期可追溯：状态 idle→executing→idle；"
          f"思考 {len(think_thinking)} 轮(thinking)+1 轮(final)；"
          f"工具 {len(distinct_tools)} 种({distinct_tools}) 全部 start/end 配对；"
          f"流式 {n_tokens} token；时序链 executing→tool→complete→idle 有序；"
          f"所有 task_* 事件同属 task {next(iter(task_ids))[:8]}...；"
          f"无孤儿事件；task_complete(success) + 磁盘产物落盘。"
          f"证明 LeaderPanel/WorkerTrace 的 WS 事件源完整覆盖每一步。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
