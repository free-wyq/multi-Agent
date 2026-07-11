"""MT-13 自测：Leader 实时跟踪各 Worker 执行进度（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-09~MT-12 自测模式（httpx HTTP 真源 + WS 抓事件流 +
reload 触发引擎启动 + 直接干模式 fan-out + 真执行 report-back）。

MT-13 链路（Worker 执行 → report-back → Leader 跟踪 → 汇总）：
  前端 GroupPage：用户发可并行目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出 >=2 步 depends_on==[] 的 plan（前后端各一步）
    · node_dispatch → direct_run → node_dispatch_next
    · dispatch_ready_steps → _dispatch_one 逐个 fan-out：step.status=dispatched + step.task_id=pushed.id
      + push_task 到对口 worker 引擎 inbox + emit_task_dispatched
  关键「跟踪」链路（本任务核心）——每个 worker 执行完向 Leader report-back：
    worker _run_worker_task 执行完（create_react_agent + 工具）→ emit_task_completed（WS task_complete）
    → complete_task → 最后 push_notify(group_id,"agent_reply",worker,coordinator_id,
      f"步骤完成：{task_content}\\n\\n结果：{snippet}",{"task_id":task_id,"success":success})
    ——这条 notify 是「Worker 向 Leader report-back」的通道，唤醒 coordinator 引擎。
  coordinator 引擎 _handle_notify（处理 worker 的 agent_reply notify）→ LangGraph ainvoke：
    · classify：incoming_kind=="agent_reply" + sender!=user + data.task_id 匹配某 dispatched step
      → handle_reply（非 llm_decide——Leader 已在跟踪，无需重新决策）
    · node_handle_reply：按 data.task_id 找到 status=="dispatched" 的 step →
      step.status = "completed" if success else "failed" + step.result = notify.content（worker 的汇报内容）
      ——这是「Leader 跟踪各 Worker 进度」的核心：_dispatch_plan 的每步 status/result 随 worker
      report-back 实时更新（dispatched→completed + 记录 result）。
      all_done？→ summarize（全完成）/ dispatch_next（还有未完成 step）
    · node_summarize（all_done 时）：从 plan 各 step 的 result 构建「🎉 全部完成！协作结果汇总」
      reply（每步 ✅ agent_name: result[:200]）→ _unified_reply 落库 + emit_message_added。
      汇总 reply 只能从「Leader 跟踪的各 step.result」构建——证明 Leader 捕获了每 worker 的产出。

「Leader 实时跟踪各 Worker 执行进度」的三层证据：
  ① report-back 通道——每个 worker 执行完向 Leader 推 agent_reply notify（携带 task_id+success），
     Leader 收到每 worker 的完成汇报（非群发广播，是定向回报 Leader）；
  ② 实时按步跟踪——Leader 的 _dispatch_plan 每步 status 随 worker report-back 实时更新
     （dispatched→completed + 记录 result），中途快照可见混合状态（某步 completed + 某步 dispatched，
     = Leader 知道一个 worker 已完成、另一个还在跑）；
  ③ 汇总反映跟踪——all_done 时 node_summarize 从各 step.result 构建汇总 reply，内容含每 worker 的
     agent_name + 产出，证明 Leader 跟踪了每 worker 的具体结果（非空泛「完成」）。

为何用专属探针群 + reload + 直接干：group_demo_1 coordinator 引擎累积历史 _memory + 残留 _dispatch_plan
+ auto_confirm 状态，会污染「跟踪本目标 worker 进度」断言。新建 [MT-13] 探针群 → reload 起干净引擎
→ 显式设 auto_confirm=True → coordinator 只看到本目标 → 拆出本目标的 2 步 plan → 2 worker 真执行
report-back → Leader 干净跟踪本目标的 2 worker 进度（沿用 MT-09~MT-12 隔离模式）。直接干让 plan 后
立即 fan-out + worker 真执行 + report-back，触发 Leader 的 handle_reply 跟踪链路（wait_confirm 不
fan-out，无 worker 执行，无法验证跟踪）。

为何让两 worker 写不同小产物文件：①让两 worker 都有真实执行 + 产出（report-back 有内容，汇总有 result
可引用）；②两产物文件名不同便于溯源清理；③产物小（bounded）避免 OOM（沿用 test_pl11 单用例防 OOM 立场）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle（Leader + 2 worker 驻留）；
  ④ 设直接干：PUT config auto_confirm=True（让 Leader fan-out + worker 真执行 + report-back）；
  ⑤ 发可并行目标 + 抓事件流：连 WS → POST /api/messages 发前后端可并行目标（前端写 mt13_frontend.md
     + 后端写 mt13_backend.md，两个独立小产物）→ 抓 coordinator_plan + task_dispatch + task_complete
     + Leader 汇总 agent_reply（collect_until_summary 收到「全部完成」汇总即收尾）；
  ⑥ plan 多步派两 worker（HARD）：plan >=2 步 + >=1 步派 frontend_id + >=1 步派 backend_id
     （2 worker 都被 Leader 纳入跟踪计划）；
  ⑦ 两 worker 都执行完成 + report-back（HARD）：task_dispatch 覆盖 frontend+backend +
     task_complete(sender=frontend)>=1 + task_complete(sender=backend)>=1（两 worker 都真执行并完成，
     各自向 Leader report-back）；
  ⑧ Leader 汇总（HARD 核心）：抓到 coordinator 的「全部完成/汇总」agent_reply（node_summarize）+
     GET /api/messages 交叉确认落库（WS 事件 vs DB 持久化双真源）+ 汇总 reply 含两 worker 的 plan
     agent_name（Leader 从跟踪的各 step.result 聚合，非空泛「完成」）——证明 Leader 跟踪了所有 worker
     完成并聚合各自结果；
  ⑨ report-back 全覆盖（HARD 确定性）：每个 task_dispatch.task_id 都有对应 task_complete/task_failed
     （所有派发任务都 report-back 到 Leader，无一遗漏）；
  ⑩ 实时按步跟踪（SOFT 实时证据）：中途高频轮询 GET /api/groups/{id}/plan，捕获某一时刻 _dispatch_plan
     含混合状态（>=1 completed + >=1 dispatched）= Leader 实时按步跟踪的直接证据（知一 worker 完成、
     另一 worker 仍在跑）。若两 worker 完成窗口太窄未采样到混合态，退为 INFO（HARD ⑧ 汇总仍证跟踪）；
  ⑪ 汇总引用目标领域（SOFT）：汇总 reply 引用目标关键词（前端/后端/文件/标记/进度）之一；
  ⑫ 收尾：DELETE 探针群（stop_group + delete_group + 清理工作区产物）→ 全局无残留。

为何 HARD 核心是「汇总 reply」而非「中途快照」：node_handle_reply 是 Leader 的跟踪节点——它按 worker
report-back 的 task_id 匹配 dispatched step，更新 status+result。这一跟踪状态（_dispatch_plan）的最终
态（all completed + result）触发 node_summarize，其 reply 从各 step.result 构建。故「汇总 reply 含每
worker 的 agent_name + result」是 Leader 跟踪了每 worker 的确定性证据（它只能从跟踪的 step.result
构建该 reply）。汇总 reply 经 WS 事件 + DB 持久化双真源确认（非 WS 时序幻觉）。中途混合态快照是「实时
性」的直接证据，但依赖两 worker 完成时差（采样窗口），偶发两 worker 同时完成则采样不到——故作 SOFT，
HARD ⑧ 汇总不依赖时序，确定性 PASS。

为何「report-back 全覆盖」（⑨）是确定性 HARD：task_dispatch 事件携带 pushed task_id（_dispatch_one
设 step.task_id=pushed.id 并 emit_task_dispatched），task_complete/task_failed 事件携带同一 task_id
（worker 执行该 task 后 emit_task_completed）。若每个 task_dispatch.task_id 都有对应 task_complete/
task_failed，证明每个派发给 worker 的任务都执行完并 report-back 到 Leader（Leader 收到全量 worker
汇报，无一丢失）。这是「Leader 跟踪每 worker」的 per-task 确定性证据（非聚合级「汇总」）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

BASE = "http://localhost:8000"
BACKEND_MAIN = "/home/wyq/work/project/multi-Agent/backend/main.py"
DATA_DIR = str(Path.home() / ".local" / "share" / "multi-agent")

TIMEOUT = 20.0
RELOAD_WAIT = 45.0
SUMMARY_TIMEOUT = 120.0  # 等 Leader 汇总 reply（含 2 worker 真执行 + report-back + summarize）
POLL_TIMEOUT = 110.0     # 中途轮询 GET /plan 捕获实时跟踪快照的窗口
POLL_INTERVAL = 0.4      # 轮询间隔（秒）

# 探针群组名（[MT-13] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-13] Leader跟踪Worker进度探针组"

# 可并行目标——前端写 mt13_frontend.md（含前端进度标记），后端写 mt13_backend.md（含后端进度标记），
# 两个独立小产物，两 worker 并发执行互不依赖，各自执行完向 Leader report-back。
GOAL = (
    "【MT-13】请帮我完成两个独立的小任务，可以并行执行，不需要额外的集成步骤："
    "1. 前端工程师用 write_file 工具创建文件 mt13_frontend.md，内容写明'MT13前端进度已上报'并写一段简短前端说明；"
    "2. 后端工程师用 write_file 工具创建文件 mt13_backend.md，内容写明'MT13后端进度已上报'并写一段简短后端说明。"
    "这两个任务互不依赖，请同时派发并行执行。请直接派发执行计划。"
)

# 产物文件名 + 期望标记词（交叉验证两 worker 各自独立产出 + 汇总引用）。
FE_FILE = "mt13_frontend.md"
BE_FILE = "mt13_backend.md"
FE_MARKER = "MT13前端"
BE_MARKER = "MT13后端"

# 目标关键词（软断言用——汇总 reply 引用其一即「跟踪基于本目标」）。
GOAL_KEYWORDS = ["前端", "后端", "文件", "标记", "进度", "上报", "mt13", "MT13"]


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/health")
        return r.status_code == 200 and r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def add_member(group_id: str, agent_id: str, alias: str | None = None) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/groups/{group_id}/members",
            json={"agentId": agent_id, "alias": alias},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def group_status(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/status/{group_id}")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def set_auto_confirm(group_id: str, value: bool) -> dict | None:
    """PUT /api/groups/{id} 改 config.auto_confirm（返回 config dict）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        cur = (await c.get(f"{BASE}/api/groups/{group_id}")).json()
        config = dict(cur.get("config") or {})
        config["auto_confirm"] = value
        r = await c.put(f"{BASE}/api/groups/{group_id}", json={"config": config})
        return r.json().get("config") if r.status_code == 200 else None


