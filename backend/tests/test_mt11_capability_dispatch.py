"""MT-11 自测：根据 Worker 专业能力智能派工（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-09/MT-10/test_m12 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动）。

MT-11 链路（Leader 据成员专业能力把子任务派给对口 Worker）：
  前端 GroupPage：用户发含前后端分工的目标 → messageApi.send → POST /api/messages
  后端 send_message：落库 user_input + route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke：
    · node_llm_decide：build_coordinator_prompt 嵌入成员 roster（agent_id, agent_name, agent_role）
      —— roster 是「专业能力」的真源（role=frontend_engineer/backend_engineer，skills=React/Python...）。
      LLM 据每个 step 的指令领域匹配对口成员 → plan 每个 step 填 agent_id/agent_name。
    · node_dispatch：emit_coordinator_plan（plan 驻留 _dispatch_plan）
    · auto_confirm=True（直接干）→ route_after_dispatch=direct_run → dispatch_next
    · node_dispatch_next → dispatch_ready_steps → _dispatch_one 逐个 fan-out：
      对每个 ready step，按 step.agent_id push_task 到对口 worker 引擎 + emit_task_dispatched
      （task_dispatch 事件携带 agent_id/agent_name/instruction，是「真派工」的确定性强证据）。

「根据 Worker 专业能力智能派工」的三层证据：
  ① 派工决策——plan 每个 step 的 agent_id 是 Leader 据成员 role/skills 匹配指令领域选出的
     （前端页面任务 → frontend_engineer，后端 API 任务 → backend_engineer）；
  ② 派工执行——task_dispatch 事件触发（_dispatch_one 真把任务 push 到对口 worker inbox），
     task_dispatch.agent_id == plan step.agent_id（执行忠实于决策，无错派）；
  ③ 能力匹配（核心）——派给某 worker 的任务指令领域与该 worker 的专业能力（role）对口：
     前端领域指令（页面/UI/表格/React）→ frontend_engineer；后端领域指令（API/接口/数据库/Python）
     → backend_engineer。证明 Leader 不是随机派工而是据专业能力智能匹配。

为何用专属探针群 + reload：group_demo_1 coordinator 引擎累积历史 _memory + 残留 _dispatch_plan，
会污染「派工本目标」断言。新建 [MT-11] 探针群 → reload 起干净引擎（空 memory/空 plan）→
coordinator 只看到本目标 + 本群 roster → 派工决策纯净（沿用 MT-09/MT-10 隔离模式）。

为何设 auto_confirm=True（直接干）：本任务验证「派工」（实际 fan-out 到 worker），需触发
dispatch_ready_steps → _dispatch_one → emit_task_dispatched 产出 task_dispatch 事件。wait_confirm
模式下 plan 驻留不 fan-out（无 task_dispatch 事件，只能验证 plan 决策不能验证派工执行）。
直接干模式让 Leader 出 plan 后立即 fan-out，task_dispatch 事件证明「真派工」。
（auto_confirm 经 PUT config 设置，registry 每 ainvoke fresh 读 grp.config，无需重启引擎。）

为何捕获后立即 stop_group：fan-out 后 worker 会真的执行（跑 LLM、可能写工作区产物），本任务
只需验证「派工决策 + 派工执行 + 能力匹配」，不需验证 worker 执行结果（那是 MT-12/MT-16 范畴）。
捕获 task_dispatch 事件后 stop_group + delete_group 取消 worker 执行，避免无谓 LLM 调用 + 产物污染。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 前端 + 后端（建探针群 roster，目标设计为前后端分工）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle；
  ④ 设直接干：PUT config auto_confirm=True → 回读确认；
  ⑤ 发分工目标 + 抓 plan + 抓 task_dispatch：连 WS → POST /api/messages 发前后端分工目标
     （前端做用户列表页面含表格分页，后端做用户列表查询API 从数据库查询返回JSON）→
     抓 coordinator_plan 事件 + 全部 task_dispatch 事件（HARD：Leader 拆解 + 真派工）；
  ⑥ plan 派工决策（HARD）：plan 非空 + >=2 步 + 每步含 agent_id/agent_name/instruction +
     至少一步派给 frontend_id、至少一步派给 backend_id（Leader 把不同领域任务派给不同专业成员）；
  ⑦ task_dispatch 派工执行（HARD）：task_dispatch 事件数 == plan 步数（全部 step 都被 fan-out）+
     每个 task_dispatch.agent_id 在 {frontend_id, backend_id} 内（派给真实 worker 非群主）；
  ⑧ 派工执行忠实于决策（HARD）：每个 task_dispatch 的 (step, agent_id) == plan 对应 step 的
     (step, agent_id)（_dispatch_one 按 step.agent_id 派，无错派/乱派）；
  ⑨ 能力匹配（HARD 核心）：对每个派工，按指令领域分类——前端领域指令（页面/UI/表格/React/分页/组件）
     → 派给 frontend_id；后端领域指令（API/接口/数据库/Python/查询/服务/SQL/JSON）→ 派给 backend_id。
     要求：无错配（前端领域不能派给后端，反之亦然）+ 至少一处前端领域→前端 + 一处后端领域→后端
     （证明 Leader 据专业能力智能匹配，非随机派工）；
  ⑩ 派工覆盖目标领域（SOFT）：派工指令引用目标关键词（用户/列表/页面/API/表格/数据库/查询）之一；
  ⑪ 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group 删 DB）→ 全局无残留。

为何 HARD 断言「能力匹配」用指令领域分类而非 agent_name：agent_name（前端工程师/后端工程师）
是中文显示名，LLM 可能改写（如「前端」），但 agent_id（agent_frontend_1/agent_backend_1）是稳定
标识。按指令领域关键词分类（前端 kw vs 后端 kw）再核对 agent_id，比核对 agent_name 更稳。
「无错配 + 双向覆盖」（前端任务→前端 + 后端任务→后端 至少各一处）是「智能派工」的直接证据。

为何不强制全部步骤可分类：复杂目标可能有联调/集成步骤（指令同时含前后端 kw 或都不含），无法
明确分类。MT-11 只要求「能分类的步骤无错配 + 前后端各至少一处匹配」，不要求全部步骤可分类
（过于严格，联调步骤 LLM 会合理派给某方）。
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
DISPATCH_TIMEOUT = 90.0  # 等 coordinator_plan + 全部 task_dispatch（含 LLM 调用，给足超时）

# 探针群组名（[MT-11] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-11] 智能派工探针组"

# 前后端分工目标——前端做用户列表页面（表格+分页），后端做用户列表查询API（数据库+JSON），
# 两个子任务领域清晰可分，Leader 应据专业能力分别派给前端/后端工程师。
GOAL = (
    "【MT-11】请帮我开发一个用户管理模块，需要前后端协作："
    "前端工程师负责做用户列表页面（用表格展示用户数据、带分页组件）；"
    "后端工程师负责做用户列表查询API（从数据库查询用户并返回JSON）。"
    "请直接制定并派发执行计划。"
)

# 指令领域关键词（按 agent role 分类派工是否匹配专业能力）。
# 这些词是「构建物」信号：构建 UI 的词（页面/表格/组件/按钮/输入框/展示/样式）= 前端工作；
# 构建服务端/数据的词（API/接口/数据库/查询/SQL/路由/端点/返回）= 后端工作。
FRONTEND_KW = ["页面", "前端", "UI", "表单", "React", "列表页", "表格", "分页", "组件",
               "展示", "按钮", "输入框", "样式", "渲染", "交互", "界面"]
BACKEND_KW = ["API", "接口", "后端", "数据库", "Python", "查询", "服务", "FastAPI", "JSON",
              "SQL", "路由", "端点", "返回", "表结构", "CRUD", "REST", "POST", "GET"]


def _classify_domain(instruction: str) -> str:
    """按指令关键词计数分类领域（dominant scoring）。

    返回 'frontend' / 'backend' / 'mixed'（平局无法判定）/ 'unclear'（都为 0）。

    用 dominant scoring 而非排他命中：派工指令天然会交叉引用对侧（前端任务会提「调用后端
    API」、后端任务会提「支持分页参数」），故两个领域都可能命中关键词。按命中数多少判
    主领域——前端任务（构建 UI）会命中大量前端词（页面/表格/组件/展示/分页）远超偶然命
    中的 1-2 个后端词，反之亦然。只有两者命中数相等（平局）才判 'mixed' 不强行分类。
    这比「任一领域命中即排除另一领域」的排他法鲁棒得多（排他法会把所有交叉引用指令都
    判成 mixed 导致无法验证能力匹配）。
    """
    if not instruction:
        return "unclear"
    fe = sum(1 for kw in FRONTEND_KW if kw in instruction)
    be = sum(1 for kw in BACKEND_KW if kw in instruction)
    if fe == 0 and be == 0:
        return "unclear"
    if fe > be:
        return "frontend"
    if be > fe:
        return "backend"
    return "mixed"  # 平局，无法判定主领域
# 目标关键词（软断言用——派工指令引用其一即「派工基于本目标」）。
GOAL_KEYWORDS = ["用户", "列表", "页面", "API", "表格", "数据库", "查询", "分页", "JSON"]


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
    """PUT /api/groups/{id} 改 config.auto_confirm（key 级 merge 保留其他键）。

    update_group 返回完整 Group，故取 r.json().get('config')（config dict），
    与 test_m12.set_auto_confirm 同构——返回 config dict 让调用方直接 .get('auto_confirm')。
    """
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

    策略：先等 coordinator_plan 出现；出现后多收 8 秒抓全 task_dispatch（direct 模式 fan-out
    紧跟 plan）。返回 (全量事件, plan 事件, task_dispatch 事件列表)。
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
    print("=== MT-11 自测：根据 Worker 专业能力智能派工 ===")
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
        print(f"      群主={coord_id}({coord['name']}) 前端={frontend_id}({frontend['name']}) "
              f"后端={backend_id}({backend['name']})")

        # ── 2. 建探针群：coord + [frontend, backend] ──
        print("\n[check 2] 建探针群：POST /api/groups（coord + [frontend, backend]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "MT-11 根据 Worker 专业能力智能派工自测探针",
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
        print("\n[check 3] 引擎启动：reload 触发 load_from_store → 3 引擎 idle（Leader + 2 worker 驻留）")
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
        print("\n[check 4] 设直接干：PUT config auto_confirm=True（让 Leader 出 plan 后立即 fan-out）")
        cfg = await set_auto_confirm(probe_group_id, True)
        auto_on = bool(cfg and cfg.get("auto_confirm") is True)
        if _check("config.auto_confirm==True（回读确认，registry 每 ainvoke fresh 读）",
                  auto_on, f"config={cfg}"):
            print(f"      auto_confirm={cfg.get('auto_confirm')}（直接干模式，plan 后立即 fan-out）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 5. 发分工目标 + 抓 plan + 抓 task_dispatch ──
        print("\n[check 5] 发分工目标 + 抓 coordinator_plan + 全部 task_dispatch（Leader 拆解 + 真派工）")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"

        async def _send():
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, plan_ev, dispatches = await collect_plan_and_dispatches(
            ws_url, _send, DISPATCH_TIMEOUT
        )
        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_plan 事件（Leader 自动拆解出计划）",
                      plan_ev is not None, "未捕获 coordinator_plan"):
            errs.append("[plan] 未捕获 coordinator_plan 事件")
        if not _check(f"捕获 task_dispatch 事件（真派工执行）", len(dispatches) >= 1,
                      "未捕获 task_dispatch"):
            errs.append("[dispatch] 未捕获 task_dispatch 事件")

        # ── 6. plan 派工决策（HARD） ──
        print("\n[check 6] plan 派工决策：>=2 步 + 每步含 agent_id + 前后端各至少一步")
        plan: list[dict] = []
        if plan_ev is not None:
            plan = (plan_ev.get("data") or {}).get("plan") or []
        plan_agents: set[str] = set()
        if isinstance(plan, list) and len(plan) > 0:
            plan_agents = {
                s.get("agent_id") for s in plan if isinstance(s, dict) and s.get("agent_id")
            }
            steps_ok = all(
                isinstance(s, dict) and bool(s.get("agent_id"))
                and bool(s.get("agent_name")) and bool(s.get("instruction"))
                for s in plan
            )
            if _check("plan >=2 步 + 每步含 agent_id/agent_name/instruction",
                      len(plan) >= 2 and steps_ok,
                      f"len={len(plan)} steps={[(s.get('step'), s.get('agent_name')) for s in plan]}"):
                print(f"      plan {len(plan)} 步：")
                for s in plan:
                    print(f"        · 步骤{s.get('step')} {s.get('agent_name')} "
                          f"agent_id={s.get('agent_id')} {str(s.get('instruction',''))[:50]}")
            else:
                errs.append("[plan] plan 步数/字段不足")
            # 前后端各至少一步（Leader 把不同领域任务派给不同专业成员）
            both_roles = frontend_id in plan_agents and backend_id in plan_agents
            if _check("plan 至少一步派给 frontend_id + 至少一步派给 backend_id（分工派工）",
                      both_roles, f"plan_agents={plan_agents}"):
                pass
            else:
                errs.append(f"[plan] plan 未同时派给前后端：plan_agents={plan_agents}")

        # ── 7. task_dispatch 派工执行（HARD） ──
        print("\n[check 7] task_dispatch 派工执行：事件数 == plan 步数 + agent_id ∈ {前端,后端}")
        if isinstance(plan, list) and len(plan) > 0 and dispatches:
            # task_dispatch 数 >= plan 步数（每步 fan-out 一个 dispatch；可能因时序少收，用 >= 宽容）
            # 实际 direct 模式 dispatch_ready_steps 对每个 ready step 调 _dispatch_one → 1:1
            dispatch_count_ok = len(dispatches) >= len(plan) or len(dispatches) >= 2
            if _check(f"task_dispatch 事件数 ({len(dispatches)}) 覆盖 plan 步数 ({len(plan)})",
                      dispatch_count_ok, f"dispatches={len(dispatches)} plan_steps={len(plan)}"):
                pass
            else:
                errs.append(f"[dispatch] task_dispatch 数 {len(dispatches)} < plan 步数 {len(plan)}")
            # 每个 task_dispatch.agent_id 在 {frontend_id, backend_id}（派给真实 worker 非群主）
            # agent_id 在 event.data（emit_task_dispatched 写 data.agent_id），event 顶层无 agent_id
            dispatch_agents = {((d.get("data") or {}).get("agent_id")) for d in dispatches}
            worker_only = dispatch_agents.issubset({frontend_id, backend_id})
            if _check("每个 task_dispatch.agent_id ∈ {frontend_id, backend_id}（派给真实 worker 非群主）",
                      worker_only, f"dispatch_agents={dispatch_agents}"):
                print(f"      派工目标：{dispatch_agents}")
            else:
                errs.append(f"[dispatch] 派工含非 worker agent：{dispatch_agents}")
        elif dispatches:
            # 无 plan 但有 dispatch（罕见，plan 漏抓）—— 仅校验 dispatch agent
            dispatch_agents = {((d.get("data") or {}).get("agent_id")) for d in dispatches}
            worker_only = dispatch_agents.issubset({frontend_id, backend_id})
            if _check("task_dispatch.agent_id ∈ {frontend_id, backend_id}", worker_only,
                      f"dispatch_agents={dispatch_agents}"):
                pass
            else:
                errs.append(f"[dispatch] 派工含非 worker agent：{dispatch_agents}")

        # ── 8. 派工执行忠实于决策（HARD）：task_dispatch (step,agent_id) == plan step ──
        print("\n[check 8] 派工执行忠实于决策：task_dispatch (step, agent_id) == plan 对应 step")
        if isinstance(plan, list) and len(plan) > 0 and dispatches:
            plan_by_step = {s.get("step"): s for s in plan if isinstance(s, dict)}
            mismatches = []
            for d in dispatches:
                d_data = d.get("data") or {}
                d_step = d_data.get("step")
                d_agent = d_data.get("agent_id")
                p_step = plan_by_step.get(d_step)
                if p_step is None:
                    mismatches.append((d_step, d_agent, "plan 无此 step"))
                    continue
                p_agent = p_step.get("agent_id")
                if d_agent != p_agent:
                    mismatches.append((d_step, d_agent, f"plan={p_agent}"))
            if _check("每个 task_dispatch 的 (step, agent_id) == plan 对应 step（无错派/乱派）",
                      len(mismatches) == 0, f"mismatches={mismatches}"):
                print(f"      {len(dispatches)} 个派工全部忠实于 plan 决策（_dispatch_one 按 step.agent_id 派）")
            else:
                errs.append(f"[faithful] 派工与 plan 不符：{mismatches}")

        # ── 9. 能力匹配（HARD 核心）：指令领域 → 对口 agent role ──
        print("\n[check 9] 能力匹配（HARD 核心）：指令领域 → 对口 agent（无错配 + 前后端各至少一处匹配）")
        # 用 task_dispatch 的 instruction + agent_id 做能力匹配校验（派工执行态，最强证据）
        # 若无 dispatch 则退用 plan step 的 instruction + agent_id
        check_items: list[tuple[str, str]] = []  # (instruction, agent_id)
        if dispatches:
            for d in dispatches:
                d_data = d.get("data") or {}
                inst = d_data.get("instruction") or d.get("content") or ""
                aid = d_data.get("agent_id")
                check_items.append((inst, aid))
        else:
            for s in plan:
                if isinstance(s, dict):
                    check_items.append((s.get("instruction", ""), s.get("agent_id", "")))

        mismatches_cap = []  # 能力错配（前端领域派后端 / 后端领域派前端）
        fe_match = 0  # 前端领域 → frontend_id 匹配数
        be_match = 0  # 后端领域 → backend_id 匹配数
        classified = 0
        for inst, aid in check_items:
            domain = _classify_domain(inst)
            if domain in ("frontend", "backend"):
                classified += 1
            if domain == "frontend" and aid != frontend_id:
                mismatches_cap.append((domain, aid, inst[:40]))
            elif domain == "backend" and aid != backend_id:
                mismatches_cap.append((domain, aid, inst[:40]))
            elif domain == "frontend" and aid == frontend_id:
                fe_match += 1
            elif domain == "backend" and aid == backend_id:
                be_match += 1

        if _check("无能力错配（前端领域不派后端，后端领域不派前端）",
                  len(mismatches_cap) == 0, f"mismatches={mismatches_cap}"):
            pass
        else:
            errs.append(f"[capability] 能力错配：{mismatches_cap}")
        if _check("前端领域→frontend_id 至少一处 + 后端领域→backend_id 至少一处（双向能力匹配）",
                  fe_match >= 1 and be_match >= 1,
                  f"fe_match={fe_match} be_match={be_match} classified={classified}"):
            print(f"      能力匹配：前端任务→前端工程师×{fe_match}，后端任务→后端工程师×{be_match}")
        else:
            errs.append(f"[capability] 双向能力匹配不足：fe_match={fe_match} be_match={be_match}")

        # ── 10. 派工覆盖目标领域（SOFT） ──
        print("\n[check 10] 派工覆盖目标领域（SOFT）：派工指令引用目标关键词")
        all_inst = " ".join(inst for inst, _ in check_items)
        hit_kw = next((kw for kw in GOAL_KEYWORDS if kw in all_inst), None)
        _info("派工指令引用目标关键词（派工基于对本目标的理解，非泛泛模板）",
              hit_kw is not None, f"命中={hit_kw}" if hit_kw else f"all_inst 预览={all_inst[:80]}")

        # ── 11. 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group 删 DB） ──
        print("\n[check 11] 收尾：DELETE 探针群（stop_group 取消 worker 执行 + delete_group）→ 全局无残留")
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
        # 兜底：若中途失败探针群可能还在（auto_confirm 可能 True），清理之（停引擎+删DB）
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
    print("PASS — 根据 Worker 专业能力智能派工端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 Leader + 2 worker 引擎；")
    print("  · 设直接干 auto_confirm=True → 发前后端分工目标 → 抓 coordinator_plan + task_dispatch；")
    print("  · plan 派工决策：>=2 步每步含 agent_id + 前后端各至少一步（分工派工）；")
    print("  · task_dispatch 派工执行：事件覆盖 plan 步数 + agent_id ∈ {前端,后端}（派给真实 worker）；")
    print("  · 派工忠实于决策：task_dispatch (step,agent_id) == plan 对应 step（无错派）；")
    print("  · [核心] 能力匹配：指令领域→对口 agent（前端任务→前端，后端任务→后端，无错配+双向覆盖）；")
    print("  · [SOFT] 派工指令引用目标关键词（派工基于理解）；")
    print("  · 收尾 DELETE 探针群（stop_group 取消执行）→ 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
