"""VH36 回归：dispatch_next 节点 Send fan-out（去中心化 handoff 迁移·派发层）.

锁住 task-8 决策——coordinator dispatch_next 节点：dispatcher.dispatch_ready_steps 输出从
``push_task`` 改为 LangGraph ``Send``/并行 fan-out 到各 agent 节点（保 DAG fail-fast 与
ready_steps 逻辑）.

设计真源见 memory ``decentralized-scheduling-stop-plan-2026-07-13``（方向 A）+ [[group-graph-assembly-added]].
本任务把 DAG 派发从「resident ``dispatch_ready_steps`` → ``push_task`` 到 worker inbox（band-out 经
引擎 run loop）」改为「group-graph ``build_dispatch_sends`` → ``Send`` 到 agent 节点（in-graph 并行
fan-out，LangGraph 一 ``ainvoke`` 内并发）」. **保 DAG fail-fast + ready_steps 逻辑**——两者调同一
``apply_fail_fast`` + ``find_ready_steps``（单一真源），只是传输层不同.

五段契约（纯静态 + 真 StateGraph stub，不依赖 live server / 真实 LLM）：

  A. 派发工厂锁——build_dispatch_sends 返 Send 列表
    1. ``engine.dispatcher`` 新增 ``build_dispatch_sends(group_id, coordinator_id, plan)`` 工厂.
    2. ``build_dispatch_sends`` 调 ``apply_fail_fast(plan)`` + ``find_ready_steps(plan)``（与
       ``dispatch_ready_steps`` 同一 DAG 真源，fail-fast + ready 逻辑不漂移）.
    3. ``build_dispatch_sends`` 返 ``(sends, dispatched_steps)`` 元组——sends 是 ``Send`` 列表
       （每个 ready step 一个），dispatched_steps 是 mutated（pending→dispatched + task_id）的 step 列表.

  B. Send fan-out 语义锁——并行到各 agent 节点
    4. 每个 ``Send`` 的目标节点名 = ``agent_<agent_id>``（``agent_node_target`` 单一真源，与
       group_graph/worker 的 ``agent_<id>`` 命名一致）.
    5. 每个 ``Send`` 的 payload 含 step 的 instruction（``incoming_message``）+ coordinator_id
       （``incoming_sender``）+ step identity（``incoming_data``）——agent 节点据此建 brain context.
    6. 真 StateGraph 跑通：``node_dispatch_next_group`` 返 ``Command(goto=sends, update=dispatch_plan)``，
       LangGraph 并行驱动各 agent 节点（每个收自己的 instruction）.

  C. DAG fail-fast + ready_steps 保真锁——与 resident dispatch_ready_steps 同源
    7. fail-fast：step1 failed + step2 depends_on step1 → build_dispatch_sends 把 step2 级联 failed，
       不派发（``apply_fail_fast`` 同源）.
    8. ready_steps：step1 completed + step2 depends_on step1 → step2 ready 被派发；
       step1 pending + step2 depends_on step1 → step2 不 ready 不派发（deps 未满足）.
    9. step mutation 一致：dispatched step 的 ``status``="dispatched" + ``task_id`` 被设（与
       resident ``_dispatch_one`` 同款 mutation，handle_reply 的 task_id 匹配不破）.

  D. 路由保真锁——summarize/END 分支与 resident 同款
   10. 无 dispatchable + all done → ``action_taken="summarize"`` → route_after_dispatch_next → summarize.
   11. 无 dispatchable + not all done → END（in-flight steps running，report-back 下回合再进）.
   12. 有 dispatched → ``Command(goto=sends)``（LangGraph 跟 Send 走，route_after_dispatch_next 不被咨询）.

  E. 向后兼容锁——resident dispatch_ready_steps 不破
   13. ``dispatch_ready_steps`` 仍在（resident coordinator 图仍用，m12/mt15/mt16/vh10/vh35 不破）.
   14. ``_dispatch_one`` / ``apply_fail_fast`` / ``find_ready_steps`` 仍在（resident 派发链不破）.
   15. ``node_dispatch_next``（resident）仍在（驻留图未改）；``node_dispatch_next_group``（群图 twin）
       是新增 additive.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
DISPATCHER_PY = BACKEND / "engine" / "dispatcher.py"
COORD_PY = BACKEND / "engine" / "coordinator.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    disp_src = _read(DISPATCHER_PY)
    coord_src = _read(COORD_PY)

    try:
        from engine.dispatcher import (  # type: ignore
            AGENT_NODE_PREFIX,
            agent_node_target,
            apply_fail_fast,
            build_dispatch_sends,
            dispatch_ready_steps,
            find_ready_steps,
            _dispatch_one,
        )
        from engine.coordinator import (  # type: ignore
            node_dispatch_next,
            node_dispatch_next_group,
            route_after_dispatch_next,
            build_coordinator_subnodes,
        )
        from langgraph.types import Send
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. 派发工厂 ──────────────────────────────────────────
    # A1 build_dispatch_sends 工厂存在
    if not callable(build_dispatch_sends):
        errs.append("[A1] build_dispatch_sends 不可调用")
    else:
        sig = inspect.signature(build_dispatch_sends)
        for p in ("group_id", "coordinator_id", "plan"):
            if p not in sig.parameters:
                errs.append(f"[A1] build_dispatch_sends 缺参数 {p}")
        if not any(e.startswith("[A1]") for e in errs):
            print("[A1] OK  build_dispatch_sends(group_id, coordinator_id, plan) 工厂存在")

    # A2 调 apply_fail_fast + find_ready_steps（同源 DAG 真源）
    bds_body = _fn_body(disp_src, "build_dispatch_sends", is_async=False)
    if "apply_fail_fast(plan)" not in bds_body:
        errs.append("[A2] build_dispatch_sends 未调 apply_fail_fast(plan)（fail-fast 真源断）")
    elif "find_ready_steps(plan)" not in bds_body:
        errs.append("[A2] build_dispatch_sends 未调 find_ready_steps(plan)（ready 真源断）")
    else:
        print("[A2] OK  build_dispatch_sends 调 apply_fail_fast + find_ready_steps（与 dispatch_ready_steps 同源）")

    # A3 返 (sends, dispatched_steps) 元组
    try:
        plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                 "status": "pending", "depends_on": [], "task_id": None}]
        sends, dispatched = build_dispatch_sends("g1", "c1", plan)
        if not isinstance(sends, list) or not all(isinstance(s, Send) for s in sends):
            errs.append(f"[A3] sends 应为 list[Send]，实际 {type(sends)}")
        elif not isinstance(dispatched, list):
            errs.append(f"[A3] dispatched_steps 应为 list，实际 {type(dispatched)}")
        else:
            print(f"[A3] OK  build_dispatch_sends 返 (sends={len(sends)} Send, dispatched={len(dispatched)} step) 元组")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] build_dispatch_sends 调用异常：{type(e).__name__}: {e}")

    # ── B. Send fan-out 语义 ──────────────────────────────────
    # B4 Send 目标 = agent_<agent_id>
    try:
        if agent_node_target("w1") != "agent_w1":
            errs.append(f"[B4] agent_node_target('w1')={agent_node_target('w1')!r}，应 'agent_w1'")
        elif AGENT_NODE_PREFIX != "agent_":
            errs.append(f"[B4] AGENT_NODE_PREFIX={AGENT_NODE_PREFIX!r}，应 'agent_'")
        else:
            # 实际 Send 目标
            tgt = sends[0].node if sends else None
            if tgt != "agent_w1":
                errs.append(f"[B4] Send 目标={tgt!r}，应 'agent_w1'（agent_node_target 单一真源）")
            else:
                print("[B4] OK  Send 目标 = agent_<agent_id>（agent_node_target 单一真源，与 group_graph/worker 命名一致）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] Send 目标检查异常：{type(e).__name__}: {e}")

    # B5 Send payload 含 instruction + coordinator_id + step identity
    try:
        payload = sends[0].arg if sends else None
        if not isinstance(payload, dict):
            errs.append("[B5] Send payload 应为 dict")
        else:
            checks = {
                "incoming_message": payload.get("incoming_message") == "do A",
                "incoming_sender": payload.get("incoming_sender") == "c1",
                "incoming_kind": payload.get("incoming_kind") == "coordinator_task",
                "incoming_data.step": (payload.get("incoming_data") or {}).get("step") == 1,
                "incoming_data.task_id": bool((payload.get("incoming_data") or {}).get("task_id")),
                "coordinator_id": payload.get("coordinator_id") == "c1",
                "current_speaker": payload.get("current_speaker") == "w1",
            }
            missing = [k for k, v in checks.items() if not v]
            if missing:
                errs.append(f"[B5] Send payload 缺/错 {missing}（payload={payload}）")
            else:
                print("[B5] OK  Send payload 含 instruction/coordinator_id/step identity（agent 节点据此建 brain context）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] Send payload 检查异常：{type(e).__name__}: {e}")

    # B6 真 StateGraph 跑通：node_dispatch_next_group 并行 fan-out
    try:
        async def _run_b6():
            from langgraph.graph import END, START, StateGraph
            from langgraph.checkpoint.memory import MemorySaver
            from langgraph.types import Command
            from engine.state import GroupState
            from langchain_core.messages import AIMessage

            got: list[tuple] = []

            async def agent_w1(state):
                got.append(("w1", state.get("incoming_message"),
                           state.get("incoming_sender"),
                           (state.get("incoming_data") or {}).get("step")))
                return {"messages": [AIMessage(content="w1 done", id="w1r", name="W1")]}
            async def agent_w2(state):
                got.append(("w2", state.get("incoming_message"),
                           state.get("incoming_sender"),
                           (state.get("incoming_data") or {}).get("step")))
                return {"messages": [AIMessage(content="w2 done", id="w2r", name="W2")]}

            g: StateGraph = StateGraph(GroupState)
            g.add_node("dispatch_next_group", node_dispatch_next_group)
            g.add_node("agent_w1", agent_w1)
            g.add_node("agent_w2", agent_w2)
            g.add_edge(START, "dispatch_next_group")
            g.add_edge("agent_w1", END)
            g.add_edge("agent_w2", END)
            app = g.compile(checkpointer=MemorySaver())
            plan = [
                {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                 "status": "pending", "depends_on": [], "task_id": None},
                {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                 "status": "pending", "depends_on": [], "task_id": None},
            ]
            r = await app.ainvoke({"group_id": "g1", "coordinator_id": "c1",
                                   "dispatch_plan": plan, "turn_count": 0},
                                  config={"configurable": {"thread_id": "vh36-b6"}})
            return got, r

        got, r = asyncio.run(_run_b6())
        targets = sorted(g[0] for g in got)
        if targets != ["w1", "w2"]:
            errs.append(f"[B6] 并行 fan-out 应触达 w1+w2，实际 {targets}")
        elif not all(g[1] == ("do A" if g[0] == "w1" else "do B") for g in got):
            errs.append(f"[B6] agent 收到的 instruction 错（got={got}）")
        elif not all(g[2] == "c1" for g in got):
            errs.append(f"[B6] incoming_sender 应全 c1（got={got}）")
        elif not all(s.get("status") == "dispatched" for s in r.get("dispatch_plan", [])):
            errs.append(f"[B6] dispatched step 状态非 dispatched（{[(s['step'],s['status']) for s in r.get('dispatch_plan',[])]}）")
        elif not all(s.get("task_id") for s in r.get("dispatch_plan", [])):
            errs.append("[B6] dispatched step task_id 未设（handle_reply task_id 匹配会破）")
        else:
            print("[B6] OK  真 StateGraph：node_dispatch_next_group → Command(goto=Send[]) 并行 fan-out w1+w2（各收自己 instruction，step→dispatched+task_id）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B6] 真 StateGraph fan-out 测试异常：{type(e).__name__}: {e}")

    # ── C. DAG fail-fast + ready_steps 保真 ──────────────────
    # C7 fail-fast：step1 failed + step2 deps step1 → step2 级联 failed 不派发
    try:
        plan = [
            {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
             "status": "failed", "depends_on": [], "task_id": "t1", "result": "err"},
            {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
             "status": "pending", "depends_on": [1], "task_id": None},
        ]
        sends, dispatched = build_dispatch_sends("g1", "c1", plan)
        if sends:
            errs.append(f"[C7] fail-fast 应级联不派发 step2，实际 sends={len(sends)}")
        elif plan[1]["status"] != "failed":
            errs.append(f"[C7] step2 应级联 failed，实际 {plan[1]['status']!r}")
        else:
            print("[C7] OK  fail-fast：step1 failed + step2 deps step1 → step2 级联 failed 不派发（apply_fail_fast 同源）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] fail-fast 测试异常：{type(e).__name__}: {e}")

    # C8 ready_steps：step1 completed + step2 deps step1 → step2 ready 派发
    try:
        plan = [
            {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
             "status": "completed", "depends_on": [], "task_id": "t1", "result": "ok"},
            {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
             "status": "pending", "depends_on": [1], "task_id": None},
        ]
        sends, dispatched = build_dispatch_sends("g1", "c1", plan)
        if len(sends) != 1 or sends[0].node != "agent_w2":
            errs.append(f"[C8] ready 应只派 step2 到 agent_w2，实际 sends={[s.node for s in sends]}")
        else:
            print("[C8] OK  ready_steps：step1 completed + step2 deps step1 → step2 ready 被派发（find_ready_steps 同源）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] ready_steps 测试异常：{type(e).__name__}: {e}")

    # C8b ready_steps：step1 pending + step2 deps step1 → step2 不 ready 不派发
    try:
        plan = [
            {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
             "status": "pending", "depends_on": [], "task_id": None},
            {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
             "status": "pending", "depends_on": [1], "task_id": None},
        ]
        sends, dispatched = build_dispatch_sends("g1", "c1", plan)
        if len(sends) != 1 or sends[0].node != "agent_w1":
            errs.append(f"[C8b] 只 step1（deps=[]）应派，step2 deps 未满足不派，实际 sends={[s.node for s in sends]}")
        else:
            print("[C8b] OK  ready_steps：step1 pending+deps=[] 派 / step2 deps step1 未满足不派（deps gate 保真）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8b] ready_steps deps gate 测试异常：{type(e).__name__}: {e}")

    # C9 step mutation：dispatched step status + task_id 一致
    try:
        plan = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                 "status": "pending", "depends_on": [], "task_id": None}]
        sends, dispatched = build_dispatch_sends("g1", "c1", plan)
        if dispatched[0]["status"] != "dispatched":
            errs.append(f"[C9] dispatched step status 应 dispatched，实际 {dispatched[0]['status']!r}")
        elif not dispatched[0].get("task_id"):
            errs.append("[C9] dispatched step task_id 未设（handle_reply task_id 匹配会破）")
        elif not dispatched[0]["task_id"].startswith("task_"):
            errs.append(f"[C9] task_id 应 task_ 前缀，实际 {dispatched[0]['task_id']!r}")
        else:
            print("[C9] OK  step mutation：pending→dispatched + task_id 设（与 _dispatch_one 同款，handle_reply 匹配不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C9] step mutation 测试异常：{type(e).__name__}: {e}")

    # ── D. 路由保真 ──────────────────────────────────────────
    # D10 无 dispatchable + all done → summarize
    try:
        if route_after_dispatch_next({"action_taken": "summarize"}) != "summarize":
            errs.append("[D10] route_after_dispatch_next(summarize) 应返 'summarize'")
        else:
            print("[D10] OK  无 dispatchable + all done → action_taken=summarize → route → summarize")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] 路由检查异常：{type(e).__name__}: {e}")

    # D11 无 dispatchable + not all done → END
    try:
        from langgraph.graph import END as _END
        if route_after_dispatch_next({}) != _END:
            errs.append(f"[D11] route_after_dispatch_next({{}}) 应返 END，实际 {route_after_dispatch_next({})!r}")
        else:
            print("[D11] OK  无 dispatchable + not all done → END（in-flight steps running）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] 路由检查异常：{type(e).__name__}: {e}")

    # D12 有 dispatched → Command(goto=sends)（静态：node_dispatch_next_group 体含 Command(goto=sends)）
    ndng_body = _fn_body(coord_src, "node_dispatch_next_group")
    if "Command(goto=sends" not in ndng_body and "goto=sends" not in ndng_body:
        errs.append("[D12] node_dispatch_next_group 未返 Command(goto=sends)（fan-out 路由破）")
    else:
        print("[D12] OK  有 dispatched → Command(goto=sends)（LangGraph 跟 Send 走，route_after 不被咨询）")

    # ── E. 向后兼容 ──────────────────────────────────────────
    # E13 dispatch_ready_steps 仍在
    if not callable(dispatch_ready_steps):
        errs.append("[E13] dispatch_ready_steps 不可调用（resident 图应保留——m12/mt15/vh35 不破）")
    else:
        print("[E13] OK  dispatch_ready_steps 保留（resident coordinator 图仍用，m12/mt15/mt16/vh10/vh35 不破）")

    # E14 _dispatch_one / apply_fail_fast / find_ready_steps 仍在
    for fn in (_dispatch_one, apply_fail_fast, find_ready_steps):
        if not callable(fn):
            errs.append(f"[E14] {fn} 不可调用（resident 派发链断）")
    if not any(e.startswith("[E14]") for e in errs):
        print("[E14] OK  _dispatch_one/apply_fail_fast/find_ready_steps 全保留（resident 派发链不破）")

    # E15 node_dispatch_next（resident）+ node_dispatch_next_group（群图 twin）共存
    if not inspect.iscoroutinefunction(node_dispatch_next):
        errs.append("[E15] node_dispatch_next（resident）应是 async")
    elif not inspect.iscoroutinefunction(node_dispatch_next_group):
        errs.append("[E15] node_dispatch_next_group（群图 twin）应是 async")
    else:
        print("[E15] OK  node_dispatch_next（resident）+ node_dispatch_next_group（群图 twin）共存（additive）")

    # E16 build_coordinator_subnodes 含 dispatch_next_group twin
    specs = build_coordinator_subnodes(coordinator_id="c1")
    if "dispatch_next_group" not in specs or "route_after_dispatch_next" not in specs:
        errs.append("[E16] build_coordinator_subnodes 缺 dispatch_next_group/route_after_dispatch_next twin")
    else:
        print("[E16] OK  build_coordinator_subnodes 含 dispatch_next_group + route_after_dispatch_next twin（后续接线任务可切换）")

    return errs


def main() -> int:
    print("=== VH36 回归：dispatch_next 节点 Send fan-out（去中心化 handoff 迁移·派发层）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "dispatch_next 节点 Send fan-out 锁定：\n"
        "  · A build_dispatch_sends 工厂 + 调 apply_fail_fast/find_ready_steps（同源 DAG 真源）+ 返 (sends, dispatched)；\n"
        "  · B Send 目标 agent_<id>（agent_node_target 单一真源）+ payload 含 instruction/coordinator_id/step identity + 真 StateGraph 并行 fan-out；\n"
        "  · C DAG fail-fast + ready_steps 保真（step1 failed 级联 step2 / step1 completed 释 step2 / deps gate）+ step mutation 一致；\n"
        "  · D 路由保真（summarize/END/Command(goto=sends) 三分支）；\n"
        "  · E 向后兼容（dispatch_ready_steps/_dispatch_one/apply_fail_fast/find_ready_steps/node_dispatch_next 全保留，dispatch_next_group 是 additive twin）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
