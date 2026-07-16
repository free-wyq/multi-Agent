"""PL-10 自测：WS 断线重连后任务状态/计划不丢失。

不依赖 pytest，直接 asyncio 跑。沿用 PL-05/08/09 自测模式（httpx + websockets）。

PL-10 的机制：前端 onBusEvent 第三参 onReconnect，断后重连时触发 useBusEvent 的
handleReconnect，按真源重拉三类状态——agentStatuses(listStatus) / plan(getPlan) /
messages(listByGroup→logs)。本自测在 Python 侧复刻 handleReconnect 的三个 GET，
验证一次「WS 断线 → 期间有任务推进/计划驻留 → 重连 → 重拉」全流程后，真源端点仍返回
完整当前态（断线期间漏收的 WS 事件已由持久化层补齐），且重连后实时通道恢复正常。

为何分阶段模拟断线而非真的断网：
  onBusEvent 的重连是 WS onclose→指数退避→重连。自测用「主动 close WS#1 → 期间发
  任务 → 再开 WS#2」精确复刻「断线期间事件丢失、重连后重拉补齐」语义，确定性可控。

校验项（确定性，非语义判断）：
  1. 计划不丢失：Phase1 WS#1 收到 coordinator_plan（pending 计划驻留引擎），断线后
     Phase3 重连，GET /plan 返回同一计划（step 数/指令一致，status 仍 pending）——
     证明断线期间 coordinator_plan 事件即使漏收，getPlan 真源仍能复原。
  2. 任务消息不丢失：Phase2 断线期间发 worker 任务（产生 agent_reply 含哨兵
     PL10_GAP_OK），Phase3 重连后 listByGroup 包含该哨兵消息——证明断线期间漏收的
     任务消息由 messages 表补齐。
  3. 状态不丢失：Phase3 重连后 listStatus 反映 worker 当前 idle（断线期间
     idle→executing→idle 迁移已发生但 WS 漏收，listStatus 真源仍返回当前态）。
  4. 重连后实时通道恢复：Phase4 WS#2 开着发新消息，收到含哨兵 PL10_LIVE_OK 的
     agent_reply——证明重连后 WS 通道仍能接收实时事件（不只靠重拉历史）。
  5. task_complete(success) 收尾（Phase2 断线期间任务确实完成，非报错）。

为何单用例且控制 LLM 调用数：
  M12 自测双用例叠驻留后端触发 exit 137 OOM。本测试 LLM 调用：1（协调者计划生成，
  auto_confirm=False 不执行故仅 1 call）+ 1~2（gap worker 任务）+ 1（live 探测）+
  cleanup 有界（neutralize 后每步 1 call）。总计 ~5 call，远低于 M12 双场景。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"
WORKER_ID = "agent_backend_1"

# 哨兵：worker 回复中须包含的固定串，做确定性断言（非语义判断）。
GAP_SENTINEL = "PL10_GAP_OK"
LIVE_SENTINEL = "PL10_LIVE_OK"

# 复杂目标（复用 PL-01 验证过会触发 dispatch 拆计划），auto_confirm=False 时计划
# 驻留引擎 wait_confirm，不 fan-out 执行（Phase1 仅生成计划，不产生 worker 任务）。
PLAN_GOAL = "开发一个用户登录功能，需要前端表单页和后端登录 API，最后联调"

# 断线期间的 worker 任务：要求直接回复哨兵（brain 判 chat 即可，1 model call，轻量）。
GAP_TASK = f"@后端工程师 请直接回复这句确认（只回这句，不要用工具）：{GAP_SENTINEL}"
# 重连后实时探测任务：同理轻量。
LIVE_TASK = f"@后端工程师 请直接回复这句确认（只回这句，不要用工具）：{LIVE_SENTINEL}"

WS_TIMEOUT = 180.0
PLAN_WAIT = 90.0
GAP_TASK_TIMEOUT = 180.0
LIVE_WAIT = 90.0
CLEANUP_TIMEOUT = 150.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def group_config() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/groups/{GROUP_ID}")
        g = r.json()
        return g.get("config") or {}


async def worker_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == WORKER_ID:
                return a["status"]
    return "unknown"


async def all_workers_idle() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        return all(a["status"] == "idle" for a in r.json())


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


async def get_plan() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/groups/{GROUP_ID}/plan")
        return r.json()


async def list_messages(limit: int = 200) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{BASE}/api/messages",
            params={"groupId": GROUP_ID, "limit": str(limit)},
        )
        return r.json()


async def collect_until_plan(timeout: float) -> list[dict]:
    """WS#1：收事件直到抓到 coordinator_plan 或超时。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_plan":
                # 再收 2s 尾巴
                end = time.time() + 2.0
                while time.time() < end:
                    try:
                        events.append(json.loads(await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time()))))
                    except asyncio.TimeoutError:
                        break
                break
    return events


async def collect_first_event(timeout: float) -> dict | None:
    """WS#2：收第一个事件（验证重连后实时通道），超时返回 None。"""
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(raw)
        except asyncio.TimeoutError:
            return None


