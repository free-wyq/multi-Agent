"""M12 自测：计划确认闭环（PL-02 确认继续派发 / PL-03 直接干跳过确认）。

不依赖 pytest，直接 asyncio 跑。两条用例共用一个演示群组 group_demo_1：

用例 A — 确认继续派发（PL-02 默认 wait_confirm 路径）
  1. 先确保 group config.auto_confirm=False（等确认模式）
  2. POST 一条复杂目标消息给协调者
  3. WS 抓 coordinator_plan 事件 → 校验 plan 全 pending（证明 node_dispatch 走了
     wait_confirm 分支、graph END、计划驻留引擎未 fan-out）
  4. 此时不应有 task_dispatch 事件（验证「未确认不派发」）
  5. POST /api/groups/{id}/plan/confirm
  6. WS 抓 task_dispatch 事件（证明确认后 classify→confirm_dispatch→dispatch_next
     fan-out，绕过 LLM 零成本恢复）

用例 B — 直接干跳过确认（PL-03 auto_confirm 路径）
  1. POST /api/groups/{id}/plan/direct 把 config.auto_confirm=True
  2. POST 一条新复杂目标消息
  3. WS 抓 coordinator_plan 后紧接着抓 task_dispatch（证明 auto_confirm=True 时
     node_dispatch→direct_run→route_after_dispatch→dispatch_next 立即 fan-out，
     无需用户确认）
  4. 收尾：把 config.auto_confirm 复位 False（避免污染其他自测）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"

GOAL_A = "帮我开发一个数据导出功能：后端做 CSV 导出接口，前端做导出按钮并调用接口。请先制定协作计划。"
GOAL_B = "帮我开发一个搜索功能：后端做搜索 API，前端做搜索框并对接 API。请先制定协作计划。"

# 等待单条事件出现的超时
PLAN_TIMEOUT = 90.0
DISPATCH_TIMEOUT = 60.0


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def get_group_config() -> dict | None:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.get(f"{BASE}/api/groups/{GROUP_ID}")
        return r.json().get("config")


async def set_auto_confirm(value: bool) -> dict | None:
    """直接 PUT /api/groups/{id} 改 config（保留其他键）。"""
    async with httpx.AsyncClient(timeout=120.0) as c:
        cur = (await c.get(f"{BASE}/api/groups/{GROUP_ID}")).json()
        config = dict(cur.get("config") or {})
        config["auto_confirm"] = value
        r = await c.put(f"{BASE}/api/groups/{GROUP_ID}", json={"config": config})
        return r.json().get("config")


async def send_user_message(content: str) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as c:
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


async def plan_confirm() -> dict:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{BASE}/api/groups/{GROUP_ID}/plan/confirm")
        return r.json()


async def plan_direct() -> dict:
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{BASE}/api/groups/{GROUP_ID}/plan/direct")
        return r.json()


async def collect_until(
    predicate,
    timeout: float,
    send_action=None,
) -> tuple[list[dict], Any]:
    """连 WS 收事件直到 predicate(event) 返回真值或超时。

    send_action 在连上 WS 后立即调用（发消息/点确认），确保不漏首批事件。
    返回 (全量事件列表, predicate 命中的值)。
    """
    events: list[dict] = []
    hit: Any = None
    deadline = time.time() + timeout
    # 客户端 ping_interval=None 关闭 websockets 库的 keepalive ping（默认 20s）——协调者
    # 一回合 ainvoke 可达 50s+（reasoning 模型），客户端默认 ping 在长 recv 间隔下也易自伤。
    # max_size 抬到 8MB 防 plan+trace 大事件被拒。
    async with websockets.connect(
        WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024,
    ) as ws:
        # send_action 并发跑（不 await 阻塞 recv）：POST /api/messages 同步 await 整个
        # 协调者回合（ainvoke ~25s+），若在此 await，主循环不调 ws.recv() → 服务端的
        # keepalive ping 帧读不到、pong 不发 → ping_timeout → 1011 断连。放 background
        # task 让主循环持续 recv（websockets 在 recv 时自动回 pong），send 并行完成。
        send_task: asyncio.Task | None = None
        if send_action is not None:
            send_task = asyncio.create_task(send_action())
        while time.time() < deadline and hit is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            res = predicate(ev)
            if res:
                hit = res
                # 多收 2 秒，让紧随其后的派发/状态事件也进来
                end = time.time() + 2.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break
        if send_task is not None and not send_task.done():
            send_task.cancel()
    return events, hit


def first_plan(events: list[dict]) -> dict | None:
    for e in events:
        if e.get("type") == "coordinator_plan":
            return e
    return None


def has_task_dispatch(events: list[dict]) -> bool:
    return any(e.get("type") == "task_dispatch" for e in events)


async def case_a_confirm_resume() -> tuple[bool, str]:
    """用例 A：确认继续派发。"""
    print("\n--- 用例 A：确认继续派发（auto_confirm=False）---")
    # 1. 确保 auto_confirm=False
    cfg = await set_auto_confirm(False)
    if cfg and cfg.get("auto_confirm"):
        return False, "无法复位 auto_confirm=False"
    print(f"[setup] auto_confirm=False (config={cfg})")

    # 2. 发目标消息 + 抓 coordinator_plan
    events1, plan_ev = await collect_until(
        lambda e: e if e.get("type") == "coordinator_plan" else None,
        PLAN_TIMEOUT,
        send_action=lambda: send_user_message(GOAL_A),
    )
    if plan_ev is None:
        return False, f"未捕获 coordinator_plan（收到 {len(events1)} 条事件）"
    plan = (plan_ev.get("data") or {}).get("plan") or []
    pending_all = all(s.get("status") == "pending" for s in plan) and len(plan) > 0
    dispatched_before_confirm = has_task_dispatch(events1)
    print(f"[plan] {len(plan)} 步, 全 pending={pending_all}, 抓到 task_dispatch={dispatched_before_confirm}")
    if not pending_all:
        return False, "计划非全 pending（node_dispatch 可能未走 wait_confirm）"
    if dispatched_before_confirm:
        return False, "确认前已有 task_dispatch（wait_confirm 未生效，计划被提前 fan-out）"

    # 3. 点确认 + 抓 task_dispatch
    events2, _ = await collect_until(
        lambda e: e if e.get("type") == "task_dispatch" else None,
        DISPATCH_TIMEOUT,
        send_action=plan_confirm,
    )
    confirmed_dispatched = has_task_dispatch(events2)
    print(f"[confirm] 点确认后抓到 task_dispatch={confirmed_dispatched}（{len(events2)} 条事件）")
    if not confirmed_dispatched:
        return False, "确认后未 fan-out（classify→confirm_dispatch→dispatch_next 链路断）"
    return True, "确认继续派发闭环 PASS"


async def case_b_direct_run() -> tuple[bool, str]:
    """用例 B：直接干跳过确认。"""
    print("\n--- 用例 B：直接干跳过确认（auto_confirm=True）---")
    # 1. 切直接干模式（/plan/direct 会置 config.auto_confirm=True）
    res = await plan_direct()
    auto = res.get("auto_confirm")
    print(f"[direct] plan/direct 返回 auto_confirm={auto}, resumed={res.get('resumed_resident_plan')}")
    if auto is not True:
        return False, f"/plan/direct 未置 auto_confirm=True (got {auto})"
    # 仍有上一用例驻留的 pending 计划会被 resume——清空它后再测直接干
    # （发一条新目标，让协调者重新出计划；此时 auto_confirm 已 True，应直接 fan-out）

    # 2. 发新目标 + 抓 coordinator_plan，且紧随其后应出现 task_dispatch（无需确认）
    events, plan_ev = await collect_until(
        lambda e: e if (
            e.get("type") == "coordinator_plan"
            and any(s.get("confirm_mode") == "auto" for s in ((e.get("data") or {}).get("plan") or []))
        ) else None,
        PLAN_TIMEOUT,
        send_action=lambda: send_user_message(GOAL_B),
    )
    if plan_ev is None:
        # 退一步：可能 plan 没有 confirm_mode 字段，只看 plan 事件 + 是否 fan-out
        events, plan_ev = await collect_until(
            lambda e: e if e.get("type") == "coordinator_plan" else None,
            PLAN_TIMEOUT,
            send_action=lambda: send_user_message(GOAL_B),
        )
        if plan_ev is None:
            return False, f"未捕获 coordinator_plan（收到 {len(events)} 条事件）"
    plan = (plan_ev.get("data") or {}).get("plan") or []
    print(f"[plan] {len(plan)} 步, confirm_mode 标记={[s.get('confirm_mode') for s in plan]}")

    # 直接干模式下，coordinator_plan 之后应紧跟 task_dispatch（无需点确认）
    # 再多等一段时间专门抓 task_dispatch
    events2, _ = await collect_until(
        lambda e: e if e.get("type") == "task_dispatch" else None,
        DISPATCH_TIMEOUT,
    )
    all_events = events + events2
    dispatched = has_task_dispatch(all_events)
    print(f"[direct-run] 抓到 task_dispatch={dispatched}（额外 {len(events2)} 条事件）")
    if not dispatched:
        return False, "直接干模式下未自动 fan-out（auto_confirm→direct_run→dispatch_next 链路断）"
    return True, "直接干跳过确认 PASS"


async def main() -> int:
    print("=== M12 自测：计划确认闭环 ===")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    results: list[tuple[str, bool, str]] = []
    try:
        ok_a, msg_a = await case_a_confirm_resume()
        results.append(("A 确认继续派发", ok_a, msg_a))
    except Exception as e:
        results.append(("A 确认继续派发", False, f"异常: {e!r}"))

    try:
        ok_b, msg_b = await case_b_direct_run()
        results.append(("B 直接干跳过确认", ok_b, msg_b))
    except Exception as e:
        results.append(("B 直接干跳过确认", False, f"异常: {e!r}"))

    # 收尾：复位 auto_confirm=False，避免污染后续自测
    try:
        await set_auto_confirm(False)
        print("\n[cleanup] auto_confirm 复位 False")
    except Exception as e:
        print(f"[cleanup] 复位失败（非致命）: {e!r}")

    print("\n=== 用例结论 ===")
    all_pass = True
    for name, ok, msg in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")
        if not ok:
            all_pass = False

    print(f"\n=== 结果: {'PASS' if all_pass else 'FAIL'} ===")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
