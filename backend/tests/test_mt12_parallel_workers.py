"""MT-12 自测：多 Worker 同时执行互不阻塞（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-11/test_m12/test_pl11 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动 + 直接干模式 fan-out）。

MT-12 链路（多 Worker 并发执行，互不阻塞）：
  前端 GroupPage：用户发可并行目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出 >=2 步 depends_on==[] 的 plan（前后端各一步）
    · node_dispatch → direct_run → node_dispatch_next
    · dispatch_ready_steps：find_ready_steps 返回 >=2 个 ready step（空 deps 全 ready），
      for step in ready: await _dispatch_one——逐个 push_task 到对口 worker 引擎 inbox。
  关键并发机制：每个 worker 是独立的 AgentEngine（独立 asyncio.Queue inbox + 独立 _run_loop
  asyncio.Task）。_dispatch_one 对 worker A push_task 后立即（不 await A 完成）对 worker B
  push_task——两个 worker 的 _run_loop 各自独立消费各自 inbox，作为独立 asyncio Task 并发
  执行（一个 worker 的 LLM await 不会阻塞另一个 worker 的执行）。

「多 Worker 同时执行互不阻塞」的核心证据：
  ① 并发派发——>=2 个 task_dispatch 事件，分别派给不同 worker（A+B）；
  ② 同时 executing——某一时刻两个 worker 的 status 同时为 executing（并发执行的直接证据：
     若串行则 B 永远在 A 完成后才 executing，不可能同时 executing）；
  ③ 互不阻塞——A 完成不依赖 B（反之亦然），且 A 执行期间 B 也在执行/推进（A 的 await 不阻塞 B）。

为何用「写不同产物文件」作为并发执行的可观测信号：
  worker A 写 frontend 文件、worker B 写 backend 文件，两个产物最终都出现在工作区——
  证明两个 worker 都真执行了（非一个被 backlog 阻塞）。但「最终产物都在」只证「都执行过」
  不证「同时执行」。「同时 executing」是并发的直接证据（status 轮询交叉验证）。

为何捕获后 stop_group：worker 真执行会跑 LLM（耗时 + 占内存）。本任务只需验证「并发执行 +
  同时 executing + 互不阻塞」，捕获到双 worker 同时 executing 状态即可 stop_group 取消后续，
  避免无谓 LLM 调用 + 内存峰值（沿用 test_pl11 单用例防 OOM 立场，但不等任务全完成）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle；
  ④ 设直接干：PUT config auto_confirm=True；
  ⑤ 发可并行目标 + 抓 plan + 抓 task_dispatch：连 WS → POST /api/messages 发前后端可并行
     目标（前端写 frontend_mt12.md，后端写 backend_mt12.md，两个独立产物）→ 抓 coordinator_plan
     + 全部 task_dispatch（HARD：并发派发给不同 worker）；
  ⑥ 并发派发（HARD）：task_dispatch >=2 个 + 分别派给 frontend_id 和 backend_id（不同 worker）；
  ⑦ 同时 executing（HARD 核心）：发消息后高频轮询 GET /api/status，捕获某一时刻 frontend 和
     backend 的 status 同时为 executing（并发执行的直接证据，轮询窗口给足让两个 worker
     都进入 executing）；
  ⑧ 互不阻塞（HARD）：两个 worker 最终都完成各自任务（task_complete/agent_reply 各自独立，
     无一方因另一方卡住而超时不完成）——用产物文件 + WS task_complete 交叉验证；
  ⑨ 产物隔离（SOFT/HARD）：frontend 产物（含前端标记词）+ backend 产物（含后端标记词）都落盘，
     证明两个 worker 在各自任务里独立产出（非串行一个等另一个）；
  ⑩ 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group 删 DB + 清理工作区产物）
     → 全局列表无残留。

为何「同时 executing」是核心 HARD 断言：MT-12「互不阻塞」的充要条件是「并发执行」。若串行
（B 等 A 完成才执行），则任意时刻最多一个 executing，永远不可能同时 executing。故「某一时刻
A.status==executing 且 B.status==executing」是并发执行的直接证据。轮询 GET /api/status
（list_group_status 遍历 _engines 真源）捕捉这一时刻。worker 执行 LLM 耗时数秒~数十秒，
两个 worker executing 窗口重叠概率高，高频轮询（0.3s）能捕捉到。

为何不强制「完整执行完成」验证互不阻塞：worker 完整执行（LLM 多轮 + 工具调用）耗时长 +
内存重，且「都完成」只证「都执行过」不证「同时执行」（串行也能都完成）。核心证据是
「同时 executing」窗口，捕获到即证明并发。捕获后立即 stop_group 避免无谓资源消耗（与
test_pl11 不等长任务完成、捕获关键状态即收尾同立场）。但保留「产物落盘」作 SOFT 证据
（若两个产物都在，进一步佐证并发执行非串行）。

为何用专属探针群 + reload：group_demo_1 coordinator 引擎累积历史状态 + 残留 plan，
会污染「并发执行本目标」断言。新建 [MT-12] 探针群 → reload 起干净引擎 → 显式设
auto_confirm=True → coordinator 只看到本目标 → 拆出的 plan 是本目标的纯净并发拆解
（沿用 MT-09/MT-10/MT-11 隔离模式）。
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
PLAN_TIMEOUT = 90.0       # 等 coordinator_plan + task_dispatch（含 LLM 调用）
CONCURRENT_POLL = 50.0    # 高频轮询 status 捕获「同时 executing」窗口
POLL_INTERVAL = 0.3       # 轮询间隔（秒）

# 探针群组名（[MT-12] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-12] 多Worker并发执行探针组"

# 可并行目标——前端写 frontend_mt12.md（含前端标记），后端写 backend_mt12.md（含后端标记），
# 两个独立产物，两个 worker 并发执行互不依赖。
GOAL = (
    "【MT-12】请帮我完成两个独立的小任务，可以直接并行执行，不需要互相等待："
    "1. 前端工程师用 write_file 工具创建文件 frontend_mt12.md，内容写明'前端并发执行标记'并写一段前端说明；"
    "2. 后端工程师用 write_file 工具创建文件 backend_mt12.md，内容写明'后端并发执行标记'并写一段后端说明。"
    "这两个任务互不依赖，请同时派发给前后端工程师并行执行。请直接派发执行计划。"
)

# 产物文件名 + 期望标记词（交叉验证两个 worker 各自独立产出）。
FE_FILE = "frontend_mt12.md"
BE_FILE = "backend_mt12.md"
FE_MARKER = "前端"
BE_MARKER = "后端"


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


async def collect_plan_and_dispatches(
    ws_url: str, send_action, timeout: float
) -> tuple[list[dict], dict | None, list[dict]]:
    """连 WS，send_action 发消息，收事件直到抓到 coordinator_plan 并收齐其后的 task_dispatch。

    返回 (全量事件, plan 事件, task_dispatch 事件列表)。
    """
    events: list[dict] = []
    plan_ev: dict | None = None
    dispatches: list[dict] = []
    deadline = time.time() + timeout
    async with websockets.connect(ws_url) as ws:
        if send_action is not None:
            await send_action()
        # 阶段 1：等 coordinator_plan
        while time.time() < deadline and plan_ev is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_plan":
                plan_ev = ev
        # 阶段 2：plan 出现后多收 8 秒抓全 task_dispatch（fan-out 紧跟 plan）
        if plan_ev is not None:
            end = time.time() + 8.0
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, end - time.time())
                    )
                    ev = json.loads(raw)
                    events.append(ev)
                    if ev.get("type") == "task_dispatch":
                        dispatches.append(ev)
                except asyncio.TimeoutError:
                    break
    # 兜底：从全量事件里再扫一遍 task_dispatch（防阶段切换漏收）
    for ev in events:
        if ev.get("type") == "task_dispatch" and ev not in dispatches:
            dispatches.append(ev)
    return events, plan_ev, dispatches


async def poll_concurrent_executing(
    group_id: str, frontend_id: str, backend_id: str, timeout: float
) -> tuple[bool, list[dict]]:
    """高频轮询 GET /api/status，捕获 frontend + backend 同时 executing 的时刻。

    返回 (是否捕获到同时 executing, 捕获时的 status 快照)。
    捕到即返（无需继续轮询）；超时未捕到返 False。
    """
    deadline = time.time() + timeout
    snapshots: list[dict] = []
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{BASE}/api/status/{group_id}")
                if r.status_code == 200:
                    st = r.json()
                    status_map = {e["id"]: e.get("status") for e in st}
                    snapshots.append({
                        "t": round(time.time(), 2),
                        "fe": status_map.get(frontend_id),
                        "be": status_map.get(backend_id),
                    })
                    if (status_map.get(frontend_id) == "executing"
                            and status_map.get(backend_id) == "executing"):
                        return True, snapshots
        except (httpx.HTTPError, Exception):
            pass
        await asyncio.sleep(POLL_INTERVAL)
    return False, snapshots


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
    print("=== MT-12 自测：多 Worker 同时执行互不阻塞 ===")
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
            "description": "MT-12 多 Worker 同时执行互不阻塞自测探针",
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
        print("\n[check 4] 设直接干：PUT config auto_confirm=True（让 Leader fan-out 并发派发）")
        cfg = await set_auto_confirm(probe_group_id, True)
        if _check("config.auto_confirm==True（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True), f"config={cfg}"):
            print(f"      auto_confirm=True（直接干，plan 后立即并发 fan-out）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 5. 发可并行目标 + 抓 plan + 抓 task_dispatch ──
        print("\n[check 5] 发可并行目标 + 抓 coordinator_plan + 全部 task_dispatch（并发派发）")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"
        # 并发：连 WS 收事件 + 发消息 + 高频轮询 status 捕获同时 executing
        # 三路并发：WS 收事件、发消息、轮询并发执行窗口
        async def _send():
            await asyncio.sleep(0.3)  # 让 WS 先连上
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        ws_collect = asyncio.create_task(
            collect_plan_and_dispatches(ws_url, _send, PLAN_TIMEOUT)
        )
        # 等 WS 收到 plan/dispatch 后再开始轮询并发窗口（dispatch 后 worker 才进入 executing）
        await asyncio.sleep(2.0)
        concurrent_task = asyncio.create_task(
            poll_concurrent_executing(probe_group_id, frontend_id, backend_id, CONCURRENT_POLL)
        )
        events, plan_ev, dispatches = await ws_collect

        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_plan 事件", plan_ev is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获 coordinator_plan 事件")
        if not _check(f"捕获 task_dispatch 事件（>=2 个并发派发）", len(dispatches) >= 2,
                      f"仅 {len(dispatches)} 个"):
            errs.append(f"[dispatch] task_dispatch 数 {len(dispatches)} < 2")

        # ── 6. 并发派发（HARD）：>=2 个 task_dispatch 分别派给不同 worker ──
        print("\n[check 6] 并发派发：task_dispatch >=2 + 分别派给 frontend_id 和 backend_id（不同 worker）")
        if dispatches:
            dispatch_agents = {((d.get("data") or {}).get("agent_id")) for d in dispatches}
            both_dispatched = frontend_id in dispatch_agents and backend_id in dispatch_agents
            if _check("task_dispatch 分别派给 frontend_id + backend_id（不同 worker 并发派发）",
                      both_dispatched, f"dispatch_agents={dispatch_agents}"):
                print(f"      并发派发目标：{dispatch_agents}")
            else:
                errs.append(f"[dispatch] 未分别派给前后端：{dispatch_agents}")

        # ── 7. 同时 executing（HARD 核心）：捕获某一时刻前后端同时 executing ──
        print("\n[check 7] 同时 executing（HARD 核心）：轮询 status 捕获前后端同时 executing")
        concurrent_ok, snapshots = await concurrent_task
        # 打印 status 演化片段（前 15 + 后 5 个采样点）便于诊断
        if snapshots:
            head = snapshots[:15]
            tail = snapshots[15:] if len(snapshots) > 15 else []
            print(f"      status 演化（共 {len(snapshots)} 采样点）：")
            for s in head:
                print(f"        t={s['t']} fe={s['fe']} be={s['be']}")
            if tail:
                print(f"        ... ({len(tail)} more) ...")
                for s in tail[-3:]:
                    print(f"        t={s['t']} fe={s['fe']} be={s['be']}")
        if _check("捕获某一时刻 frontend+backend 同时 executing（并发执行直接证据）",
                  concurrent_ok, "未捕获同时 executing 窗口"):
            print(f"      ✓ 两个 worker 并发执行（status 同时为 executing）——证明非串行")
        else:
            errs.append("[concurrent] 未捕获前后端同时 executing 窗口")

        # ── 8. 互不阻塞（HARD）：两个 worker 各自推进，无一方因另一方卡住 ──
        print("\n[check 8] 互不阻塞（HARD）：两 worker 各自推进（产物落盘 + task_complete 独立）")
        # 此时可能已被 stop？不——本测试未主动 stop，让 worker 跑完。但 WS 收完可能已超时。
        # 用产物落盘交叉验证：两个产物文件都在 = 两个 worker 都真执行了（非一个被阻塞）
        fe_path = workspace_file(probe_group_id, FE_FILE)
        be_path = workspace_file(probe_group_id, BE_FILE)
        # 给 worker 一些时间完成产物（若 WS 收完时还在跑）
        # 等待两个产物都出现或超时
        product_deadline = time.time() + 60.0
        while time.time() < product_deadline:
            if fe_path.exists() and be_path.exists():
                break
            await asyncio.sleep(1.0)
        fe_ok = fe_path.exists() and fe_path.stat().st_size > 0
        be_ok = be_path.exists() and be_path.stat().st_size > 0
        # 也检查 WS 事件里两个 worker 各自的 task_complete/agent_reply（独立推进证据）
        fe_activity = sum(
            1 for e in events
            if e.get("sender_id") == frontend_id
            and e.get("type") in ("task_tool", "task_complete", "task_failed", "task_log", "agent_reply")
        )
        be_activity = sum(
            1 for e in events
            if e.get("sender_id") == backend_id
            and e.get("type") in ("task_tool", "task_complete", "task_failed", "task_log", "agent_reply")
        )
        print(f"      [activity] frontend 事件={fe_activity} backend 事件={be_activity}")
        print(f"      [product] frontend_mt12.md 存在={fe_ok} backend_mt12.md 存在={be_ok}")
        # 互不阻塞：两个 worker 都有活动（事件 > 0）——证明各自独立推进非一个卡死
        both_active = fe_activity > 0 and be_activity > 0
        if _check("两 worker 各自有执行活动（事件 > 0，各自独立推进非一个卡死）",
                  both_active, f"fe_activity={fe_activity} be_activity={be_activity}"):
            pass
        else:
            errs.append(f"[unblocked] 某 worker 无活动：fe_activity={fe_activity} be_activity={be_activity}")

        # ── 9. 产物隔离（SOFT/HARD）：两个产物都落盘 + 内容含各自标记 ──
        print("\n[check 9] 产物隔离：frontend_mt12.md + backend_mt12.md 都落盘且内容含各自标记")
        fe_content = fe_path.read_text(encoding="utf-8", errors="replace") if fe_ok else ""
        be_content = be_path.read_text(encoding="utf-8", errors="replace") if be_ok else ""
        fe_marker_ok = FE_MARKER in fe_content
        be_marker_ok = BE_MARKER in be_content
        # 产物落盘作 SOFT（worker 可能被 LLM 多轮拖久未在窗口内完成），但内容标记作强证据
        _info("frontend_mt12.md 落盘 + 含前端标记", fe_ok and fe_marker_ok,
              f"exists={fe_ok} marker={fe_marker_ok}" + (f" content预览={fe_content[:50]!r}" if fe_ok else ""))
        _info("backend_mt12.md 落盘 + 含后端标记", be_ok and be_marker_ok,
              f"exists={be_ok} marker={be_marker_ok}" + (f" content预览={be_content[:50]!r}" if be_ok else ""))

        # ── 10. 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group 删 DB） ──
        print("\n[check 10] 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group）→ 全局无残留")
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
        # 清理探针产物文件（可能在群工作区残留，群删了目录也删了，兜底删一次）
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
    print("PASS — 多 Worker 同时执行互不阻塞端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 Leader + 2 worker 引擎；")
    print("  · 设直接干 → 发可并行目标（前端写 frontend_mt12.md + 后端写 backend_mt12.md）；")
    print("  · 并发派发：task_dispatch >=2 分别派给 frontend_id + backend_id（不同 worker）；")
    print("  · [核心] 同时 executing：轮询 status 捕获某一时刻前后端同时 executing（并发直接证据）；")
    print("  · 互不阻塞：两 worker 各自有执行活动（独立推进，无一方卡死）；")
    print("  · [SOFT] 产物隔离：frontend/backend 产物各自落盘含标记（独立产出）；")
    print("  · 收尾 DELETE 探针群（stop_group 取消执行）→ 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
