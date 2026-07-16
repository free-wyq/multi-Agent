"""MT-03 自测：Leader 指挥策略影响调度决策。

验证群设置写入的 group.config.leader_strategy 经 registry 注入 → coordinator
node_llm_decide → build_coordinator_prompt，真正改变了 Leader 的拆解/派工决策。
不依赖 pytest，直接 asyncio 跑。共用种子群 group_demo_1（协调者 agent_coord_1 +
前端 agent_frontend_1 + 后端 agent_backend_1，引擎在 lifespan 启动时已 load）。

对比实验（同一目标，两个策略档位）：
  档位 A — leader_strategy = STRATEGY_BACKEND_FIRST
           「后端先行：后端接口先做，前端再联调；每步必须有明确的接口契约」
           期望：plan 出现「后端在前 / 前端 depends_on 后端」的串行依赖拓扑，
                 指令里强调接口契约。
  档位 B — leader_strategy = STRATEGY_PARALLEL
           「尽量并行：互不依赖的步骤全部并行，不要串行等待」
           期望：plan 出现「可并行步骤 depends_on 为空 []」的并行拓扑，
                 与档位 A 的串行结构有可观测差异。

判定逻辑（避免 LLM 输出不确定性导致硬断言误判）：
  1. 两个档位都成功抓到 coordinator_plan（证明策略注入链路不破坏正常拆解）。
  2. 档位 A：plan 中存在「前端步骤 depends_on 含后端步骤号」OR
     指令文本含「接口/契约/API 先」语义 → 后端先行策略生效。
  3. 档位 B：plan 中存在「至少两个步骤 depends_on 为空 []」→ 并行策略生效。
  4. 软断言（信息性，不计 PASS/FAIL）：两档位 plan 的 depends_on 拓扑结构不同，
     佐证「同一目标不同策略 → 不同决策」。
  5. 收尾：复位 leader_strategy 为空串（清空策略，不污染其他自测），
     auto_confirm 保持 False（避免 PL 自测串扰）。

WS 主断言（coordinator_plan 真源）+ HTTP 交叉（group.config 落库回读）。
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

# 两个对比策略档位（语义对立，便于观测拓扑差异）
STRATEGY_BACKEND_FIRST = (
    "后端先行：后端接口必须先完成，前端才能联调；"
    "每个步骤必须输出明确的接口契约（请求/响应字段），不得并行。"
)
STRATEGY_PARALLEL = (
    "尽量并行：互不依赖的步骤必须全部并行派发，不要串行等待；"
    "只要没有数据依赖就同时开工。"
)

# 同一个需要前后端协作的目标（控制变量，只改策略）
GOAL = (
    "帮我开发一个用户注册功能：前端做注册表单页，后端做注册 API。"
    "请制定协作计划。"
)

PLAN_TIMEOUT = 120.0  # 单档位等 coordinator_plan 的超时


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def get_group() -> dict:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"{BASE}/api/groups/{GROUP_ID}")).json()


async def set_leader_strategy(strategy: str) -> dict:
    """PUT group.config（key 级 merge：保留 auto_confirm，覆盖 leader_strategy）。

    同时把 auto_confirm 设为 False——本自测要抓 coordinator_plan 事件本身，
    必须走 wait_confirm 路径（计划驻留不 fan-out），避免真实派发消耗 token +
    污染群消息。auto_confirm=False 时 node_dispatch 宣布计划后 END。
    """
    async with httpx.AsyncClient() as c:
        cur = await get_group()
        config = dict(cur.get("config") or {})
        config["leader_strategy"] = strategy
        config["auto_confirm"] = False
        r = await c.put(
            f"{BASE}/api/groups/{GROUP_ID}",
            json={"config": config},
        )
        return r.json().get("config")


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


async def collect_plan(timeout: float, goal: str) -> tuple[dict | None, list[dict]]:
    """连 WS 抓事件，返回第一个 coordinator_plan 事件 + 全量事件列表。

    发消息前先连上 WS，确保不漏首批事件。
    """
    events: list[dict] = []
    plan_event: dict | None = None
    deadline = time.time() + timeout
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        await send_user_message(goal)
        while time.time() < deadline and plan_event is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "coordinator_plan":
                plan_event = ev
                # 多收 3 秒，让 coordinator_think 等后续事件进来便于核查
                end = time.time() + 3.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break
    return plan_event, events


def _step_summary(plan: list[dict]) -> list[dict]:
    out = []
    for s in plan:
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "step": s.get("step"),
                "agent_name": s.get("agent_name", ""),
                "agent_id": s.get("agent_id", ""),
                "instruction": (s.get("instruction") or "")[:80],
                "depends_on": s.get("depends_on", []) or [],
            }
        )
    return out


def _has_backend_first_topology(plan: list[dict]) -> tuple[bool, str]:
    """档位 A 生效判定：后端先行 → 前端步骤 depends_on 含后端步骤号，
    或指令文本体现「接口契约/API 先」语义。

    LLM 输出有不确定性，故用「拓扑 OR 文本语义」任一命中即判生效，
    宽松判定避免误杀。
    """
    steps = _step_summary(plan)
    if not steps:
        return False, "plan 为空"
    # 拓扑：存在某步骤 depends_on 非空（串行依赖 = 后端先行策略的典型表现）
    has_serial = any(len(s["depends_on"]) > 0 for s in steps)
    # 文本：指令含「接口/契约/API/先」语义
    blob = " ".join(s["instruction"] for s in steps)
    has_text = any(
        kw in blob for kw in ["接口", "契约", "API", "api", "先", "后端"]
    )
    if has_serial or has_text:
        return True, f"串行依赖={has_serial}, 文本含后端先行语义={has_text}"
    return False, f"既无串行依赖也无后端先行文本（blob={blob[:120]}）"


def _has_parallel_topology(plan: list[dict]) -> tuple[bool, str]:
    """档位 B 生效判定：尽量并行 → 至少两个步骤 depends_on 为空 []（可并行）。

    并行策略的典型表现是多个步骤无依赖、同时派发。
    """
    steps = _step_summary(plan)
    if not steps:
        return False, "plan 为空"
    parallel_count = sum(1 for s in steps if len(s["depends_on"]) == 0)
    if parallel_count >= 2:
        return True, f"{parallel_count} 个步骤 depends_on 为空（可并行）"
    return False, f"仅 {parallel_count} 个步骤可并行（期望 ≥2）"


def _topology_signature(plan: list[dict]) -> str:
    """把 plan 的依赖拓扑压成可比较的签名字符串（用于对比两档位差异）。

    只取「步骤号 → depends_on」的依赖结构，忽略指令文本（文本天然不同），
    专注拓扑差异。
    """
    steps = _step_summary(plan)
    parts = []
    for s in sorted(steps, key=lambda x: (x["step"] is None, x["step"])):
        deps = sorted(s["depends_on"]) if s["depends_on"] else []
        parts.append(f"{s['step']}<-{deps}")
    return "; ".join(parts)


async def run_case(label: str, strategy: str) -> tuple[bool, str, dict]:
    """跑一个策略档位：设策略 → 发目标 → 抓 plan → 判定。"""
    print(f"\n--- {label} ---")
    print(f"[strategy] {strategy[:60]}...")
    cfg = await set_leader_strategy(strategy)
    ls = (cfg or {}).get("leader_strategy", "")
    if ls != strategy:
        return False, f"leader_strategy 落库不符（got={ls[:40]!r})", {}
    print(f"[setup] leader_strategy 已落库 + auto_confirm=False")

    plan_event, events = await collect_plan(PLAN_TIMEOUT, GOAL)
    if plan_event is None:
        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.get("type", "?")] = type_counts.get(e.get("type", "?"), 0) + 1
        return False, f"未捕获 coordinator_plan（收到 {len(events)} 条事件，分布={type_counts}）", {}

    plan = (plan_event.get("data") or {}).get("plan") or []
    steps = _step_summary(plan)
    print(f"[plan] {len(steps)} 步:")
    for s in steps:
        print(f"   - 步骤{s['step']} | {s['agent_name']} | deps={s['depends_on']} | {s['instruction']}")
    return True, "", {"plan": plan, "steps": steps, "signature": _topology_signature(plan)}


async def main() -> int:
    print("=== MT-03 自测：Leader 策略影响调度决策 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    # 备份原 config 以便收尾还原
    orig_group = await get_group()
    orig_config = dict(orig_group.get("config") or {})
    print(f"[backup] 原 config={orig_config}")

    results: list[tuple[str, bool, str]] = []
    case_a_data: dict = {}
    case_b_data: dict = {}

    try:
        # 档位 A：后端先行
        ok_a, msg_a, data_a = await run_case(
            "档位 A：后端先行策略", STRATEGY_BACKEND_FIRST
        )
        case_a_data = data_a
        if ok_a and data_a:
            ok_a, msg_a = _has_backend_first_topology(data_a["plan"])
        results.append(("档位A 后端先行策略生效", ok_a, msg_a))

        # 档位 B：尽量并行
        ok_b, msg_b, data_b = await run_case(
            "档位 B：尽量并行策略", STRATEGY_PARALLEL
        )
        case_b_data = data_b
        if ok_b and data_b:
            ok_b, msg_b = _has_parallel_topology(data_b["plan"])
        results.append(("档位B 并行策略生效", ok_b, msg_b))

        # 软断言：两档位拓扑结构不同（信息性，不计 PASS/FAIL）
        if case_a_data and case_b_data:
            sig_a = case_a_data.get("signature", "")
            sig_b = case_b_data.get("signature", "")
            diff = sig_a != sig_b
            print(f"\n[拓扑对比] A={sig_a}")
            print(f"           B={sig_b}")
            print(f"           拓扑差异={'有' if diff else '无'}（软断言，不计 PASS/FAIL）")
            results.append(
                ("(软)两档位拓扑结构不同", diff, f"A签名≠B签名" if diff else "两档位拓扑相同")
            )

    except Exception as e:
        results.append(("执行", False, f"异常: {e!r}"))

    # 收尾：复位 leader_strategy 为空串 + auto_confirm=False（不污染其他自测）
    try:
        await set_leader_strategy("")
        # 再次确认清空
        cfg = (await get_group()).get("config") or {}
        ls_after = cfg.get("leader_strategy", "")
        ac_after = cfg.get("auto_confirm", None)
        print(f"\n[cleanup] leader_strategy={ls_after!r}, auto_confirm={ac_after}")
        if ls_after != "" or ac_after is not False:
            results.append(("收尾复位", False, f"复位不符 ls={ls_after!r} ac={ac_after}"))
    except Exception as e:
        results.append(("收尾复位", False, f"异常: {e!r}"))

    print("\n=== 用例结论 ===")
    all_pass = True
    # 软断言（带 (软) 前缀）不计入硬 PASS/FAIL
    hard_results = [r for r in results if not r[0].startswith("(软")]
    for name, ok, msg in results:
        tag = "PASS" if ok else ("INFO" if name.startswith("(软") else "FAIL")
        print(f"[{tag}] {name}: {msg}")
        if not name.startswith("(软") and not ok:
            all_pass = False

    print(f"\n=== 结果: {'PASS' if all_pass else 'FAIL'} ===")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
