"""MT-10 自测：自动拆解为可并行子任务（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-09/PL-01/test_m12 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动）。

MT-10 链路（Leader 自动拆解 → 可并行子任务）：
  前端 GroupPage：用户发一个可并行的复杂目标 → messageApi.send → POST /api/messages
  后端 send_message：落库 user_input + route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke：
    · classify（新需求 coordinator_reply → llm_decide）
    · node_llm_decide：build_coordinator_prompt（COORDINATOR_SYSTEM 明确「能并行的步骤就并行：
      互不依赖的步骤 depends_on 设为 []」）→ LLM 调用 → emit_coordinator_think + 返回 decision
    · route_after_llm_decide（action==dispatch → dispatch）
    · node_dispatch：emit_coordinator_plan（plan 驻留引擎 _dispatch_plan，wait_confirm 模式）
  LLM 拆解出的 plan：每个 step 含 step/agent_id/agent_name/instruction/depends_on。
    「可并行」= 多个 step 的 depends_on == []（find_ready_steps 一并返回 → dispatch_ready_steps
    fan-out 到各自 worker 引擎并发执行，MT-12 范畴验证并发执行，本任务验证「拆出可并行结构」）。

「自动拆解为可并行子任务」的三层证据：
  ① 自动拆解——用户只发一句目标，coordinator 自动产出多步 plan（无人工干预拆解）；
  ② 多步——plan 含 >=2 步（复杂目标拆成多个子任务，非单步直答）；
  ③ 可并行——>=2 步 depends_on == []（find_ready_steps 会一并返回，证明拓扑上可并行 fan-out）。

为何用专属探针群 + reload：group_demo_1 coordinator 引擎累积历史 _memory + 残留 _dispatch_plan，
会污染「拆解本目标」的断言（plan 可能引用历史目标/叠加历史 plan）。新建 [MT-10] 探针群 →
reload 起干净引擎（空 memory/空 plan）→ coordinator 只看到本目标 → 拆出的 plan 是对本目标的
纯净拆解（沿用 MT-09 验证的隔离模式）。

为何设 auto_confirm=False（wait_confirm）：本任务验证「拆解为可并行子任务」（plan 结构），
不验证「执行」（fan-out 并发执行是 MT-12 范畴）。wait_confirm 模式下 node_dispatch 出 plan
后宣布并 END，不 fan-out（隔离「拆解」与「执行」）。plan 驻留引擎可经 GET /plan 交叉验证。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster，目标设计为前后端可并行）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle（Leader 驻留）；
  ④ 发可并行目标 + 抓 plan：连 WS → POST /api/messages 发前后端可并行的目标（前端做登录页 +
     后端做登录API，天然独立可并行）→ 抓 coordinator_plan 事件（HARD：Leader 自动拆解出 plan）；
  ⑤ plan 多步结构（HARD）：plan 非空 list + len >=2 + 每步含 step/agent_name/agent_id/instruction +
     depends_on 是 list（拆解出多个完整子任务，非单步）；
  ⑥ 可并行结构（HARD 核心）：>=2 步 depends_on == []（find_ready_steps 会一并返回 → 可并行 fan-out，
     证明拓扑上独立可并行，是 MT-10「可并行子任务」的直接证据）；
  ⑦ depends_on 引用合法（HARD）：每步 depends_on 引用的 step 号都在 plan 内存在（无悬空依赖）；
  ⑧ plan_get 独立真源交叉（HARD）：GET /api/groups/{id}/plan 返回的 plan == WS 抓到的 plan
     （驻留引擎 _dispatch_plan 是真源，避免「WS 写啥读啥」的同源幻觉）；
  ⑨ 拆解覆盖目标领域（SOFT）：plan 步骤的 agent_name+instruction 引用目标关键词（登录/表单/API/
     用户名/密码/前端/后端）之一，证明拆解基于对本目标的理解而非泛泛模板（LLM 可能改写，引用=真拆解）；
  ⑩ 收尾：DELETE 探针群（stop_group + delete_group）→ 全局列表无残留。

为何 HARD 断言「>=2 步 depends_on == []」是核心：MT-10 的「可并行」语义 = 拓扑上独立。
find_ready_steps 返回所有 pending + deps 满足的步骤，若 >=2 步 depends_on==[] 则它们同时
ready → dispatch_ready_steps 一并 fan-out（dispatcher.py for step in ready: _dispatch_one），
各自 worker 引擎并发执行（MT-12 验证并发执行）。故「>=2 步空 deps」是「可并行子任务」的
直接结构性证据。若所有步骤都串行依赖（每步 depends_on 前一步），则无可并行性，不满足 MT-10。

为何不强制全部步骤可并行：复杂目标可能既有可并行部分（前后端独立）又有串行依赖（联调依赖前后端）。
MT-10 只要求「拆出可并行子任务」（>=2 步空 deps），不要求「全部步骤可并行」（那过于严格，
LLM 对联调步骤会合理设依赖）。>=2 步空 deps 即证明 Leader 识别了并行机会并拆出并行结构。

为何用 plan_get 交叉验证：WS 抓的 coordinator_plan 事件是「宣布时的快照」，plan_get 读引擎
_dispatch_plan 是「当前驻留态」。两者都源自同一 _dispatch_plan，理论必相等；校验它排除
「WS 事件与引擎驻留态不一致」的回归（如未来 plan 在 announce 后被意外篡改）。与 MT-02
plan_get 交叉验证 coordinator_id、MT-08 单读==列表元素同理（独立真源交叉）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
import websockets

BASE = "http://localhost:8000"
BACKEND_MAIN = "/home/wyq/work/project/multi-Agent/backend/main.py"

TIMEOUT = 20.0
RELOAD_WAIT = 45.0
PLAN_TIMEOUT = 90.0  # 等 coordinator_plan 事件（含 LLM 调用，给足超时）

# 探针群组名（[MT-10] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-10] 可并行拆解探针组"

# 可并行目标——前端做登录页 + 后端做登录API，天然独立可并行（无前后依赖）。
# 设计为两个独立子任务，coordinator 应拆出 >=2 步 depends_on==[]。
GOAL = (
    "【MT-10】请帮我开发一个登录功能，需要前后端同时开工："
    "前端工程师做登录页面（含用户名和密码输入框、登录按钮），"
    "后端工程师做登录API（接收用户名密码、校验后返回token）。"
    "这两件事互不依赖，可以并行。请制定协作计划。"
)

# 目标关键词（软断言用——plan 步骤引用其一即「真拆解本目标」）。
GOAL_KEYWORDS = ["登录", "表单", "API", "用户名", "密码", "token", "前端", "后端", "页面"]


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


async def delete_group(group_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


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


async def collect_until_plan(
    ws_url: str, send_action, timeout: float
) -> tuple[list[dict], dict | None]:
    """连 WS，send_action 发消息，收事件直到 coordinator_plan 出现或超时。

    命中 plan 后多收 5 秒，让紧随其后的 think/reply 也进来。返回 (全量事件, plan 事件)。
    """
    events: list[dict] = []
    plan_ev: dict | None = None
    deadline = time.time() + timeout
    async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        if send_action is not None:
            await send_action()
        while time.time() < deadline and plan_ev is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_plan":
                plan_ev = ev
                end = time.time() + 5.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break
    return events, plan_ev


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _info(name: str, cond: bool, detail: str = "") -> None:
    """软断言：不计 FAIL，只 INFO 报告（LLM 输出相关的语义检查）。"""
    mark = "✓" if cond else "·"
    tag = "INFO" if cond else "SOFT-MISS"
    print(f"  {mark} [{tag}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("=== MT-10 自测：自动拆解为可并行子任务 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None

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
        if not _check("候选池含 coordinator + 2 成员", coord and frontend and backend,
                      f"coord={bool(coord)} fe={bool(frontend)} be={bool(backend)}"):
            errs.append("[pool] 候选不足 3 个，无法建探针群")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord and frontend and backend
        coord_id = coord["id"]
        print(f"      群主={coord_id}({coord['name']}) 成员=[{frontend['id']}({frontend['name']}),"
              f"{backend['id']}({backend['name']})]")

        # ── 2. 建探针群：coord + [frontend, backend] ──
        print("\n[check 2] 建探针群：POST /api/groups（coord + [frontend, backend]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "MT-10 自动拆解为可并行子任务自测探针",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        for aid in (frontend["id"], backend["id"]):
            await add_member(probe_group_id, aid, None)
        if _check("group_ 前缀 + coordinator_id==群主", str(g["id"]).startswith("group_")
                  and g.get("coordinator_id") == coord_id):
            print(f"      样本：id={g['id'][:24]}… coord={coord_id}")
        else:
            errs.append("[create] 群结构异常")

        # ── 3. 引擎启动：reload → 轮询 status 直到 3 引擎 idle ──
        print("\n[check 3] 引擎启动：reload 触发 load_from_store → 3 引擎 idle（Leader 驻留）")
        ready = await wait_for_engines(probe_group_id, expected=3)
        if not _check("reload 后探针群 3 引擎 idle", ready, "reload 后引擎未到位"):
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'], e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            ids = {e["id"] for e in engines}
            all_idle = all(e.get("status") == "idle" for e in engines)
            if _check("3 引擎 id 含 coordinator + 2 成员 且全 idle",
                      coord_id in ids and frontend["id"] in ids and backend["id"] in ids and all_idle,
                      f"ids={ids} statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎集合/idle 不符")

        # ── 4. 发可并行目标 + 抓 plan ──
        print("\n[check 4] 发可并行目标 + 抓 coordinator_plan（Leader 自动拆解）")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"

        async def _send():
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, plan_ev = await collect_until_plan(ws_url, _send, PLAN_TIMEOUT)
        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_plan 事件（Leader 自动拆解出计划）",
                      plan_ev is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获 coordinator_plan 事件")

        # ── 5. plan 多步结构（HARD） ──
        print("\n[check 5] plan 多步结构：非空 list + >=2 步 + 每步字段完整 + depends_on 是 list")
        plan: list[dict] = []
        if plan_ev is not None:
            plan = (plan_ev.get("data") or {}).get("plan") or []
        if _check("plan 非空 list", isinstance(plan, list) and len(plan) > 0, f"plan={plan}"):
            pass
        else:
            errs.append("[plan] plan 为空或非 list")
        if isinstance(plan, list) and len(plan) > 0:
            if _check(f"plan >=2 步（复杂目标多步拆解）", len(plan) >= 2, f"仅 {len(plan)} 步"):
                pass
            else:
                errs.append(f"[plan] 仅 {len(plan)} 步，期望 >=2")
            # 每步字段完整
            steps_ok = all(
                isinstance(s, dict)
                and s.get("step") is not None
                and bool(s.get("agent_name"))
                and bool(s.get("agent_id"))
                and bool(s.get("instruction"))
                and isinstance(s.get("depends_on"), list)
                for s in plan
            )
            if _check("每步含 step/agent_name/agent_id/instruction + depends_on 是 list",
                      steps_ok, f"steps={[(s.get('step'), s.get('agent_name')) for s in plan]}"):
                print(f"      plan {len(plan)} 步：")
                for s in plan:
                    print(f"        · 步骤{s.get('step')} {s.get('agent_name')} "
                          f"deps={s.get('depends_on')} {str(s.get('instruction',''))[:50]}")
            else:
                errs.append("[plan] 步骤字段不完整")

        # ── 6. 可并行结构（HARD 核心）：>=2 步 depends_on == [] ──
        print("\n[check 6] 可并行结构（HARD 核心）：>=2 步 depends_on == []（find_ready_steps 一并返回 → 可并行 fan-out）")
        if isinstance(plan, list) and len(plan) > 0:
            empty_deps = [s for s in plan if isinstance(s, dict) and s.get("depends_on") == []]
            if _check(f">=2 步 depends_on == []（可并行子任务）",
                      len(empty_deps) >= 2, f"空 deps 步骤数={len(empty_deps)}"):
                print(f"      可并行步骤：{[(s.get('step'), s.get('agent_name')) for s in empty_deps]}")
                print(f"      → find_ready_steps 会一并返回这 {len(empty_deps)} 步 → "
                      f"dispatch_ready_steps fan-out 到各自 worker 并发执行")
            else:
                errs.append(f"[parallel] 仅 {len(empty_deps)} 步空 deps，期望 >=2（无可并行结构）")

        # ── 7. depends_on 引用合法（HARD） ──
        print("\n[check 7] depends_on 引用合法：每步 depends_on 引用的 step 号都在 plan 内存在")
        if isinstance(plan, list) and len(plan) > 0:
            step_nums = {s.get("step") for s in plan if isinstance(s, dict)}
            dangling = []
            for s in plan:
                if not isinstance(s, dict):
                    continue
                for dep in s.get("depends_on", []) or []:
                    if dep not in step_nums:
                        dangling.append((s.get("step"), dep))
            if _check("无悬空依赖（所有 depends_on 引用合法 step 号）",
                      len(dangling) == 0, f"悬空={dangling}"):
                pass
            else:
                errs.append(f"[deps] 悬空依赖：{dangling}")

        # ── 8. plan_get 独立真源交叉（HARD） ──
        print("\n[check 8] plan_get 独立真源交叉：GET /api/groups/{id}/plan == WS 抓到的 plan")
        pg = await get_plan(probe_group_id)
        resident_plan = (pg.get("plan") or []) if isinstance(pg, dict) else []
        # 交叉验证：驻留 plan 的 step 数 + agent_name 集合 == WS plan
        ws_steps = {(s.get("step"), s.get("agent_name"), s.get("agent_id")) for s in plan if isinstance(s, dict)}
        resident_steps = {(s.get("step"), s.get("agent_name"), s.get("agent_id"))
                          for s in resident_plan if isinstance(s, dict)}
        cross_ok = (
            isinstance(pg, dict)
            and pg.get("coordinator_id") == coord_id
            and len(resident_plan) == len(plan)
            and ws_steps == resident_steps
        )
        if _check("plan_get.coordinator_id==群主 + plan 步数==WS + (step,agent_name,agent_id) 集合一致",
                  cross_ok, f"pg={pg}"):
            print(f"      驻留引擎 plan {len(resident_plan)} 步 == WS plan（独立真源交叉一致）")
        else:
            errs.append(f"[cross] plan_get 与 WS plan 不一致：pg={pg}")

        # ── 9. 拆解覆盖目标领域（SOFT） ──
        print("\n[check 9] 拆解覆盖目标领域（SOFT）：plan 步骤引用目标关键词")
        if isinstance(plan, list) and len(plan) > 0:
            plan_text = " ".join(
                str(s.get("instruction", "")) + str(s.get("agent_name", "")) for s in plan
                if isinstance(s, dict)
            )
            hit_kw = next((kw for kw in GOAL_KEYWORDS if kw in plan_text), None)
            _info("plan 步骤引用目标关键词（拆解基于对本目标的理解，非泛泛模板）",
                  hit_kw is not None, f"命中={hit_kw}" if hit_kw else f"plan_text 预览={plan_text[:80]}")

        # ── 10. 收尾：DELETE 探针群 → 全局无残留 ──
        print("\n[check 10] 收尾：DELETE 探针群（stop_group + delete_group）→ 全局无残留")
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
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项硬断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 自动拆解为可并行子任务端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起干净 Leader 引擎；")
    print("  · 发可并行目标（前端登录页 + 后端登录API 互不依赖）→ 抓 coordinator_plan；")
    print("  · plan >=2 步 + 每步字段完整（step/agent_name/agent_id/instruction/depends_on list）；")
    print("  · [核心] >=2 步 depends_on==[]（find_ready_steps 一并返回 → 可并行 fan-out）；")
    print("  · depends_on 引用合法（无悬空依赖）；")
    print("  · plan_get 独立真源交叉 == WS plan（驻留引擎 _dispatch_plan 一致）；")
    print("  · [SOFT] plan 步骤引用目标关键词（拆解基于理解非模板）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