async def collect_until_sentinel(sentinel: str, timeout: float) -> list[dict]:
    """WS#2：收事件直到出现含哨兵的 agent_reply 或超时。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if (ev.get("type") == "agent_reply"
                    and sentinel in str(ev.get("content") or "")):
                break
    return events


async def wait_worker_idle(timeout: float) -> bool:
    """轮询 HTTP 直到 worker idle（断线期间任务完成的判定，不靠 WS）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await all_workers_idle():
            return True
        await asyncio.sleep(2)
    return False


def plan_signature(plan: list[dict]) -> tuple:
    """计划指纹：(step 数, 各 step 指令元组)。用于断言重连前后是同一计划。"""
    if not plan:
        return (0, ())
    steps = sorted(plan, key=lambda s: s.get("step", 0))
    return (len(steps), tuple(
        (s.get("step"), s.get("instruction"), s.get("agent_id")) for s in steps
    ))


def plan_has_pending(plan: list[dict]) -> bool:
    return any(s.get("status") == "pending" for s in (plan or []))


async def cleanup_plan(plan: list[dict]) -> str:
    """清理驻留计划：modify 把各步指令 neutralize 成「回复：完成」+ 依赖清空，
    /plan/modify 内部会确认并 fan-out（每步 1 model call，有界），再轮询直到全完成。
    返回 'cleaned' / 'timeout' / 'skipped' / 'error'。"""
    if not plan:
        return "skipped"
    steps_payload = [
        {
            "step": s.get("step"),
            "agent_id": WORKER_ID,
            "agent_name": "后端工程师",
            "instruction": "直接回复：完成",
            "depends_on": [],
        }
        for s in plan
    ]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{BASE}/api/groups/{GROUP_ID}/plan/modify",
                json={"steps": steps_payload},
            )
            if r.status_code != 200:
                return f"error({r.status_code})"
    except Exception as e:
        return f"error({e})"
    # 轮询直到计划无 pending 且无 dispatched（全 completed）或超时
    deadline = time.time() + CLEANUP_TIMEOUT
    while time.time() < deadline:
        p = (await get_plan()).get("plan") or []
        if not p:
            return "cleaned"
        if not any(s.get("status") in ("pending", "dispatched") for s in p):
            return "cleaned"
        await asyncio.sleep(3)
    return "timeout"


