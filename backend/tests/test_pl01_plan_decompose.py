"""PL-01 自测：验证 coordinator 对复杂目标自动拆解多步可执行计划（depends_on 正确）。

不依赖 pytest（环境未装），直接用 asyncio 跑。流程：
1. 连 WS bus 抓 group_demo_1 事件流
2. POST 一条用户消息「做一个用户登录模块」给协调者
3. 轮询事件流，等待 coordinator_plan 事件出现
4. 校验 plan：多步、有 depends_on、至少 2 步、步骤有 agent_name/agent_id
5. 允许后续 dispatch（真实 LLM 会真正派发）；只校验计划结构本身
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"

# 一个需要前后端协作、天然多步且有依赖的复杂目标
GOAL = "帮我开发一个用户登录功能：前端做登录表单页，后端做登录校验 API，再由前端联调对接 API。请制定协作计划。"

TIMEOUT = 90.0  # 秒


async def fetch_messages(limit: int = 200) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/messages", params={"groupId": GROUP_ID, "limit": str(limit)})
        return r.json()


async def send_user_message(content: str) -> dict:
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


async def collect_plan(timeout: float) -> tuple[dict | None, list[dict]]:
    """连 WS 抓事件，返回第一个 coordinator_plan 事件 + 全量事件列表。"""
    events: list[dict] = []
    plan_event: dict | None = None
    deadline = time.time() + timeout

    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        # 发消息前先连上 WS，确保不漏事件
        sent = await send_user_message(GOAL)
        print(f"[send] user message id={sent['id'][:16]}...")

        while time.time() < deadline and plan_event is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_plan":
                plan_event = ev
            # 多收 3 秒，让后续 coordinator_think 也进来，便于核查
            if plan_event is not None:
                end = time.time() + 3.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break

    return plan_event, events


def validate_plan(plan_event: dict | None) -> tuple[bool, str, list[dict]]:
    if plan_event is None:
        return False, "未捕获到 coordinator_plan 事件", []
    plan = (plan_event.get("data") or {}).get("plan")
    if not isinstance(plan, list) or len(plan) == 0:
        return False, "plan 为空或非 list", plan or []

    errs = []
    steps = []
    for i, s in enumerate(plan, 1):
        if not isinstance(s, dict):
            errs.append(f"步骤{i} 非 dict")
            continue
        step_no = s.get("step")
        agent_name = s.get("agent_name")
        agent_id = s.get("agent_id")
        instruction = s.get("instruction", "")
        depends_on = s.get("depends_on", [])
        if not agent_name:
            errs.append(f"步骤{step_no} 缺 agent_name")
        if not agent_id:
            errs.append(f"步骤{step_no} 缺 agent_id")
        if not instruction:
            errs.append(f"步骤{step_no} 缺 instruction")
        if not isinstance(depends_on, list):
            errs.append(f"步骤{step_no} depends_on 非 list")
        steps.append({
            "step": step_no, "agent": agent_name, "instruction": instruction[:60],
            "depends_on": depends_on,
        })

    # 校验多步
    if len(plan) < 2:
        errs.append(f"计划仅 {len(plan)} 步，期望 ≥2 步（复杂目标应多步拆解）")

    # 校验 depends_on 引用的步骤号都存在
    step_nums = {s.get("step") for s in plan if isinstance(s, dict)}
    for s in plan:
        if isinstance(s, dict):
            for dep in s.get("depends_on", []) or []:
                if dep not in step_nums:
                    errs.append(f"步骤{s.get('step')} depends_on 引用了不存在的步骤 {dep}")

    return (len(errs) == 0), "\n".join(errs) if errs else "OK", steps


async def main() -> int:
    print("=== PL-01 自测：coordinator 自动拆解多步计划 ===")
    # 确认后端在线
    async with httpx.AsyncClient() as c:
        h = await c.get(f"{BASE}/health")
        if h.json().get("status") != "ok":
            print("[fatal] backend health 不 ok"); return 2
    print("[health] ok")

    print(f"[goal] {GOAL}")
    plan_event, events = await collect_plan(TIMEOUT)

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        type_counts[e.get("type", "?")] = type_counts.get(e.get("type", "?"), 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    ok, msg, steps = validate_plan(plan_event)
    print(f"[plan] 校验={'PASS' if ok else 'FAIL'}: {msg}")
    print("[plan] 步骤:")
    for s in steps:
        print(f"   - 步骤{s['step']} | {s['agent']} | deps={s['depends_on']} | {s['instruction']}")

    # 从历史消息再交叉确认计划卡片可见
    msgs = await fetch_messages(200)
    plan_msgs = [m for m in msgs if (m.get("data") or {}).get("plan")]
    print(f"[history] messages={len(msgs)} 含 plan 的消息={len(plan_msgs)}")

    if ok:
        print("\n=== 结果: PASS ===")
        return 0
    else:
        print("\n=== 结果: FAIL ===")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