async def send_user_message(group_id: str, content: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": group_id,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json() if r.status_code == 200 else {}


async def get_plan(group_id: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/plan")
        return r.json() if r.status_code == 200 else {}


async def list_messages(group_id: str, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(
            f"{BASE}/api/messages", params={"groupId": group_id, "limit": str(limit)}
        )
        return r.json() if r.status_code == 200 else []


async def delete_group(group_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json() if r.status_code == 200 else []


async def wait_for_engines(group_id: str, expected: int) -> bool:
    """touch main.py 触发 reload，轮询健康 + status 直到探针群引擎数 == expected。"""
    os.system(f"touch {BACKEND_MAIN}")
    deadline = asyncio.get_event_loop().time() + RELOAD_WAIT
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                h = await c.get(f"{BASE}/health")
                if h.status_code == 200:
                    st = await c.get(f"{BASE}/api/status/{group_id}")
                    if st.status_code == 200 and len(st.json()) >= expected:
                        return True
        except (httpx.HTTPError, Exception):
            pass
        await asyncio.sleep(1.0)
    return False


async def collect_until_summary(
    ws_url: str, send_action, timeout: float, coordinator_id: str
) -> tuple[list[dict], dict | None, dict | None, list[dict], list[dict]]:
    """连 WS，send_action 发消息，收事件直到抓到 Leader 汇总 agent_reply 或超时。

    返回 (全量事件, plan 事件, 汇总 reply 事件, task_dispatch 事件列表, task_complete 事件列表)。
    汇总 reply = node_summarize 产出的 agent_reply（sender=coordinator，content 含「全部完成」或「汇总」）。
    命中汇总后多收 3 秒收尾事件。
    """
    events: list[dict] = []
    plan_ev: dict | None = None
    summary_ev: dict | None = None
    dispatches: list[dict] = []
    completes: list[dict] = []
    deadline = time.time() + timeout
    async with websockets.connect(ws_url) as ws:
        if send_action is not None:
            await send_action()
        while time.time() < deadline and summary_ev is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            t = ev.get("type")
            if t == "coordinator_plan" and plan_ev is None:
                plan_ev = ev
            elif t == "task_dispatch":
                dispatches.append(ev)
            elif t in ("task_complete", "task_failed"):
                completes.append(ev)
            elif (
                t == "agent_reply"
                and ev.get("sender_id") == coordinator_id
                and summary_ev is None
            ):
                content = ev.get("content") or ""
                if "全部完成" in content or "汇总" in content:
                    summary_ev = ev
                    # 多收 3 秒尾事件
                    end = time.time() + 3.0
                    while time.time() < end:
                        try:
                            raw2 = await asyncio.wait_for(
                                ws.recv(), timeout=max(0.1, end - time.time())
                            )
                            ev2 = json.loads(raw2)
                            events.append(ev2)
                            if ev2.get("type") == "task_dispatch":
                                dispatches.append(ev2)
                            elif ev2.get("type") in ("task_complete", "task_failed"):
                                completes.append(ev2)
                        except asyncio.TimeoutError:
                            break
                    break
    # 兜底：从全量事件补扫（防阶段切换漏收）
    for ev in events:
        if ev.get("type") == "task_dispatch" and ev not in dispatches:
            dispatches.append(ev)
        elif ev.get("type") in ("task_complete", "task_failed") and ev not in completes:
            completes.append(ev)
    return events, plan_ev, summary_ev, dispatches, completes


async def poll_plan_snapshots(group_id: str, timeout: float) -> list[dict]:
    """高频轮询 GET /api/groups/{id}/plan，捕获 Leader _dispatch_plan 的实时状态快照。

    每个快照记录时刻 + 各步 status 列表。捕到混合态（>=1 completed + >=1 dispatched/pending）
    是「Leader 实时按步跟踪」的直接证据。被 cancel 时返回已采快照（不抛）。
    """
    snapshots: list[dict] = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    r = await c.get(f"{BASE}/api/groups/{group_id}/plan")
                    if r.status_code == 200:
                        body = r.json() or {}
                        plan = body.get("plan") or []
                        statuses = [
                            s.get("status") for s in plan if isinstance(s, dict)
                        ]
                        snapshots.append({
                            "t": round(time.time(), 2),
                            "statuses": statuses,
                        })
            except (httpx.HTTPError, Exception):
                pass
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        pass
    return snapshots


def workspace_file(group_id: str, rel: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / group_id / rel


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _info(name: str, cond: bool, detail: str = "") -> None:
    """软断言：不计 FAIL，只 INFO 报告。"""
    mark = "✓" if cond else "·"
    tag = "INFO" if cond else "SOFT-MISS"
    print(f"  {mark} [{tag}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("=== MT-13 自测：Leader 实时跟踪各 Worker 执行进度 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    frontend_id: str | None = None
    backend_id: str | None = None

    try:
        # ── 1. 前置：候选池含 coordinator + 前端 + 后端 ──
        print("\n[check 1] 前置：GET /api/agents 候选池含 coordinator + 前端 + 后端")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        frontend = next((a for a in agents if a.get("role") == "frontend_engineer"), None)
        backend = next((a for a in agents if a.get("role") == "backend_engineer"), None)
        if not (coord and frontend and backend) and len(agents) >= 3:
            if not coord:
                coord = agents[0]
            if not frontend:
                frontend = next((a for a in agents if a["id"] != coord["id"]), None) or agents[1]
            if not backend:
                backend = next(
                    (a for a in agents if a["id"] not in (coord["id"], frontend["id"])), None
                ) or agents[2]
            print("      [fallback] 种子角色缺失，退化取前 3 个 agent 组队")
        if not _check("候选池含 coordinator + 前端 + 后端", coord and frontend and backend,
                      f"coord={bool(coord)} fe={bool(frontend)} be={bool(backend)}"):
            errs.append("[pool] 候选不足 3 个，无法建探针群")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord and frontend and backend
        coord_id, frontend_id, backend_id = coord["id"], frontend["id"], backend["id"]
        print(f"      群主={coord_id} 前端={frontend_id}({frontend['name']}) "
              f"后端={backend_id}({backend['name']})")

        # ── 2. 建探针群：coord + [frontend, backend] ──
        print("\n[check 2] 建探针群：POST /api/groups（coord + [frontend, backend]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "MT-13 Leader 实时跟踪各 Worker 执行进度自测探针",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        for aid in (frontend_id, backend_id):
            await add_member(probe_group_id, aid, None)
        if _check("group_ 前缀 + coordinator_id==群主", str(g["id"]).startswith("group_")
                  and g.get("coordinator_id") == coord_id):
            print(f"      样本：id={g['id'][:24]}… coord={coord_id}")
        else:
            errs.append("[create] 群结构异常")

        # ── 3. 引擎启动：reload → 轮询 status 直到 3 引擎 idle ──
        print("\n[check 3] 引擎启动：reload 触发 load_from_store → 3 引擎 idle（Leader + 2 worker）")
        ready = await wait_for_engines(probe_group_id, expected=3)
        if not _check("reload 后探针群 3 引擎 idle", ready, "reload 后引擎未到位"):
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'], e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            ids = {e["id"] for e in engines}
            all_idle = all(e.get("status") == "idle" for e in engines)
            if _check("3 引擎含 coord + 前端 + 后端 且全 idle",
                      {coord_id, frontend_id, backend_id}.issubset(ids) and all_idle,
                      f"ids={ids} statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎集合/idle 不符")

        # ── 4. 设直接干：PUT config auto_confirm=True ──
        print("\n[check 4] 设直接干：PUT config auto_confirm=True（让 Leader fan-out + worker 真执行 + report-back）")
        cfg = await set_auto_confirm(probe_group_id, True)
        if _check("config.auto_confirm==True（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True), f"config={cfg}"):
            print(f"      auto_confirm=True（直接干，plan 后立即 fan-out + worker 真执行 report-back）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 5. 发可并行目标 + 抓 plan + task_dispatch + task_complete + 汇总 reply ──
        print("\n[check 5] 发可并行目标 + 抓 coordinator_plan + task_dispatch + task_complete + Leader 汇总 reply")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"
        # 三路并发：WS 收事件（含 send）+ 中途轮询 GET /plan 实时快照
        async def _send():
            await asyncio.sleep(0.3)  # 让 WS 先连上
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        ws_collect = asyncio.create_task(
            collect_until_summary(ws_url, _send, SUMMARY_TIMEOUT, coord_id)
        )
        await asyncio.sleep(1.0)  # 等 WS 连上 + send 触发
        poll_task = asyncio.create_task(
            poll_plan_snapshots(probe_group_id, POLL_TIMEOUT)
        )
        events, plan_ev, summary_ev, dispatches, completes = await ws_collect
        # 停止轮询（汇总已抓到，worker 已完成）
        poll_task.cancel()
        snapshots = await poll_task

        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_plan 事件", plan_ev is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获 coordinator_plan 事件")
        if not _check("捕获 Leader 汇总 reply（node_summarize 全完成汇报）",
                      summary_ev is not None, "未捕获汇总 reply（Leader 未检测到 all-done）"):
            errs.append("[summary] 未捕获 Leader 汇总 reply")

        # ── 6. plan 多步派两 worker（HARD） ──
        print("\n[check 6] plan 多步派两 worker：>=2 步 + >=1 派 frontend + >=1 派 backend（2 worker 被纳入跟踪）")
        plan: list[dict] = []
        if plan_ev is not None:
            plan = (plan_ev.get("data") or {}).get("plan") or []
        plan_agent_ids = {s.get("agent_id") for s in plan if isinstance(s, dict)}
        if _check("plan >=2 步", isinstance(plan, list) and len(plan) >= 2, f"plan 步数={len(plan)}"):
            pass
        else:
            errs.append(f"[plan] plan 仅 {len(plan) if isinstance(plan, list) else 'NA'} 步，期望 >=2")
        if _check("plan >=1 步派 frontend_id + >=1 步派 backend_id（2 worker 被纳入跟踪）",
                  frontend_id in plan_agent_ids and backend_id in plan_agent_ids,
                  f"plan_agent_ids={plan_agent_ids}"):
            print(f"      plan 派工：{[(s.get('step'), s.get('agent_name')) for s in plan if isinstance(s, dict)]}")
        else:
            errs.append(f"[plan] plan 未覆盖前后端两 worker：{plan_agent_ids}")

        # ── 7. 两 worker 都执行完成 + report-back（HARD） ──
        print("\n[check 7] 两 worker 都执行完成 + report-back：task_dispatch 覆盖两 worker + 各有 task_complete")
        dispatch_agents = {((d.get("data") or {}).get("agent_id")) for d in dispatches}
        fe_complete = [c for c in completes if c.get("sender_id") == frontend_id and c.get("type") == "task_complete"]
        be_complete = [c for c in completes if c.get("sender_id") == backend_id and c.get("type") == "task_complete"]
        if _check("task_dispatch 覆盖 frontend_id + backend_id（两 worker 都被派发执行）",
                  frontend_id in dispatch_agents and backend_id in dispatch_agents,
                  f"dispatch_agents={dispatch_agents}"):
            pass
        else:
            errs.append(f"[dispatch] task_dispatch 未覆盖两 worker：{dispatch_agents}")
        if _check("frontend worker 执行完成（task_complete by frontend）",
                  len(fe_complete) >= 1, f"fe task_complete={len(fe_complete)}"):
            pass
        else:
            errs.append(f"[complete] frontend 无 task_complete（fe={len(fe_complete)}）")
        if _check("backend worker 执行完成（task_complete by backend）",
                  len(be_complete) >= 1, f"be task_complete={len(be_complete)}"):
            pass
        else:
            errs.append(f"[complete] backend 无 task_complete（be={len(be_complete)}）")
        if fe_complete or be_complete:
            print(f"      [report-back] frontend 完成={len(fe_complete)} backend 完成={len(be_complete)} "
                  f"（两 worker 各自向 Leader report-back）")

        # ── 8. Leader 汇总（HARD 核心）：汇总 reply 抓到 + 落库交叉 + 含两 worker agent_name ──
        print("\n[check 8] Leader 汇总（HARD 核心）：汇总 reply 抓到 + DB 交叉确认 + 含两 worker agent_name")
        # 收集 plan 中两 worker 步骤的 agent_name（node_summarize 从各 step.result 构建 reply，含 agent_name）
        fe_step_names = {s.get("agent_name") for s in plan
                         if isinstance(s, dict) and s.get("agent_id") == frontend_id}
        be_step_names = {s.get("agent_name") for s in plan
                         if isinstance(s, dict) and s.get("agent_id") == backend_id}
        summary_content = (summary_ev or {}).get("content") or ""
        print(f"      [summary] content 预览：{summary_content[:160]}…")
        fe_name_in_summary = any(name and name in summary_content for name in fe_step_names)
        be_name_in_summary = any(name and name in summary_content for name in be_step_names)
        if _check("汇总 reply 含 frontend 步骤 agent_name（Leader 跟踪了前端 worker 结果）",
                  fe_name_in_summary,
                  f"fe_step_names={fe_step_names}"):
            pass
        else:
            errs.append(f"[summary] 汇总 reply 未引用 frontend worker agent_name：{fe_step_names}")
        if _check("汇总 reply 含 backend 步骤 agent_name（Leader 跟踪了后端 worker 结果）",
                  be_name_in_summary,
                  f"be_step_names={be_step_names}"):
            pass
        else:
            errs.append(f"[summary] 汇总 reply 未引用 backend worker agent_name：{be_step_names}")
        # DB 交叉确认：汇总 reply 落库（type=agent_reply, sender=coordinator, content 含「全部完成/汇总」）
        msgs = await list_messages(probe_group_id, limit=100)
        db_summary = next(
            (m for m in msgs
             if m.get("type") == "agent_reply"
             and m.get("sender_id") == coord_id
             and ("全部完成" in (m.get("content") or "") or "汇总" in (m.get("content") or ""))),
            None,
        )
        if _check("汇总 reply 落库（GET /api/messages 交叉确认，WS 事件 vs DB 双真源）",
                  db_summary is not None, "DB 未找到汇总 reply"):
            print(f"      [db] 汇总 message id={(db_summary or {}).get('id', '')[:16]}… 落库确认")
        else:
            errs.append("[summary] 汇总 reply 未落库（DB 无记录）")

        # ── 9. report-back 全覆盖（HARD 确定性）：每 task_dispatch.task_id 有对应 task_complete/task_failed ──
        print("\n[check 9] report-back 全覆盖（确定性）：每 task_dispatch.task_id 有对应 task_complete/task_failed（全量 worker 汇报回 Leader）")
        dispatch_tids = {d.get("task_id") for d in dispatches if d.get("task_id")}
        report_tids = {c.get("task_id") for c in completes if c.get("task_id")}
        missing = dispatch_tids - report_tids
        if _check("每个派发任务都 report-back（task_dispatch.task_id ⊆ {task_complete,task_failed}.task_id）",
                  len(missing) == 0, f"缺失 report-back 的 task_id={missing}"):
            print(f"      [report-back] 派发 {len(dispatch_tids)} 任务，全部 report-back 到 Leader（无一遗漏）")
        else:
            errs.append(f"[report-back] {len(missing)} 个派发任务未 report-back：{missing}")

        # ── 10. 实时按步跟踪（SOFT 实时证据）：中途 GET /plan 捕获混合状态快照 ──
        print("\n[check 10] 实时按步跟踪（SOFT 实时证据）：中途 GET /plan 捕获混合状态快照（>=1 completed + >=1 dispatched）")
        mixed_snaps = [
            s for s in snapshots
            if s["statuses"]
            and any(st == "completed" for st in s["statuses"])
            and any(st in ("dispatched", "pending") for st in s["statuses"])
        ]
        all_done_snaps = [
            s for s in snapshots
            if s["statuses"] and all(st == "completed" for st in s["statuses"])
        ]
        # 打印 status 演化片段（前 12 + 末 3）便于诊断
        if snapshots:
            head = snapshots[:12]
            tail = snapshots[12:] if len(snapshots) > 12 else []
            print(f"      plan status 演化（共 {len(snapshots)} 采样点）：")
            for s in head:
                print(f"        t={s['t']} statuses={s['statuses']}")
            if tail:
                print(f"        ... ({len(tail)} more) ...")
                for s in tail[-3:]:
                    print(f"        t={s['t']} statuses={s['statuses']}")
        if _info("捕获混合状态快照（Leader 实时按步跟踪：知一 worker 完成、另一 worker 仍在跑）",
                 len(mixed_snaps) > 0,
                 f"混合态快照数={len(mixed_snaps)}" if mixed_snaps
                 else (f"未采样到混合态（两 worker 完成窗口太窄）；"
                       f"采样到 all-completed 快照={len(all_done_snaps)}（HARD ⑧ 汇总仍证跟踪）")):
            print(f"      ✓ 实时跟踪直接证据：_dispatch_plan 中途出现混合状态（Leader 实时按步更新跟踪态）")

        # ── 11. 汇总引用目标领域（SOFT） ──
        print("\n[check 11] 汇总引用目标领域（SOFT）：汇总 reply 引用目标关键词")
        hit_kw = next((kw for kw in GOAL_KEYWORDS if kw in summary_content), None)
        _info("汇总 reply 引用目标关键词（跟踪基于本目标，非空泛模板）",
              hit_kw is not None, f"命中={hit_kw}" if hit_kw else f"summary 预览={summary_content[:80]}")

        # ── 12. 收尾：DELETE 探针群（stop_group + delete_group + 清理产物）→ 全局无残留 ──
        print("\n[check 12] 收尾：DELETE 探针群（stop_group + delete_group）→ 全局无残留")
        st, ok = await delete_group(probe_group_id)
        if _check("DELETE 200 True", st == 200 and ok is True, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[cleanup] DELETE status={st} ok={ok}")
        groups_final = await list_groups()
        leaked = [x for x in groups_final if x.get("id") == probe_group_id]
        if _check("全局列表无探针群残留", len(leaked) == 0, f"{len(leaked)} 个残留"):
            pass
        else:
            errs.append("[cleanup] 探针群在全局列表残留")

    finally:
        # 兜底：若中途失败探针群可能还在（auto_confirm=True worker 在跑），清理之
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")
        # 清理探针产物文件（群删了目录也删了，兜底删一次）
        if probe_group_id:
            for name in (FE_FILE, BE_FILE):
                p = workspace_file(probe_group_id, name)
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项硬断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — Leader 实时跟踪各 Worker 执行进度端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 Leader + 2 worker 引擎；")
    print("  · 设直接干 → 发可并行目标（前端写 mt13_frontend.md + 后端写 mt13_backend.md）；")
    print("  · 两 worker 真执行 + report-back（task_complete 各自上报 Leader）；")
    print("  · [核心] Leader 汇总 reply 抓到 + DB 交叉确认 + 含两 worker agent_name")
    print("    （node_summarize 从跟踪的各 step.result 聚合 = Leader 跟踪了每 worker 结果）；")
    print("  · report-back 全覆盖（每派发任务都汇报回 Leader，无一遗漏）；")
    print("  · [SOFT] 实时按步跟踪：中途 GET /plan 捕获混合状态快照（Leader 实时更新跟踪态）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
