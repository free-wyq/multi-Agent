"""MT-09 自测：Leader 接收用户任务并理解目标（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL-01/test_m12 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动，MT-07 已验证 reload 可靠）。

MT-09 链路（用户发任务 → Leader 接收 → 理解目标 → 回应）：
  前端 GroupPage：用户在群聊输入框发消息 → messageApi.send → POST /api/messages
  后端 send_message：
    · crud.create_message 落库 user_input 消息 + emit_message_added（WS 推送）
    · route_user_message——无 @mention → push_notify 到 coordinator（kind=coordinator_reply，
      唤醒 coordinator 引擎 inbox）
  coordinator 引擎 _handle_notify：
    · LangGraph ainvoke → classify（新需求 incoming_kind=coordinator_reply → llm_decide）
    · node_llm_decide：build_coordinator_prompt 嵌入用户消息 + 成员 + 上下文 → LLM 调用
      → emit_coordinator_think(action, content)【「理解目标」的核心证据——大脑跑了这条消息】
      → 返回 decision(action/content/plan)
    · route_after_llm_decide → chat（直接回复）或 dispatch（出计划）
    · node_chat/node_dispatch：_unified_reply 落库 agent_reply 消息 + emit_message_added
      （dispatch 还 emit_coordinator_plan）

「Leader 接收用户任务理解目标」的两层证据：
  ① 接收——user_input 消息落库 + route_user_message 把 notify 推给 coordinator 引擎；
  ② 理解——coordinator_think 事件触发（coordinator 大脑对这条消息跑了 LLM），think.content
     是 coordinator 对目标的理解/推理（非空 + 引用目标关键词=真理解，非空=至少跑通）；
  ③ 回应——coordinator 落库一条 agent_reply 消息（sender_id==coordinator_id），是对目标的回应
     （chat 直答 或 dispatch 宣布计划），证明 Leader 不仅收到还产出了基于理解的响应。

为何用专属探针群 + reload：group_demo_1 的 coordinator 引擎累积了 PL-01/test_m12/MT-03 的
历史 _memory + 残留 _dispatch_plan，对话上下文会污染「理解本条目标」的断言（think/reply 可能
引用历史目标）。新建 [MT-09] 探针群 → reload 触发 load_from_store 起干净引擎（空 memory/空 plan）
→ coordinator 只看到本条目标 → think/reply 引用本目标=「真理解本目标」的强证据。reload 是
MT-07 已验证的可靠手段（引擎无按需启动，仅 load_from_store 启动）。

验证块（HARD 硬断言 + SOFT 软断言）：
  ① 前置：候选池含 coordinator + 2 成员角色（建探针群的 roster 来源）；
  ② 建探针群：POST /api/groups（coord + [frontend, backend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 3 引擎 idle（coordinator + 2 成员），
     证明 Leader 引擎驻留（接收任务的前提）；
  ④ 发任务 + 抓 think：连 WS → POST /api/messages 发 distinctive 目标 → 抓 coordinator_think
     事件（HARD：coordinator 大脑对本条消息跑了 LLM = 接收并处理）；
  ⑤ think 结构合法：think.action ∈ {chat,dispatch,ask,continue} + think.content 非空（HARD）；
  ⑥ 理解目标（SOFT）：think.content 或后续 reply 引用目标关键词（注册/表单/API/邮箱/密码/校验/
     数据库/前端/后端）之一——LLM 可能改写，引用=真理解，未引用不计 FAIL（INFO）；
  ⑦ Leader 回应：GET /api/messages 探针群最近消息含 user_input（我发的目标，HARD 接收证据）
     + agent_reply（sender_id==coordinator_id，HARD 回应证据）+ reply content 非空（HARD）；
  ⑧ dispatch 分支（条件 HARD）：若 think.action==dispatch 或抓到 coordinator_plan 事件 →
     plan 非空 list + 每步含 agent_name/instruction（Leader 据理解产出计划）；
  ⑨ 收尾：DELETE 探针群（stop_group + delete_group）→ 全局列表无残留。

为何不直接断言 think.content == 目标：LLM 输出不确定，think 内容是 coordinator 自由生成的
理解/推理，不可硬比对文案（不同次运行措辞不同）。硬断言「think 事件触发 + action 合法 + content
非空」（结构性证明大脑跑了）+ 软断言「引用目标关键词」（语义证明真理解），与 PL-01 软断言 plan
质量、MT-04 软断言 name 文案同立场（LLM 输出用软断言避免 flaky）。

为何断言 agent_reply 落库：coordinator 的回应经 _unified_reply → crud.create_message 持久化
（type=agent_reply, sender_id=coordinator_id）。这是「Leader 不仅收到还回应」的确定性强证据
（落库 = 真发生了，非 WS 事件时序幻觉）。与 think 事件双证：think=理解发生，reply=回应落库。

为何 auto_confirm 不显式设：新探针群 config==None → auto_confirm=False（registry 读
grp.config.get("auto_confirm", False)，None.config 安全跳过返 False）→ wait_confirm 模式 →
coordinator 出计划后宣布并 END 等待确认，不 fan-out 到 worker（隔离「理解」与「派工执行」，
派工是 MT-10/MT-11 范畴）。即使 action=chat 也只回复不派工。本自测聚焦「接收+理解+回应」。
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
RELOAD_WAIT = 45.0  # touch main.py 后等健康恢复 + load_from_store 的总时限
THINK_TIMEOUT = 90.0  # 等 coordinator_think 事件（含 LLM 调用，给足超时）

# 探针群组名（[MT-09] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-09] Leader理解目标探针组"

# distinctive 目标——含多个可检测关键词（注册/表单/API/邮箱/密码/校验/数据库/前端/后端），
# 让 coordinator 的 think/reply 引用其中之一即证明「理解了本目标」。
GOAL = (
    "【MT-09】请帮我开发一个用户注册功能：前端工程师做注册表单页面（含用户名、密码、邮箱三个字段），"
    "后端工程师做注册API（校验邮箱格式与密码强度后写入数据库）。请先理解这个需求。"
)

# 目标关键词（软断言用——think/reply 引用其一即「真理解」）。
GOAL_KEYWORDS = ["注册", "表单", "API", "邮箱", "密码", "校验", "数据库", "前端", "后端"]


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


async def list_messages(groupId: str, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/messages", params={"groupId": groupId, "limit": str(limit)})
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
            pass  # reload 期间短暂不可用，重试
        await asyncio.sleep(1.0)
    return False


async def collect_until_think(
    ws_url: str, send_action, timeout: float
) -> tuple[list[dict], dict | None]:
    """连 WS，send_action 发消息，收事件直到 coordinator_think 出现或超时。

    命中 think 后多收 5 秒，让 coordinator_plan / message_added(reply) 也进来。
    返回 (全量事件, think 事件)。
    """
    events: list[dict] = []
    think_ev: dict | None = None
    deadline = time.time() + timeout
    async with websockets.connect(ws_url) as ws:
        if send_action is not None:
            await send_action()
        while time.time() < deadline and think_ev is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_think":
                think_ev = ev
                # 多收 5 秒，让紧随其后的 plan / reply 事件也进来
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
    return events, think_ev


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _info(name: str, cond: bool, detail: str = "") -> None:
    """软断言：不计 FAIL，只 INFO 报告（LLM 输出相关的语义检查）。"""
    mark = "✓" if cond else "·"
    tag = "INFO" if cond else "SOFT-MISS"
    print(f"  {mark} [{tag}] {name}" + (f" — {detail}" if detail else ""))


def _has_keyword(text: str) -> str | None:
    """返回 text 命中的首个目标关键词，未命中返 None。"""
    if not text:
        return None
    for kw in GOAL_KEYWORDS:
        if kw in text:
            return kw
    return None


async def main() -> int:
    print("=== MT-09 自测：Leader 接收用户任务并理解目标 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None

    try:
        # ── 1. 前置：候选池含 coordinator + 2 成员角色 ──
        print("\n[check 1] 前置：GET /api/agents 候选池含 coordinator + 前端 + 后端")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        frontend = next((a for a in agents if a.get("role") == "frontend_engineer"), None)
        backend = next((a for a in agents if a.get("role") == "backend_engineer"), None)
        # 兜底：种子角色缺失退化为取前 3 个
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
            "description": "MT-09 Leader 接收用户任务理解目标自测探针",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        # 逐个 addMember（与前端 handleCreate 一致，create 后 Promise.all addMember）
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
        if not _check("reload 后探针群 3 引擎 idle（coordinator + 2 成员）", ready,
                      "reload 后引擎未到位"):
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

        # ── 4. 发任务 + 抓 think ──
        print("\n[check 4] 发任务 + 抓 coordinator_think（Leader 大脑跑了这条消息）")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"

        async def _send():
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, think_ev = await collect_until_think(ws_url, _send, THINK_TIMEOUT)
        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        if not _check("捕获 coordinator_think 事件（Leader 接收并跑 LLM 理解目标）",
                      think_ev is not None, "未捕获 coordinator_think"):
            errs.append("[think] 未捕获 coordinator_think 事件")
        else:
            assert think_ev is not None
            print(f"      think sender={think_ev.get('sender_id')} action="
                  f"{(think_ev.get('data') or {}).get('action')}")

        # ── 5. think 结构合法 ──
        print("\n[check 5] think 结构合法：action ∈ 合法集 + content 非空")
        if think_ev is not None:
            action = (think_ev.get("data") or {}).get("action", "")
            content = think_ev.get("content") or ""
            if _check("think.action ∈ {chat,dispatch,ask,continue}",
                      action in ("chat", "dispatch", "ask", "continue"), f"action={action!r}"):
                pass
            else:
                errs.append(f"[think] action 非法：{action!r}")
            if _check("think.content 非空（Leader 对目标有理解/推理输出）",
                      bool(content.strip()), "content 为空"):
                print(f"      think.content 预览：{content[:120]}…")
            else:
                errs.append("[think] content 为空")
        else:
            errs.append("[think] 无 think 事件，结构校验跳过")

        # ── 6. 理解目标（SOFT）：think/reply 引用目标关键词 ──
        print("\n[check 6] 理解目标（SOFT）：think/reply 引用目标关键词")
        think_content = (think_ev or {}).get("content") or ""
        kw = _has_keyword(think_content)
        _info("think.content 引用目标关键词（真理解本目标，非泛泛）",
              kw is not None, f"命中={kw}" if kw else f"think 预览={think_content[:80]}")

        # ── 7. Leader 回应：消息落库 user_input + agent_reply ──
        print("\n[check 7] Leader 回应：GET /api/messages 含 user_input + agent_reply(coordinator)")
        msgs = await list_messages(probe_group_id, limit=50)
        # user_input：我发的目标（接收证据）
        user_msgs = [m for m in msgs if m.get("type") == "user_input" and GOAL[:20] in (m.get("content") or "")]
        if _check("user_input 消息落库（系统收到用户任务）", len(user_msgs) >= 1,
                  f"找到 {len(user_msgs)} 条匹配 user_input"):
            pass
        else:
            # 宽松匹配：任何 user_input 都算（content 可能被截断）
            any_user = [m for m in msgs if m.get("type") == "user_input"]
            if _check("（宽松）存在 user_input 消息", len(any_user) >= 1,
                      f"user_input 总数={len(any_user)}"):
                pass
            else:
                errs.append("[recv] 无 user_input 消息落库")
        # agent_reply：coordinator 的回应（sender_id==coordinator_id）
        coord_replies = [
            m for m in msgs
            if m.get("type") == "agent_reply" and m.get("sender_id") == coord_id
        ]
        if _check("agent_reply 消息落库 + sender_id==coordinator_id（Leader 回应）",
                  len(coord_replies) >= 1, f"找到 {len(coord_replies)} 条 coordinator 回应"):
            reply_content = coord_replies[0].get("content") or ""
            print(f"      reply 预览：{reply_content[:120]}…")
            if _check("reply content 非空", bool(reply_content.strip()), "reply 为空"):
                pass
            else:
                errs.append("[reply] coordinator 回应 content 为空")
            # SOFT：reply 引用目标关键词
            rkw = _has_keyword(reply_content)
            _info("reply 引用目标关键词（回应基于对本目标的理解）",
                  rkw is not None, f"命中={rkw}" if rkw else f"reply 预览={reply_content[:80]}")
        else:
            errs.append("[reply] 无 coordinator agent_reply 消息落库")

        # ── 8. dispatch 分支（条件 HARD）：若出计划，plan 结构合法 ──
        print("\n[check 8] dispatch 分支（条件）：若 think.action==dispatch 或抓到 plan，校验 plan 结构")
        plan_ev = next((e for e in events if e.get("type") == "coordinator_plan"), None)
        action = (think_ev or {}).get("data", {}).get("action", "") if think_ev else ""
        if action == "dispatch" or plan_ev is not None:
            if plan_ev is None:
                errs.append("[plan] action==dispatch 但未抓到 coordinator_plan 事件")
                _check("抓到 coordinator_plan 事件", False, "action=dispatch 但无 plan 事件")
            else:
                plan = (plan_ev.get("data") or {}).get("plan") or []
                if _check("plan 非空 list", isinstance(plan, list) and len(plan) > 0,
                          f"plan={plan}"):
                    steps_ok = all(
                        isinstance(s, dict) and bool(s.get("agent_name"))
                        and bool(s.get("instruction"))
                        for s in plan
                    )
                    if _check("每步含 agent_name + instruction（Leader 据理解产出计划）",
                              steps_ok, f"steps={[(s.get('step'), s.get('agent_name')) for s in plan]}"):
                        print(f"      plan {len(plan)} 步：")
                        for s in plan:
                            print(f"        · 步骤{s.get('step')} {s.get('agent_name')} "
                                  f"deps={s.get('depends_on')} {str(s.get('instruction',''))[:50]}")
                    else:
                        errs.append("[plan] 步骤缺 agent_name/instruction")
                    # SOFT：plan 步骤引用目标领域
                    plan_text = " ".join(
                        str(s.get("instruction", "")) + str(s.get("agent_name", "")) for s in plan
                    )
                    pkw = _has_keyword(plan_text)
                    _info("plan 步骤引用目标领域（计划基于对本目标的理解）",
                          pkw is not None, f"命中={pkw}" if pkw else "")
                else:
                    errs.append("[plan] plan 为空或非 list")
        else:
            print(f"      think.action={action!r}（非 dispatch，Leader 选择直接回复/提问，"
                  f"不出计划——仍算「理解目标」并回应）")

        # ── 9. 收尾：DELETE 探针群 → 全局无残留 ──
        print("\n[check 9] 收尾：DELETE 探针群（stop_group + delete_group）→ 全局无残留")
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
        # 兜底：若中途失败探针群可能还在，清理之（停引擎 + 删 DB）
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
    print("PASS — Leader 接收用户任务并理解目标端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起干净 Leader 引擎（空 memory）；")
    print("  · 发 distinctive 目标 → 捕获 coordinator_think（Leader 大脑跑了这条消息）；")
    print("  · think.action 合法 + content 非空（Leader 对目标有理解/推理输出）；")
    print("  · [SOFT] think/reply 引用目标关键词（真理解本目标，非泛泛）；")
    print("  · user_input 落库（系统收到任务）+ agent_reply(coordinator) 落库（Leader 回应）；")
    print("  · [条件] dispatch 分支 plan 非空 + 每步 agent_name/instruction（据理解产出计划）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