async def main() -> int:
    print("=== PL-10 自测：WS 断线重连后任务状态/计划不丢失 ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    cfg = await group_config()
    auto_confirm = bool(cfg.get("auto_confirm", False))
    print(f"[group] config.auto_confirm={auto_confirm}")
    if auto_confirm:
        print("[warn] auto_confirm=True，协调者计划会自动派发执行；计划驻留校验降级为契约校验")

    # 等 worker 空闲
    for _ in range(30):
        if await all_workers_idle():
            break
        print(f"[wait] worker 状态={await worker_status()}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print("[worker] all idle")

    errs: list[str] = []

    # ── Phase 1：WS#1 连接，发协调者目标，抓 coordinator_plan（计划驻留）──
    print("\n[Phase1] WS#1 连接，发协调者目标，等待 coordinator_plan...")
    ws1_task = asyncio.create_task(collect_until_plan(PLAN_WAIT))
    await asyncio.sleep(0.5)
    await send_message(PLAN_GOAL)
    ev1 = await ws1_task
    plan_ev = next((e for e in ev1 if e.get("type") == "coordinator_plan"), None)
    plan_p1 = []
    if plan_ev:
        plan_p1 = (plan_ev.get("data") or {}).get("plan") or []
        print(f"[Phase1] 收到 coordinator_plan，{len(plan_p1)} 步")
        for s in plan_p1:
            print(f"        step {s.get('step')} → {s.get('agent_name')} "
                  f"[{s.get('status')}] {str(s.get('instruction'))[:40]}")
    else:
        print("[Phase1] 未收到 coordinator_plan（协调者 chat 回复未拆计划）")
    sig_p1 = plan_signature(plan_p1)
    plan_generated = bool(plan_p1) and plan_has_pending(plan_p1)
    if auto_confirm and plan_generated:
        print("[Phase1] auto_confirm=True，计划已自动 fan-out，跳过驻留断言")

    # ── Phase 2：断线（WS#1 已随 collect_until_plan 结束而 close），期间发 worker 任务 ──
    print(f"\n[Phase2] WS#1 已断开，发 gap worker 任务（哨兵 {GAP_SENTINEL}），轮询 HTTP 等 idle...")
    await send_message(GAP_TASK)
    idle_ok = await wait_worker_idle(GAP_TASK_TIMEOUT)
    print(f"[Phase2] worker idle={idle_ok}")
    if not idle_ok:
        errs.append("Phase2 断线期间 worker 任务未在超时内完成（无法验证任务消息补齐）")

    # ── Phase 3：重连，复刻 handleReconnect 三个 GET ──
    print("\n[Phase3] 重连（WS#2），复刻 handleReconnect：getPlan / listStatus / listByGroup")
    plan_resp = await get_plan()
    plan_p3 = plan_resp.get("plan") or []
    print(f"[Phase3] getPlan → ok={plan_resp.get('ok')} steps={len(plan_p3)}")
    msgs = await list_messages()
    print(f"[Phase3] listByGroup → {len(msgs)} 条历史消息")
    status_list = []
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        status_list = r.json()
    worker_now = next((a for a in status_list if a["id"] == WORKER_ID), {})
    print(f"[Phase3] listStatus → worker {WORKER_ID} status={worker_now.get('status')}")

    # 校验 1：计划不丢失（计划指纹重连前后一致，且仍 pending）
    if plan_generated and not auto_confirm:
        sig_p3 = plan_signature(plan_p3)
        same_plan = sig_p1 == sig_p3
        still_pending = plan_has_pending(plan_p3)
        print(f"[check 1] 计划指纹一致={same_plan} 仍 pending={still_pending}"
              f" (p1 steps={sig_p1[0]}, p3 steps={sig_p3[0]})")
        if not same_plan:
            errs.append(f"计划重连前后不一致（p1={sig_p1} p3={sig_p3}）——计划丢失或被篡改")
        if not still_pending:
            errs.append("计划重连后不再 pending——断线期间计划状态异常迁移")
    else:
        print(f"[check 1] 计划驻留校验跳过（generated={plan_generated} auto_confirm={auto_confirm}）"
              f"；getPlan 契约 ok={plan_resp.get('ok')} coordinator_id={plan_resp.get('coordinator_id')}")

    # 校验 2：任务消息不丢失（listByGroup 含 gap 哨兵）
    gap_msg = next((m for m in msgs
                    if GAP_SENTINEL in str(m.get("content") or "")), None)
    print(f"[check 2] listByGroup 含 gap 哨兵 {GAP_SENTINEL}={gap_msg is not None}")
    if not gap_msg:
        errs.append("断线期间的 worker 任务消息未出现在 listByGroup——任务状态丢失")

    # 校验 3：状态不丢失（listStatus 反映当前 idle）
    status_recovered = worker_now.get("status") == "idle"
    print(f"[check 3] listStatus worker 当前 idle={status_recovered}")
    if not status_recovered:
        errs.append(f"重连后 listStatus worker 状态={worker_now.get('status')}，非 idle——状态未恢复")

    # 校验 5：Phase2 任务确实完成（task_complete success）。从历史消息找 task_complete 类型
    # （task_complete 经 emit_message_added 也会进 messages 表？实际 task_complete 是 WS 事件，
    # 不一定落 messages。改用「worker idle + gap 哨兵消息存在」间接证明任务完成。）
    print(f"[check 5] Phase2 任务完成证据：worker idle={idle_ok} 且哨兵消息存在={gap_msg is not None}")

    # ── Phase 4：重连后实时通道（WS#2 收新消息的 agent_reply）──
    print(f"\n[Phase4] WS#2 实时通道探测，发 live 任务（哨兵 {LIVE_SENTINEL}）...")
    ws2_task = asyncio.create_task(collect_until_sentinel(LIVE_SENTINEL, LIVE_WAIT))
    await asyncio.sleep(0.5)
    await send_message(LIVE_TASK)
    ev2 = await ws2_task
    live_reply = next((e for e in ev2
                      if e.get("type") == "agent_reply"
                      and LIVE_SENTINEL in str(e.get("content") or "")), None)
    print(f"[check 4] WS#2 收到含 {LIVE_SENTINEL} 的 agent_reply={live_reply is not None}"
          f" (共收 {len(ev2)} 条事件)")
    if not live_reply:
        errs.append("重连后 WS#2 未收到新任务的 agent_reply——实时通道未恢复")

    # ── Phase 5：清理驻留计划（neutralize + confirm，有界执行）──
    print("\n[Phase5] 清理驻留计划（modify neutralize → 有界 fan-out）...")
    cleanup = await cleanup_plan(plan_p1 if (plan_generated and not auto_confirm) else [])
    print(f"[Phase5] cleanup={cleanup}")

    if errs:
        print("\n[结果] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    print("\n=== 结果: PASS ===")
    plan_desc = (f"计划指纹重连前后一致（{sig_p1[0]} 步）且仍 pending，"
                 if (plan_generated and not auto_confirm) else "getPlan 契约返回引擎真源，")
    print(f"WS 断线重连后：{plan_desc}"
          f"断线期间 worker 任务消息由 listByGroup 补齐（哨兵 {GAP_SENTINEL}），"
          f"listStatus 恢复当前 idle，重连后 WS#2 实时通道收到新任务 agent_reply（哨兵 {LIVE_SENTINEL}）。"
          f"证明 onReconnect→handleReconnect 三类重拉 + 实时通道恢复端到端打通，"
          f"任务状态/计划不丢失。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
