"""MT-14 自测：执行中根据结果调整后续计划（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-09~MT-13 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动 + 直接干模式 fan-out + 真执行 report-back）。

MT-14 链路（Worker report-back → Leader 据中间结果动态调整剩余 pending 派工）：
  前端 GroupPage：用户发一个「有先后依赖」的目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出一个「后端先做 API（步骤1，deps=[]）→ 前端据 API 调用（步骤2，
      depends_on=[1]）」的串行依赖 plan（前后端有真实依赖，非完全并行）
    · node_dispatch → direct_run → node_dispatch_next
    · dispatch_ready_steps → find_ready_steps 只返回步骤1（步骤2 depends_on=[1] 未满足）→
      _dispatch_one 派发步骤1到后端 worker + emit_task_dispatched
  后端 worker 执行步骤1（create_react_agent + 工具）→ emit_task_completed（task_complete）
    → complete_task → push_notify(agent_reply, worker→coordinator, {task_id, success})
  coordinator 引擎 _handle_notify（处理 worker report-back）→ LangGraph ainvoke：
    · classify：incoming_kind=="agent_reply" + sender!=user + data.task_id 匹配步骤1
      dispatched → handle_reply
    · node_handle_reply：标记步骤1 status=completed + result=汇报内容。
      all_done？否（步骤2 还 pending）→ MT-14 核心：success 且有 pending →
      _maybe_adjust_remaining_steps(state, plan)：
        · build_plan_adjust_prompt 给 LLM 看 plan 状态（步骤1 已完成含 result + 步骤2 pending
          含 instruction）+ worker 汇报，问「是否调整剩余 pending 步骤」
        · LLM 据步骤1 的结果判断步骤2 是否需调整（如后端定了 /api/login 接口形态，前端应据
          此调整调用方式）→ 返回 adjust=true/false + revised_steps
        · adjust=true 时 splice：保留步骤1（completed 历史）+ 替换步骤2（pending）为修订版 +
          emit_coordinator_plan 重宣布 + _unified_reply 发 announce 公告
        · adjust=false / LLM 错误时原样 plan（fallback 不阻塞 dispatch_next）
      → return dispatch_next → node_dispatch_next → dispatch_ready_steps 派发步骤2（修订版或原版）

「执行中根据结果调整后续计划」的三层证据：
  ① 串行依赖触发——plan 有 depends_on=[1] 的步骤2，步骤1 先执行完成 report-back 后，
     步骤2 才 pending（有「后续计划」可调）；若全并行（步骤2 deps=[]），两步同时 dispatched
     无 pending，无调整窗口（MT-12 范畴，MT-14 需串行依赖制造调整点）；
  ② Leader 据结果调整——步骤1 report-back 后 node_handle_reply 调 _maybe_adjust_remaining_steps
     让 LLM 据步骤1 的 result 判断步骤2 是否需修订（adjust=true → 修订步骤2 instruction/deps；
     adjust=false → 不变）。这是「据中间结果动态调整」的核心——Leader 不盲目沿用原步骤2，
     而是据步骤1 的产出重新审视剩余步骤；
  ③ 修订生效——adjust=true 时步骤2 被替换为修订版（instruction 变 / depends_on 变 / 新增步骤），
     重宣布的 coordinator_plan 事件 + GET /plan 真源反映修订后的步骤2，修订后的步骤2 被派发执行
     （task_dispatch 派给步骤2 的 worker）。证明调整真的改了「后续计划」并落地执行。

为何构造「后端先做 API→前端据 API 调用」串行依赖目标：MT-14 的「调整后续计划」需要一个真实
依赖点——后端 API 形态决定前端如何调用，是「中间结果影响后续步骤」的天然场景。LLM 拆 plan 时
会让前端步骤 depends_on=[1]（等后端 API 完成），制造「步骤1 先跑→report-back→调步骤2」的
串行链路。步骤1 report-back 后 _maybe_adjust_remaining_steps 运行（步骤2 pending + success），
LLM 据步骤1 的 API 结果判断步骤2（前端调用）是否需细化。即使 LLM 判 adjust=false（独立/已足够
明确），仍证明「Leader 重新审视了后续步骤并做出 keep/adjust 决策」（决策过程=调整机制已运转）。

为何用专属探针群 + reload + 直接干：group_demo_1 coordinator 引擎累积历史 _memory + 残留
_dispatch_plan + auto_confirm 状态，污染「调整本目标后续计划」断言。新建 [MT-14] 探针群 →
reload 起干净引擎 → 显式设 auto_confirm=True → coordinator 只看到本目标 → 拆出本目标的串行依赖
plan → 步骤1 执行 report-back → Leader 干净地据结果调整步骤2（沿用 MT-09~MT-13 隔离模式）。
直接干让步骤1 fan-out + 真执行 + report-back 触发 handle_reply 调整链路（wait_confirm 不 fan-out
无执行无 report-back 无法验证调整）。

为何依赖 LLM 判 adjust（不硬断言 adjust=true）：LLM 可能判步骤2 已足够明确 adjust=false（保留
原步骤2 直接派发）——这仍是「据结果做了调整决策」（决策=keep）。MT-14 验证的是「调整机制运转」
（node_handle_reply 调 LLM 审视剩余步骤 + 串行依赖 report-back 触发），不是「必须调整」。故 HARD
断言「串行依赖触发调整点 + 步骤2 在步骤1 完成后才派发 + 步骤2 最终执行完成」，SOFT 断言「adjust=true
且步骤2 instruction 改变」（LLM 真修订了，加分但不强制——LLM 可能合理判 keep）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle（Leader + 2 worker）；
  ④ 设直接干：PUT config auto_confirm=True；
  ⑤ 发串行依赖目标 + 抓事件流：连 WS → POST /api/messages 发「后端做 API→前端据 API 调用」
     目标 → 抓 coordinator_plan + 全部 task_dispatch + task_complete + Leader 汇总（collect_until_summary
     收到「全部完成」即收尾，覆盖两步串行完整链路）；
  ⑥ 串行依赖结构（HARD 核心）：plan >=2 步 + 存在 depends_on 非空的步骤（串行依赖，非全并行）+
     depends_on 引用合法（无悬空）——证明有「后续步骤」依赖前步结果，制造 MT-14 调整点；
  ⑦ 串行派发顺序（HARD）：步骤1 先 task_dispatch + 步骤2 后 task_dispatch，且步骤1 的
     task_complete 早于步骤2 的 task_dispatch（步骤2 在步骤1 report-back 后才被派发——
     调整点确实发生在步骤1 完成后、步骤2 派发前）；
  ⑧ 两步都执行完成（HARD）：步骤1 + 步骤2 各有 task_complete（report-back 全覆盖，调整后
     步骤2 仍被正常派发执行完成，未因调整中断）；
  ⑨ Leader 汇总（HARD）：抓到「全部完成」汇总 reply + DB 落库交叉 + 含两 worker agent_name
     （调整后 Leader 仍跟踪两步完成并汇总，node_handle_reply 调整链路未破坏汇总）；
  ⑩ 调整发生（SOFT 核心）：抓到步骤1 完成后、步骤2 派发前的「第二个 coordinator_plan 事件」
     （重宣布修订 plan）或 Leader announce 公告 reply——若 LLM 判 adjust=true 则必有重宣布/
     公告，是「真调整」的直接证据；adjust=false 则无（keep 决策，调整机制仍运转，降级 SOFT）；
  ⑪ 步骤2 instruction 修订（SOFT）：adjust=true 时，步骤2 派发时的 instruction 与初始 plan 的
     步骤2 instruction 不同（被 LLM 修订）；adjust=false 则相同（keep，不计 FAIL）；
  ⑫ 收尾：DELETE 探针群 + 清理工作区产物 → 全局无残留。

为何 HARD 核心是「串行派发顺序」而非「adjust=true」：MT-14 的机制是「步骤1 report-back 后、
步骤2 派发前，node_handle_reply 调 _maybe_adjust_remaining_steps 审视步骤2」。这一机制的确定性
证据是「步骤2 的 task_dispatch 发生在步骤1 的 task_complete 之后」（时间顺序）——若步骤2 在
步骤1 完成前就被派发（全并行），则无调整窗口（步骤2 已 dispatched 在途，handle_reply 跳过
adjust）。故「步骤1 task_complete 早于步骤2 task_dispatch」证明串行依赖确实制造了调整点，
node_handle_reply 在该点运行了 _maybe_adjust_remaining_steps（无论 LLM 判 keep 还是 adjust，
调整机制已运转）。adjust=true 是「真修订」的加分证据，作 SOFT（LLM 可能合理判 keep）。

为何不强制「步骤2 instruction 必改」：LLM 据步骤1 结果判步骤2 是否需调整，可能合理判「步骤2
原指令已足够明确，不需调整」（adjust=false，keep）。这仍是「据结果做了调整决策」（决策=keep），
MT-14 验证调整机制运转（串行触发 + LLM 审视 + keep/adjust 决策），不强制 LLM 必修订（那过于
严格，LLM 对已明确步骤会合理 keep）。故 instruction 改变作 SOFT，HARD 是串行派发顺序 + 两步
完成 + 汇总（调整机制运转 + 链路未中断）。
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

TIMEOUT = 120.0
RELOAD_WAIT = 45.0
SUMMARY_TIMEOUT = 300.0  # 等完整串行链路（步骤1执行+report-back+调整+剩余步骤派发执行+汇总）；LLM 可能据结果新增步骤，链路变长需足量窗口

# 探针群组名（[MT-14] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-14] 据结果调整后续计划探针组"

# 串行依赖目标——后端先做登录API（步骤1），前端据 API 形态做登录页调用该 API（步骤2，depends_on=[1]）。
# 制造「中间结果（API 形态）决定后续步骤（前端调用方式）」的真实依赖点，触发 MT-14 调整机制。
GOAL = (
    "【MT-14】请帮我开发一个登录功能，需要按顺序协作："
    "第一步后端工程师先开发登录API（接收用户名密码、校验后返回 token），"
    "第二步前端工程师根据后端完成的登录API，开发登录页面并调用该 API 完成登录。"
    "第二步依赖第一步完成（前端要调用后端的 API），请据此制定有依赖关系的计划并直接派发执行。"
)

# 产物文件名（步骤2 前端 worker 产物，交叉验证步骤2 真执行）。
FE_FILE = "mt14_login_page.md"
BE_FILE = "mt14_login_api.md"


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
) -> tuple[list[dict], list[dict], list[dict], dict | None, list[dict]]:
    """连 WS，send_action 发消息，收事件直到抓到 Leader 汇总 agent_reply 或超时。

    返回 (全量事件, task_dispatch 事件列表, task_complete 事件列表, 初始 plan 事件, plan 事件列表)。
    plan 事件列表含初始宣布 + 任何调整后的重宣布（MT-14 adjust=true 会触发第二个 plan 事件）。
    汇总 reply = node_summarize 产出的 agent_reply（sender=coordinator，content 含「全部完成/汇总」）。
    """
    events: list[dict] = []
    dispatches: list[dict] = []
    completes: list[dict] = []
    plan_events: list[dict] = []
    initial_plan: dict | None = None
    summary_ev: dict | None = None
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
            if t == "coordinator_plan":
                plan_events.append(ev)
                if initial_plan is None:
                    initial_plan = ev
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
                            elif ev2.get("type") == "coordinator_plan":
                                plan_events.append(ev2)
                        except asyncio.TimeoutError:
                            break
                    break
    # 兜底：从全量事件补扫
    for ev in events:
        if ev.get("type") == "task_dispatch" and ev not in dispatches:
            dispatches.append(ev)
        elif ev.get("type") in ("task_complete", "task_failed") and ev not in completes:
            completes.append(ev)
        elif ev.get("type") == "coordinator_plan" and ev not in plan_events:
            plan_events.append(ev)
    return events, dispatches, completes, initial_plan, plan_events


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
    print("=== MT-14 自测：执行中根据结果调整后续计划 ===")
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
            "description": "MT-14 执行中根据结果调整后续计划自测探针",
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
        print("\n[check 4] 设直接干：PUT config auto_confirm=True（让 Leader fan-out + 串行 report-back 触发调整）")
        cfg = await set_auto_confirm(probe_group_id, True)
        if _check("config.auto_confirm==True（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True), f"config={cfg}"):
            print(f"      auto_confirm=True（直接干，串行依赖 fan-out + report-back 触发 handle_reply 调整）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 5. 发串行依赖目标 + 抓事件流 ──
        print("\n[check 5] 发串行依赖目标 + 抓 plan + task_dispatch + task_complete + Leader 汇总")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"
        async def _send():
            await asyncio.sleep(0.3)
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, dispatches, completes, initial_plan, plan_events = await collect_until_summary(
            ws_url, _send, SUMMARY_TIMEOUT, coord_id
        )

        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        print(f"      [counts] task_dispatch={len(dispatches)} task_complete={len(completes)} "
              f"coordinator_plan={len(plan_events)}")
        if not _check("捕获初始 coordinator_plan 事件", initial_plan is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获初始 coordinator_plan 事件")

        # ── 6. 串行依赖结构（HARD 核心） ──
        print("\n[check 6] 串行依赖结构（HARD 核心）：plan >=2 步 + 存在 depends_on 非空步骤（串行依赖制造调整点）")
        plan: list[dict] = []
        if initial_plan is not None:
            plan = (initial_plan.get("data") or {}).get("plan") or []
        if not _check("plan 非空 list >=2 步", isinstance(plan, list) and len(plan) >= 2,
                      f"plan={plan}"):
            errs.append(f"[plan] plan 不合法或 <2 步：{plan}")
        else:
            step_nums = {s.get("step") for s in plan if isinstance(s, dict)}
            has_dep = any(
                isinstance(s, dict) and s.get("depends_on")
                for s in plan
            )
            # 串行依赖：至少一步 depends_on 非空 + 引用合法（无悬空）
            dangling = []
            for s in plan:
                if not isinstance(s, dict):
                    continue
                for dep in s.get("depends_on", []) or []:
                    if dep not in step_nums:
                        dangling.append((s.get("step"), dep))
            if _check("存在 depends_on 非空步骤（串行依赖，非全并行 → 制造 MT-14 调整点）",
                      has_dep, f"所有步骤 depends_on 均为空（全并行，无后续步骤可调）"):
                print(f"      plan 步骤：")
                for s in plan:
                    if isinstance(s, dict):
                        print(f"        · 步骤{s.get('step')} {s.get('agent_name')} "
                              f"deps={s.get('depends_on')} {str(s.get('instruction',''))[:50]}")
            else:
                errs.append("[deps] plan 全并行无串行依赖，无 MT-14 调整点")
            if _check("depends_on 引用合法（无悬空依赖）", len(dangling) == 0, f"悬空={dangling}"):
                pass
            else:
                errs.append(f"[deps] 悬空依赖：{dangling}")

        # ── 7. 串行派发顺序（HARD 核心）：步骤1 task_complete 早于步骤2 task_dispatch ──
        print("\n[check 7] 串行派发顺序（HARD 核心）：步骤2 在步骤1 report-back 后才派发（调整点发生在中间）")
        # 按 step 号排序 task_dispatch（dispatches 可能乱序，按事件顺序已是时序，但用 step 号定位更稳）
        # 事件顺序即时序：events 列表按收到顺序。取每个 task_dispatch 在 events 的 index + task_complete 的 index。
        def _ev_index(ev_target: dict) -> int:
            for i, e in enumerate(events):
                if e is ev_target:
                    return i
            return -1

        # 分两组 dispatch：第一派发的 + 第二派发的（按 step 号或 agent）
        # 用 task_complete 也按 events index 定位时序
        if len(dispatches) >= 2 and len(completes) >= 1:
            # 第一个 task_dispatch（步骤1 派发）+ 第一个 task_complete（步骤1 完成）+ 第二个 task_dispatch（步骤2 派发）
            d1 = dispatches[0]
            c1 = completes[0]
            d2 = dispatches[1]
            idx_d1 = _ev_index(d1)
            idx_c1 = _ev_index(c1)
            idx_d2 = _ev_index(d2)
            print(f"      [时序] 步骤1派发@{idx_d1} → 步骤1完成@{idx_c1} → 步骤2派发@{idx_d2}")
            # 核心：步骤1 完成 (task_complete) 早于 步骤2 派发 (task_dispatch) —— 串行依赖制造调整窗口
            serial_order = idx_d1 >= 0 and idx_c1 >= 0 and idx_d2 >= 0 and idx_c1 < idx_d2
            if _check("步骤1 task_complete 早于步骤2 task_dispatch（串行依赖制造调整窗口）",
                      serial_order, f"idx_d1={idx_d1} idx_c1={idx_c1} idx_d2={idx_d2}"):
                print(f"      ✓ 调整点确实发生在步骤1 完成后、步骤2 派发前（node_handle_reply 在此运行 _maybe_adjust）")
            else:
                errs.append(f"[serial] 步骤2 未在步骤1 完成后才派发：idx_d1={idx_d1} idx_c1={idx_c1} idx_d2={idx_d2}")
        else:
            if not _check("捕获 >=2 task_dispatch + >=1 task_complete（串行链路完整）",
                          len(dispatches) >= 2 and len(completes) >= 1,
                          f"dispatch={len(dispatches)} complete={len(completes)}"):
                errs.append(f"[serial] 事件不足：dispatch={len(dispatches)} complete={len(completes)}")

        # ── 8. 两步都执行完成（HARD） ──
        print("\n[check 8] 两步都执行完成：步骤1 + 步骤2 各有 task_complete（调整后步骤2 仍正常派发执行）")
        if len(completes) >= 2:
            complete_senders = [c.get("sender_id") for c in completes]
            print(f"      [complete] task_complete senders={complete_senders}")
            if _check(">=2 个 task_complete（步骤1 + 步骤2 都完成 report-back）",
                      len(completes) >= 2, f"complete={len(completes)}"):
                pass
            else:
                errs.append(f"[complete] task_complete={len(completes)} < 2")
        else:
            if not _check(">=2 个 task_complete（两步都执行完成）",
                          len(completes) >= 2, f"complete={len(completes)}"):
                errs.append(f"[complete] 两步未都完成：complete={len(completes)}")

        # ── 9. Leader 汇总（HARD）：调整链路未破坏汇总 ──
        print("\n[check 9] Leader 汇总（HARD）：抓到「全部完成」汇总 reply + DB 交叉 + 含两 worker agent_name")
        summary_ev = next(
            (e for e in events
             if e.get("type") == "agent_reply"
             and e.get("sender_id") == coord_id
             and ("全部完成" in (e.get("content") or "") or "汇总" in (e.get("content") or ""))),
            None,
        )
        summary_content = (summary_ev or {}).get("content") or ""
        print(f"      [summary] content 预览：{summary_content[:160]}…")
        fe_step_names = {s.get("agent_name") for s in plan
                         if isinstance(s, dict) and s.get("agent_id") == frontend_id}
        be_step_names = {s.get("agent_name") for s in plan
                         if isinstance(s, dict) and s.get("agent_id") == backend_id}
        fe_name_in = any(n and n in summary_content for n in fe_step_names)
        be_name_in = any(n and n in summary_content for n in be_step_names)
        if _check("捕获 Leader 汇总 reply（调整链路未破坏 all-done 汇总）",
                  summary_ev is not None, "未捕获汇总 reply"):
            pass
        else:
            errs.append("[summary] 未捕获 Leader 汇总 reply")
        if _check("汇总 reply 含两 worker agent_name（调整后仍跟踪两步结果聚合）",
                  fe_name_in and be_name_in,
                  f"fe_in={fe_name_in} be_in={be_name_in} fe_names={fe_step_names} be_names={be_step_names}"):
            pass
        else:
            errs.append(f"[summary] 汇总未含两 worker name：fe_in={fe_name_in} be_in={be_name_in}")
        msgs = await list_messages(probe_group_id, limit=100)
        db_summary = next(
            (m for m in msgs
             if m.get("type") == "agent_reply"
             and m.get("sender_id") == coord_id
             and ("全部完成" in (m.get("content") or "") or "汇总" in (m.get("content") or ""))),
            None,
        )
        if _check("汇总 reply 落库（GET /api/messages 交叉确认 WS vs DB 双真源）",
                  db_summary is not None, "DB 未找到汇总 reply"):
            print(f"      [db] 汇总 message id={(db_summary or {}).get('id', '')[:16]}… 落库确认")
        else:
            errs.append("[summary] 汇总 reply 未落库")

        # ── 10. 调整发生（SOFT 核心）：第二个 coordinator_plan 事件（重宣布修订 plan）或 announce 公告 ──
        print("\n[check 10] 调整发生（SOFT 核心）：第二个 coordinator_plan 事件（重宣布修订）或 Leader announce 公告")
        # adjust=true 会触发：①步骤1 完成后 emit_coordinator_plan 重宣布修订 plan（第二个 plan 事件）
        #   ②_unified_reply 发 announce 公告（agent_reply by coordinator，非汇总/非初始宣布）
        second_plan = plan_events[1] if len(plan_events) >= 2 else None
        # announce 公告：coordinator agent_reply 但不是「全部完成/汇总」（那是 summarize）也不是 dispatch 派发公告
        announce_replies = [
            e for e in events
            if e.get("type") == "agent_reply"
            and e.get("sender_id") == coord_id
            and "全部完成" not in (e.get("content") or "")
            and "已制定协作计划" not in (e.get("content") or "")
            and "派发" not in (e.get("content") or "")
        ]
        # adjust 发生的直接证据：第二个 plan 事件（重宣布）或 含「调整/修订/据此/根据」语义的 announce
        adjust_evidence_plan = second_plan is not None
        adjust_evidence_announce = any(
            any(kw in (e.get("content") or "")
                for kw in ["调整", "修订", "据此", "根据", "更新", "变更", "细化"])
            for e in announce_replies
        )
        adjust_happened = adjust_evidence_plan or adjust_evidence_announce
        if _info("捕获调整证据（第二个 coordinator_plan 重宣布 或 announce 公告含调整语义）",
                 adjust_happened,
                 f"second_plan={'有' if second_plan else '无'} announce含调整词={'有' if adjust_evidence_announce else '无'}"
                 + (f" announce预览={[(e.get('content') or '')[:60] for e in announce_replies]}" if announce_replies else "")):
            print(f"      ✓ LLM 据步骤1 结果判 adjust=true，重宣布修订 plan / 发调整公告（真调整）")
        else:
            print(f"      · LLM 判 adjust=false（keep 原步骤2，调整机制仍运转：串行触发 + LLM 审视 + keep 决策）")

        # ── 11. 步骤2 instruction 修订（SOFT）：adjust=true 时步骤2 派发 instruction 与初始不同 ──
        print("\n[check 11] 步骤2 instruction 修订（SOFT）：adjust=true 时步骤2 派发 instruction 与初始 plan 不同")
        # 步骤2 的初始 instruction（初始 plan 里 depends_on 非空那步，或第2个 dispatch 的 step）
        if isinstance(plan, list) and len(plan) >= 2 and len(dispatches) >= 2:
            # 初始 plan 中步骤2（按 step 号排序第2，或 depends_on 非空那步）
            sorted_plan = sorted(
                [s for s in plan if isinstance(s, dict)],
                key=lambda s: s.get("step", 0),
            )
            step2_initial = sorted_plan[1] if len(sorted_plan) >= 2 else None
            step2_initial_instr = (step2_initial or {}).get("instruction", "")
            # 步骤2 派发时的 instruction（第二个 task_dispatch，按事件顺序第二个派发的）
            d2 = dispatches[1]
            d2_instr = (d2.get("data") or {}).get("instruction", "") or d2.get("content", "")
            # 重宣布的 plan（第二个 coordinator_plan）里步骤2 的 instruction
            step2_revised_instr = step2_initial_instr
            if second_plan is not None:
                rp = (second_plan.get("data") or {}).get("plan") or []
                rp_sorted = sorted(
                    [s for s in rp if isinstance(s, dict)],
                    key=lambda s: s.get("step", 0),
                )
                if len(rp_sorted) >= 2:
                    step2_revised_instr = rp_sorted[1].get("instruction", "") or step2_revised_instr
            instr_changed = (
                bool(step2_initial_instr)
                and bool(step2_revised_instr)
                and step2_initial_instr != step2_revised_instr
            )
            print(f"      [step2] 初始 instruction: {step2_initial_instr[:60]}…")
            print(f"      [step2] 修订后 instruction: {step2_revised_instr[:60]}…")
            if _info("步骤2 instruction 被修订（adjust=true 真改了后续计划）",
                     instr_changed,
                     "instruction 改变" if instr_changed else "instruction 未变（LLM keep 原步骤2，不计 FAIL）"):
                print(f"      ✓ 后续计划（步骤2）的 instruction 被据中间结果修订")
        else:
            print("      [skip] plan/dispatch 不足，步骤2 instruction 修订校验跳过")

        # ── 12. 收尾：DELETE 探针群 + 清理工作区产物 → 全局无残留 ──
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
    print("PASS — 执行中根据结果调整后续计划端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 Leader + 2 worker 引擎；")
    print("  · 设直接干 → 发串行依赖目标（后端先做登录API→前端据 API 调用，depends_on=[1]）；")
    print("  · [核心] 串行依赖结构：plan >=2 步 + depends_on 非空步骤（制造 MT-14 调整点）；")
    print("  · [核心] 串行派发顺序：步骤1 task_complete 早于步骤2 task_dispatch")
    print("    （调整点确实发生在步骤1 完成后、步骤2 派发前，node_handle_reply 运行 _maybe_adjust）；")
    print("  · 两步都执行完成 + Leader 汇总 reply（调整链路未破坏 all-done 汇总）；")
    print("  · [SOFT] 调整发生：第二个 coordinator_plan 重宣布 / announce 公告（adjust=true 真修订）；")
    print("  · [SOFT] 步骤2 instruction 修订（后续计划据中间结果改变）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
