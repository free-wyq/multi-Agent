"""MT-16 自测：所有子任务完成后汇总整合输出（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-09~MT-15 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动 + 直接干模式 fan-out + 真执行 report-back）。

MT-16 链路（所有子任务完成 → all_done → node_summarize 整合各 step.result → 终态汇总输出 → 清空计划）：
  前端 GroupPage：用户发可并行多步目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出 >=2 步 plan（前后端各一步，deps==[] 可并行）
    · node_dispatch → direct_run → node_dispatch_next
    · dispatch_ready_steps → _dispatch_one fan-out：step.status=dispatched + step.task_id
      + push_task 到对口 worker 引擎 + emit_task_dispatched
  每个 worker 执行 _run_worker_task（create_react_agent + 工具）→ emit_task_completed（task_complete）
    → complete_task → push_notify(agent_reply, worker→coordinator,
      f"步骤完成：{task_content}\\n\\n结果：{snippet}", {"task_id","success"})
  coordinator 引擎 _handle_notify（处理 worker report-back）→ LangGraph ainvoke：
    · classify → handle_reply（data.task_id 匹配 dispatched step）
    · node_handle_reply：step.status=completed + step.result=notify.content（worker 汇报内容）
      all_done？否 → dispatch_next（还有 pending 或在途 step）
    · 最后一例 worker report-back → node_handle_reply 标记完成 → all_done=True
      → return summarize → node_summarize（本任务核心）：
        summary = "\\n".join(f"✅ {agent_name}: {result[:200]}" for step in plan)
        ——node_summarize 从「跟踪的各 step.result」整合构建「🎉 全部完成！协作结果汇总」reply，
          每步的 agent_name + result 都进入汇总（一个不漏），这是「整合输出」的核心：
          最终交付 = 所有子任务产出 的聚合。
        → _unified_reply 落库（type=agent_reply, sender=coordinator）+ emit_message_added
        → return {"dispatch_plan": []}（清空计划，终态）

「所有子任务完成后汇总整合输出」的三层证据：
  ① 所有子任务完成——每个派发的 step 都 task_complete（success=True），无 task_failed、无遗漏
     report-back（all_done 的前置条件：plan 全 completed）；
  ② 汇总整合各 step.result——node_summarize 遍历 plan，把每步的 agent_name + result 聚合成一条
     汇总 reply（每步 ✅ agent_name: result[:200]）。汇总 reply 含「每个」step 的 agent_name +
     result 内容，证明 Leader 整合了所有子任务的产出（非空泛「完成」，而是每子任务输出的聚合）；
  ③ 终态——汇总后 _dispatch_plan 清空（return {"dispatch_plan": []} → registry 同步 engine._dispatch_plan=[]）
     GET /api/groups/{id}/plan 返回空 plan，证明整合完成、计划终态（最终交付已产出，计划复位）。

为何 MT-16 与 MT-13 区分：MT-13 验证「Leader 实时跟踪进度」（report-back 通道 + 按步 status 更新 +
汇总反映跟踪），聚焦「跟踪机制运转」。MT-16 验证「所有子任务完成后的汇总整合输出」——聚焦「整合
完整性 + 终态」：①所有子任务都完成（全成功，无失败/降级，纯成功整合路径）；②汇总 reply 整合了
「每一个」子任务的产出（agent_name 集合 == plan 步骤 agent_name 集合，一个不漏=整合完整）；
③汇总后计划清空（终态证据，MT-13 未验证）。MT-16 是 MT-13「跟踪」之后的「整合交付」闭环验证。

为何用专属探针群 + reload + 直接干：group_demo_1 coordinator 引擎累积历史 _memory + 残留 _dispatch_plan
+ auto_confirm 状态，会污染「整合本目标输出」断言。新建 [MT-16] 探针群 → reload 起干净引擎 → 显式设
auto_confirm=True → coordinator 只看到本目标 → 拆出本目标的 2 步 plan → 2 worker 真执行 report-back
→ all_done → 干净整合本目标的 2 worker 产出（沿用 MT-09~MT-15 隔离模式）。直接干让 plan 后立即 fan-out
+ worker 真执行 + report-back 触发 handle_reply → all_done → summarize 整合链路。

为何让两 worker 写不同小产物文件 + 各自标记：①让两 worker 都有真实执行 + 产出（report-back 有内容，
整合有 result 可引用）；②两产物文件名不同 + 标记不同便于溯源清理 + 整合完整性校验（汇总含两标记=两产出
都整合入）；③产物小（bounded）避免 OOM（沿用 test_pl11 单用例防 OOM 立场）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle（Leader + 2 worker 驻留）；
  ④ 设直接干：PUT config auto_confirm=True（让 Leader fan-out + worker 真执行 + report-back → all_done → summarize）；
  ⑤ 发可并行目标 + 抓事件流：连 WS → POST /api/messages 发前后端可并行目标（前端写 mt16_frontend.md +
     后端写 mt16_backend.md，两独立小产物）→ 抓 coordinator_plan + task_dispatch + task_complete +
     Leader 汇总 agent_reply（collect_until_summary 收到「全部完成」即收尾）；
  ⑥ plan 多步派两 worker（HARD）：plan >=2 步 + >=1 步派 frontend_id + >=1 步派 backend_id
     （2 worker 都被纳入整合计划）；
  ⑦ 所有子任务完成（HARD 核心）：每个派发 step 都 task_complete（success=True）+ 无 task_failed +
     两 worker 都 task_complete（全成功，all_done 的前置——所有子任务完成才触发整合汇总）；
  ⑧ report-back 全覆盖（HARD 确定性）：每 task_dispatch.task_id 都有对应 task_complete/task_failed
     （所有派发任务都执行完并 report-back，无一遗漏——整合的输入完整）；
  ⑨ 汇总整合所有子任务产出（HARD 核心）：汇总 reply 抓到 + DB 落库交叉 + 汇总 reply 含「每个」plan
     step 的 agent_name（set(plan 步骤 agent_name) ⊆ 汇总 reply 中出现的 agent_name——一个不漏，
     证明 node_summarize 整合了所有子任务的产出，非部分聚合）；
  ⑩ 汇总整合各子任务结果内容（SOFT）：汇总 reply 引用两 worker 各自的产物标记/文件名
     （mt16_frontend/mt16_backend 或前端/后端标记），证明整合的是「实际产出内容」非空泛模板；
  ⑪ 终态：计划清空（HARD）：汇总后 GET /api/groups/{id}/plan 返回空 plan（node_summarize return
     dispatch_plan=[] → registry 同步 engine._dispatch_plan=[]），证明整合完成、计划终态复位；
  ⑫ 收尾：DELETE 探针群（stop_group + delete_group + 清理工作区产物）→ 全局无残留。

为何 HARD 核心是「整合完整性（每个 step 的 agent_name 都入汇总）」+「终态（计划清空）」：
node_summarize 是「所有子任务完成后」的整合节点——它遍历 plan，把每步的 agent_name + result 聚合成
一条汇总 reply。故「汇总 reply 含每个 plan step 的 agent_name」是「整合了所有子任务产出」的确定性证据
（一个不漏=整合完整；漏一个则非全整合）。再加「汇总后计划清空」终态证据（GET /plan 空），双证「所有
子任务完成 → 整合输出 → 终态」闭环。result 内容标记作 SOFT（LLM 改写 instruction 可能不逐字含标记），
HARD 用稳定的 agent_name（来自 roster，LLM 设 step.agent_name）+ 计划清空（确定性状态机）。

为何「report-back 全覆盖」（⑧）是确定性 HARD：task_dispatch 携带 pushed task_id，task_complete 携带
同一 task_id。每 task_dispatch.task_id 都有对应 task_complete=每个派发任务都执行完 report-back（整合的
输入完整，无一丢失）。这是「所有子任务完成」的 per-task 确定性证据（非聚合级「汇总」）。
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
SUMMARY_TIMEOUT = 150.0  # 等 all_done → summarize 整合 reply（2 worker 真执行 + report-back + 整合汇总）

# 探针群组名（[MT-16] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-16] 所有子任务完成汇总整合输出探针组"

# 可并行目标——前端写 mt16_frontend.md（含前端方案标记），后端写 mt16_backend.md（含后端方案标记），
# 两个独立小产物，两 worker 并发执行互不依赖，各自执行完向 Leader report-back → all_done → 整合汇总。
GOAL = (
    "【MT-16】请帮我完成一份项目技术方案，需要两位工程师并行各写一部分："
    "1. 前端工程师用 write_file 工具创建文件 mt16_frontend.md，内容第一行写'MT16前端方案：采用 React + TypeScript'，再写一段简短前端架构说明；"
    "2. 后端工程师用 write_file 工具创建文件 mt16_backend.md，内容第一行写'MT16后端方案：采用 Python FastAPI'，再写一段简短后端架构说明。"
    "这两个任务互不依赖，请同时并行派发执行。请直接派发执行计划。"
)

# 产物文件名 + 期望标记词（交叉验证两 worker 各自独立产出 + 整合引用）。
FE_FILE = "mt16_frontend.md"
BE_FILE = "mt16_backend.md"
FE_MARKER = "MT16前端"
BE_MARKER = "MT16后端"

# 目标关键词（软断言用——汇总 reply 引用其一即「整合基于本目标」）。
GOAL_KEYWORDS = ["前端", "后端", "方案", "文件", "React", "FastAPI", "mt16", "MT16", "架构"]


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
    async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
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
    print("=== MT-16 自测：所有子任务完成后汇总整合输出 ===")
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
            "description": "MT-16 所有子任务完成后汇总整合输出自测探针",
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
        print("\n[check 4] 设直接干：PUT config auto_confirm=True（让 Leader fan-out + worker 真执行 + report-back → all_done → 整合汇总）")
        cfg = await set_auto_confirm(probe_group_id, True)
        if _check("config.auto_confirm==True（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True), f"config={cfg}"):
            print(f"      auto_confirm=True（直接干，plan 后立即 fan-out + worker 真执行 report-back → all_done → summarize 整合）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 5. 发可并行目标 + 抓 plan + task_dispatch + task_complete + 汇总 reply ──
        print("\n[check 5] 发可并行目标 + 抓 coordinator_plan + task_dispatch + task_complete + Leader 整合汇总 reply")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"
        async def _send():
            await asyncio.sleep(0.3)  # 让 WS 先连上
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, plan_ev, summary_ev, dispatches, completes = await collect_until_summary(
            ws_url, _send, SUMMARY_TIMEOUT, coord_id
        )

        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_plan 事件", plan_ev is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获 coordinator_plan 事件")
        if not _check("捕获 Leader 整合汇总 reply（node_summarize all_done 整合输出）",
                      summary_ev is not None, "未捕获汇总 reply（all_done 未触发 / summarize 未跑）"):
            errs.append("[summary] 未捕获 Leader 整合汇总 reply")

        # ── 6. plan 多步派两 worker（HARD） ──
        print("\n[check 6] plan 多步派两 worker：>=2 步 + >=1 派 frontend + >=1 派 backend（2 worker 被纳入整合计划）")
        plan: list[dict] = []
        if plan_ev is not None:
            plan = (plan_ev.get("data") or {}).get("plan") or []
        plan_agent_ids = {s.get("agent_id") for s in plan if isinstance(s, dict)}
        if _check("plan >=2 步", isinstance(plan, list) and len(plan) >= 2, f"plan 步数={len(plan)}"):
            pass
        else:
            errs.append(f"[plan] plan 仅 {len(plan) if isinstance(plan, list) else 'NA'} 步，期望 >=2")
        if _check("plan >=1 步派 frontend_id + >=1 步派 backend_id（2 worker 被纳入整合计划）",
                  frontend_id in plan_agent_ids and backend_id in plan_agent_ids,
                  f"plan_agent_ids={plan_agent_ids}"):
            print(f"      plan 派工：{[(s.get('step'), s.get('agent_name')) for s in plan if isinstance(s, dict)]}")
        else:
            errs.append(f"[plan] plan 未覆盖前后端两 worker：{plan_agent_ids}")

        # ── 7. 所有子任务完成（HARD 核心）：每派发 step 都 task_complete + 无 task_failed + 两 worker 都完成 ──
        print("\n[check 7] 所有子任务完成（HARD 核心）：每派发 step 都 task_complete + 无 task_failed + 两 worker 都完成")
        complete_evs = [c for c in completes if c.get("type") == "task_complete"]
        failed_evs = [c for c in completes if c.get("type") == "task_failed"]
        fe_complete = [c for c in complete_evs if c.get("sender_id") == frontend_id]
        be_complete = [c for c in complete_evs if c.get("sender_id") == backend_id]
        if _check("无 task_failed（全成功路径，无失败/降级，纯整合）",
                  len(failed_evs) == 0, f"task_failed={len(failed_evs)}"):
            pass
        else:
            errs.append(f"[complete] 出现 task_failed={len(failed_evs)}（非全成功整合路径）")
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
        if _check("所有派发 step 都 task_complete（所有子任务完成=all_done 前置）",
                  len(complete_evs) >= len(dispatches) and len(fe_complete) >= 1 and len(be_complete) >= 1,
                  f"dispatches={len(dispatches)} complete={len(complete_evs)} fe={len(fe_complete)} be={len(be_complete)}"):
            print(f"      [完成] 派发 {len(dispatches)} 任务全部 task_complete（fe={len(fe_complete)} be={len(be_complete)}）"
                  f"——所有子任务完成触发 all_done → 整合汇总")

        # ── 8. report-back 全覆盖（HARD 确定性）：每 task_dispatch.task_id 有对应 task_complete/task_failed ──
        print("\n[check 8] report-back 全覆盖（确定性）：每 task_dispatch.task_id 有对应 task_complete/task_failed（整合输入完整）")
        dispatch_tids = {d.get("task_id") for d in dispatches if d.get("task_id")}
        report_tids = {c.get("task_id") for c in completes if c.get("task_id")}
        missing = dispatch_tids - report_tids
        if _check("每个派发任务都 report-back（task_dispatch.task_id ⊆ {task_complete,task_failed}.task_id）",
                  len(missing) == 0, f"缺失 report-back 的 task_id={missing}"):
            print(f"      [report-back] 派发 {len(dispatch_tids)} 任务，全部 report-back 到 Leader（整合输入完整无一遗漏）")
        else:
            errs.append(f"[report-back] {len(missing)} 个派发任务未 report-back：{missing}")

        # ── 9. 汇总整合所有子任务产出（HARD 核心）：汇总 reply 含「每个」plan step 的 agent_name ──
        print("\n[check 9] 汇总整合所有子任务产出（HARD 核心）：汇总 reply 含「每个」plan step 的 agent_name（一个不漏=整合完整）")
        summary_content = (summary_ev or {}).get("content") or ""
        print(f"      [summary] content 预览：{summary_content[:200]}…")
        # 收集 plan 中每个 step 的 agent_name（node_summarize 从各 step.result 聚合，含 agent_name）
        plan_step_names = {
            s.get("agent_name") for s in plan
            if isinstance(s, dict) and s.get("agent_name")
        }
        # 汇总 reply 中出现的 plan agent_name（整合完整性：每个 step 的 agent_name 都入汇总）
        names_in_summary = {n for n in plan_step_names if n and n in summary_content}
        if _check("汇总 reply 含「每个」plan step 的 agent_name（整合所有子任务产出，一个不漏）",
                  len(plan_step_names) > 0 and names_in_summary == plan_step_names,
                  f"plan步骤agent_name={plan_step_names} 入汇总={names_in_summary}"
                  + (f" 缺漏={plan_step_names - names_in_summary}" if names_in_summary != plan_step_names else "")):
            print(f"      [整合] plan {len(plan_step_names)} 个 step 的 agent_name 全部入汇总 reply"
                  f"（node_summarize 整合了所有子任务产出，无一漏聚合）")
        else:
            errs.append(f"[summary] 汇总未整合所有 step agent_name：plan={plan_step_names} 入汇总={names_in_summary}")
        # DB 交叉确认：汇总 reply 落库（type=agent_reply, sender=coordinator, content 含「全部完成/汇总」）
        msgs = await list_messages(probe_group_id, limit=100)
        db_summary = next(
            (m for m in msgs
             if m.get("type") == "agent_reply"
             and m.get("sender_id") == coord_id
             and ("全部完成" in (m.get("content") or "") or "汇总" in (m.get("content") or ""))),
            None,
        )
        if _check("整合汇总 reply 落库（GET /api/messages 交叉确认，WS 事件 vs DB 双真源）",
                  db_summary is not None, "DB 未找到整合汇总 reply"):
            print(f"      [db] 整合汇总 message id={(db_summary or {}).get('id', '')[:16]}… 落库确认")
        else:
            errs.append("[summary] 整合汇总 reply 未落库（DB 无记录）")

        # ── 10. 汇总整合各子任务结果内容（SOFT）：汇总引用两 worker 产物标记/文件名 ──
        print("\n[check 10] 汇总整合各子任务结果内容（SOFT）：汇总 reply 引用两 worker 产物标记/文件名")
        fe_content_in = FE_MARKER in summary_content or FE_FILE in summary_content
        be_content_in = BE_MARKER in summary_content or BE_FILE in summary_content
        _info("汇总 reply 引用 frontend 产物标记/文件名（整合的是前端实际产出）",
              fe_content_in,
              f"含{FE_MARKER}/{FE_FILE}={'是' if fe_content_in else '否'}")
        _info("汇总 reply 引用 backend 产物标记/文件名（整合的是后端实际产出）",
              be_content_in,
              f"含{BE_MARKER}/{BE_FILE}={'是' if be_content_in else '否'}")
        hit_kw = next((kw for kw in GOAL_KEYWORDS if kw in summary_content), None)
        _info("汇总 reply 引用目标关键词（整合基于本目标，非空泛模板）",
              hit_kw is not None, f"命中={hit_kw}" if hit_kw else f"summary 预览={summary_content[:80]}")

        # ── 11. 终态：计划清空（HARD）：汇总后 GET /plan 返回空 plan ──
        print("\n[check 11] 终态：计划清空（HARD）：汇总后 GET /api/groups/{id}/plan 返回空 plan（node_summarize 清空 _dispatch_plan）")
        # 等 ainvoke 完成 + registry 同步 _dispatch_plan=[]（summary_ev 已是 summarize reply，稍候确保同步）
        await asyncio.sleep(1.0)
        plan_after = await get_plan(probe_group_id)
        plan_after_list = (plan_after or {}).get("plan") or []
        if _check("汇总后 _dispatch_plan 已清空（GET /plan 返回空 plan，整合完成终态复位）",
                  isinstance(plan_after_list, list) and len(plan_after_list) == 0,
                  f"plan_after 步数={len(plan_after_list) if isinstance(plan_after_list, list) else 'NA'}"):
            print(f"      [终态] node_summarize return dispatch_plan=[] → registry 同步 engine._dispatch_plan=[]"
                  f"（整合完成，计划复位，最终交付已产出）")
        else:
            errs.append(f"[terminal] 汇总后计划未清空：plan_after={len(plan_after_list) if isinstance(plan_after_list, list) else 'NA'} 步")

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
    print("PASS — 所有子任务完成后汇总整合输出端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 Leader + 2 worker 引擎；")
    print("  · 设直接干 → 发可并行目标（前端写 mt16_frontend.md + 后端写 mt16_backend.md）；")
    print("  · [核心] 所有子任务完成：两 worker 都 task_complete + 无 task_failed（全成功，all_done 前置）；")
    print("  · report-back 全覆盖（每派发任务都汇报回 Leader，整合输入完整无一遗漏）；")
    print("  · [核心] 汇总整合所有子任务产出：node_summarize 把每步 agent_name+result 聚合成一条汇总 reply")
    print("    （plan 每个 step 的 agent_name 都入汇总=整合完整，一个不漏）+ DB 落库交叉确认；")
    print("  · [SOFT] 汇总整合各子任务结果内容（引用两 worker 产物标记/文件名）；")
    print("  · [核心] 终态：汇总后 _dispatch_plan 清空（GET /plan 返回空 plan，整合完成复位）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
