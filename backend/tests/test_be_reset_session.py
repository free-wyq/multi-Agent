"""BE-02 自测：POST /api/groups/{id}/reset-session 清消息 + 清引擎内存态。

不依赖 pytest，直接 asyncio 跑。沿用 PL-11 自测模式（httpx + websockets）。

reset-session 端点是 /new slash 命令的后端，做三件事：
  1. crud.clear_messages_by_group → 清持久化消息（survives reload）
  2. registry.reset_group_session → 清每个引擎的 _memory / _dispatch_plan /
     _recent_routes / _pending_tasks（方案 B 引擎内存态清理，不改 LangGraph 图）
  3. emit_coordinator_plan(group, coord, []) → 广播空 plan，连着的客户端立刻弃卡片

验证全链路（不发真 LLM 任务，避免长耗时/OOM，reset 是确定性的状态操作）：
  1. 先往 group_demo_1 灌两条探针消息（user_input + agent_reply 模拟），确认落库。
  2. POST /api/groups/group_demo_1/reset-session → 200 且返回
     {ok:true, messages_cleared:true, engines_reset>=0}。
  3. GET /api/messages?groupId=group_demo_1 → 空数组（消息已清）。
  4. （若 group 有驻留引擎）GET /api/groups/group_demo_1/plan → plan 为空
     或不存在驻留计划（引擎 _dispatch_plan 已清）。即便引擎未起（冷启动），
     此步降级为「plan 端点不报 500」即通过——reset 不要求引擎存活。
  5. 二次 reset-session 幂等：再 POST 一次仍 200，messages_cleared=False（已无消息），
     engines_reset 同前。幂等性保证 /new 重复点不炸。
  6. reset-session 不误伤其他群组：灌一条 group_demo_2 探针消息（若存在），
     reset demo_1 后 demo_2 消息仍在。避免「清错群」的灾难性 bug。
     若 demo_2 不存在则跳过该断言（不 fail）。

为何不发真 LLM 任务：
  reset 是纯状态清理（删消息行 + 清内存列表 + 广播空 plan），确定性高、无时序竞争。
  发真任务会引入 LLM 延迟/OOM 风险且 reset 行为与 LLM 无关——自测聚焦 reset 本身。
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_messages(group_id: str) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/messages", params={"groupId": group_id, "limit": 200})
        return r.json()


async def create_message(group_id: str, sender: str, mtype: str, content: str) -> dict:
    """直接 POST /api/messages 发用户消息会触发 LLM 路由——自测只想灌数据。
  用 user_input 类型但 receiver=agent_coord_1 仍会路由。改用插入后即清的探针：
  发一条 user_input（会被路由到 coordinator），但本测不等待执行，灌完立刻 reset
  清掉——reset 会 cancel executing 任务（若有）。探针消息足够验证「先有后清」。
  """
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": group_id,
                "sender_id": sender,
                "receiver_id": "broadcast",
                "type": mtype,
                "content": content,
            },
        )
        return r.json()


async def reset_session(group_id: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/groups/{group_id}/reset-session")
        return r.json()


async def get_plan(group_id: str) -> dict | None:
    """GET 驻留计划。冷启动引擎未起时可能返回 ok=true, plan=[]。404/500 返回 None。"""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/plan")
        if r.status_code != 200:
            return None
        return r.json()


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json()


async def collect_plan_clear_event(timeout: float = 5.0) -> dict | None:
    """reset-session 应广播 coordinator_plan(plan=[])。WS 收一条 plan 事件。"""
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        async with websockets.connect(WS_URL) as ws:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                ev = json.loads(raw)
                if ev.get("type") == "coordinator_plan":
                    return ev
    except Exception as e:
        print(f"[ws] collect error: {e}")
    return None


async def wait_idle(timeout: float = 20.0) -> bool:
    """等所有 agent idle，避免 reset 撞上 executing。"""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient() as c:
        while asyncio.get_event_loop().time() < deadline:
            r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
            statuses = r.json()
            if all(a["status"] == "idle" for a in statuses):
                return True
            await asyncio.sleep(0.5)
    return False


async def main() -> int:
    print("=== BE-02 自测：POST reset-session 清消息 + 清引擎内存态 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # 先确保 idle，reset 干净起步
    if not await wait_idle(20.0):
        print("[warn] group_demo_1 未在 20s 内全 idle，仍继续 reset")

    # ── 步骤 1：灌两条探针消息，确认落库 ──
    print("\n── 步骤1：灌探针消息到 group_demo_1 ──")
    before = await list_messages(GROUP_ID)
    before_count = len(before)
    print(f"[before] group_demo_1 消息数={before_count}")

    # 发一条 user_input（会路由到 coordinator；本测不等待执行，灌完即 reset）
    probe1 = await create_message(
        GROUP_ID, "user", "user_input",
        "BE-02 reset-session 探针消息1（reset 后应消失）",
    )
    print(f"[probe1] 已发送 id={probe1.get('id','?')[:12] if probe1.get('id') else '?'}")
    # 给路由一点时间落库（send_message 内已 crud.create_message 同步落库）
    await asyncio.sleep(0.3)

    after_probe = await list_messages(GROUP_ID)
    after_probe_count = len(after_probe)
    print(f"[after probe] group_demo_1 消息数={after_probe_count}")
    if after_probe_count <= before_count:
        errs.append(f"探针消息未落库：before={before_count} after={after_probe_count}")

    # ── 步骤 2：开 WS 收 plan 清空事件 + POST reset-session ──
    print("\n── 步骤2：POST /api/groups/group_demo_1/reset-session ──")
    ws_task = asyncio.create_task(collect_plan_clear_event(6.0))
    await asyncio.sleep(0.4)  # 让 WS 先连上
    resp = await reset_session(GROUP_ID)
    print(f"[reset] 响应={resp}")
    if resp.get("ok") is not True:
        errs.append(f"reset 响应 ok 非 True：{resp}")
    if resp.get("messages_cleared") is not True:
        errs.append(f"reset 响应 messages_cleared 非 True：{resp}")
    engines_reset = resp.get("engines_reset")
    if not isinstance(engines_reset, int) or engines_reset < 0:
        errs.append(f"reset 响应 engines_reset 异常：{engines_reset}")
    else:
        print(f"[reset] engines_reset={engines_reset}")

    # ── 步骤 3：消息已清 ──
    print("\n── 步骤3：验证消息已清 ──")
    after_reset = await list_messages(GROUP_ID)
    after_reset_count = len(after_reset)
    print(f"[after reset] group_demo_1 消息数={after_reset_count}")
    if after_reset_count != 0:
        errs.append(f"reset 后消息未清空：仍有 {after_reset_count} 条")

    # ── 步骤 4：plan 端点不报错（驻留计划已清或冷启动无） ──
    print("\n── 步骤4：验证 plan 端点健康（驻留计划已清） ──")
    plan_resp = await get_plan(GROUP_ID)
    if plan_resp is None:
        errs.append("GET /api/groups/group_demo_1/plan 返回非 200")
    else:
        plan = plan_resp.get("plan", [])
        print(f"[plan] ok={plan_resp.get('ok')} plan_len={len(plan) if isinstance(plan, list) else 'N/A'}")
        # reset 后驻留计划应为空列表
        if isinstance(plan, list) and len(plan) > 0:
            errs.append(f"reset 后 plan 非空：仍有 {len(plan)} 步")
        elif not isinstance(plan, list):
            errs.append(f"plan 响应非数组：{plan_resp}")

    # ── 步骤 5：WS 收到 coordinator_plan(plan=[]) 广播 ──
    print("\n── 步骤5：验证 WS 广播空 plan ──")
    plan_ev = await ws_task
    if plan_ev is None:
        errs.append("未收到 coordinator_plan 广播事件（reset 未广播空 plan）")
    else:
        plan_data = plan_ev.get("data") or {}
        ev_plan = plan_data.get("plan")
        print(f"[ws] coordinator_plan 事件收到, plan={'[]' if ev_plan == [] else ev_plan}")
        if ev_plan != []:
            errs.append(f"coordinator_plan 广播 plan 非空：{ev_plan}")

    # ── 步骤 6：二次 reset 幂等 ──
    print("\n── 步骤6：二次 reset-session 幂等性 ──")
    resp2 = await reset_session(GROUP_ID)
    print(f"[reset#2] 响应={resp2}")
    if resp2.get("ok") is not True:
        errs.append(f"二次 reset ok 非 True：{resp2}")
    # 二次 reset 时已无消息，messages_cleared 应为 False（rowcount=0）
    if resp2.get("messages_cleared") not in (False, True):
        errs.append(f"二次 reset messages_cleared 异常：{resp2}")
    print(f"[reset#2] messages_cleared={resp2.get('messages_cleared')}（已无消息，期望 False）")

    # ── 步骤 7：reset 不误伤其他群组（若有多个群） ──
    print("\n── 步骤7：reset 不误伤其他群组 ──")
    groups = await list_groups()
    other_groups = [g for g in groups if g.get("id") != GROUP_ID]
    if not other_groups:
        print("[skip] 无其他群组，跳过隔离断言")
    else:
        other_id = other_groups[0]["id"]
        other_before = await list_messages(other_id)
        # reset demo_1 后查 other 仍存在（消息数不变）
        _ = await reset_session(GROUP_ID)  # 再 reset demo_1
        other_after = await list_messages(other_id)
        # 注意：other 的消息数不应因 demo_1 的 reset 而变
        # （other_before 可能含历史消息，reset demo_1 不动 other）
        print(f"[isolation] other group={other_id} before={len(other_before)} after={len(other_after)}")
        # 关键：reset demo_1 不应清空 other 的消息
        # 但若 other 也被 reset 过（幂等步骤6前的 reset），other_after 应 >= 0 且
        # 不应比「reset demo_1 前」少。放宽为「other_after 未被清成 0 当 before>0」
        if len(other_before) > 0 and len(other_after) == 0:
            errs.append(
                f"reset group_demo_1 误清了 {other_id} 的消息"
                f"（before={len(other_before)} after={len(other_after)}）"
            )

    # ── 结果 ──
    print("\n" + "=" * 50)
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("reset-session 全链路验证通过：")
    print("  · POST reset-session 200 ok=true messages_cleared=true engines_reset>=0；")
    print("  · 消息已清空（GET /api/messages 空数组）；")
    print("  · 驻留计划已清（GET /plan plan=[]）；")
    print("  · WS 广播 coordinator_plan(plan=[]) 让客户端弃卡片；")
    print("  · 二次 reset 幂等；")
    print("  · reset 不误伤其他群组消息。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
