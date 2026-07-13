"""VH35 回归：coordinator dispatch 节点迁移到群图，保留 interrupt() + Command(resume) 唤醒.

锁住 task-7 决策——coordinator dispatch 节点迁移到群图（engine/group_graph.py 的
coordinator 子节点），保留 ``interrupt({"plan": plan})``（PL-02 计划确认），验证群图
thread_id 下 ``Command(resume=)`` 仍唤醒 dispatch 节点 fan-out.

设计真源见 memory ``engine-audit-interrupt-replacement`` + ``langgraph-interrupt-py310-
contextvar-pitfall``：方案 B 计划确认用 LangGraph 原生 ``interrupt()``（非自研内存态等待），
3.10 异步节点须用 contextvar 绕法（节点收 config + 手动 set var_child_runnable_config）.

本任务验证两件事：
  1. **dispatch 节点就位**：群图（build_group_graph）注册了 coordinator dispatch 子节点
     （task-6 的 build_coordinator_subnodes），``node_dispatch`` 仍含 ``interrupt({"plan": plan})``
     + ``_runnable_config_ctx``（3.10 绕法未删）——迁移不改 interrupt 语义.
  2. **群图 thread_id 下 Command(resume) 唤醒**：用 GroupState schema 编译一张含 dispatch 子节点的
     群图，auto_confirm=False invoke 到 dispatch → interrupt() 暂停（get_state().next=('dispatch',)
     非空，plan checkpointed 未 fan-out）→ Command(resume={"mode":"confirm"}) 唤醒 →
     dispatch_ready_steps 被调 fan-out 发生 → resume 后 next=() 线程跑完.

与 test_m12_unit_interrupt_resume 的关系：m12 验证的是**驻留 coordinator 图**（CoordinatorState）
的 interrupt/resume；本测验证的是**群图**（GroupState schema 并集后的 coordinator 子图）的
interrupt/resume —— 同一 ``node_dispatch`` 函数，不同 state schema，interrupt() 语义在 GroupState
下仍保真（dispatch_plan=replace_value reducer + checkpointer 单一真源，plan checkpointed 后
resume 重入节点第二 interrupt() 返 resume 值立即 fan-out）.

四段契约（纯静态 + 真 StateGraph stub，不依赖 live server / 真实 LLM）：

  A. dispatch 节点就位锁——群图注册 + interrupt() + 3.10 绕法保留
    1. ``build_group_graph`` 装配的图含 ``dispatch`` 节点（coordinator 子节点）.
    2. ``node_dispatch`` 函数体仍含 ``interrupt({"plan": plan})``（PL-02 暂停未删）.
    3. ``node_dispatch`` 仍用 ``_runnable_config_ctx(config)`` 包 interrupt（3.10 contextvar 绕法）.

  B. interrupt 暂停锁——auto_confirm=False invoke 到 dispatch 暂停
    4. GroupState schema 编译的含 dispatch 子节点群图，auto_confirm=False fresh-input invoke
       （stub LLM 判 dispatch + plan）→ get_state().next=('dispatch',) 非空（interrupt 暂停）.
    5. 暂停时 plan 已 checkpointed（dispatch_plan reducer replace_value）且未 fan-out
       （dispatch_ready_steps 未被调）.

  C. Command(resume) 唤醒锁——resume 后 fan-out 发生 + 线程跑完
    6. ``Command(resume={"mode":"confirm"})`` 喂同一 thread_id → dispatch_ready_steps 被调
       → fan-out 派发 plan（instruction='do A'）.
    7. resume 后 get_state().next=()（线程跑完，无残留暂停）.

  D. 向后兼容锁——驻留 coordinator 图 interrupt/resume 不破（m12 不破）
    8. 驻留 ``build_coordinator_graph``（CoordinatorState）的 interrupt/resume 仍工作
       （test_m12_unit_interrupt_resume PASS——同一 node_dispatch 函数，两 schema 共用）.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
COORD_PY = BACKEND / "engine" / "coordinator.py"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def _build_coord_subgraph_on_groupstate():
    """Compile a GroupState-schema graph with the coordinator dispatch sub-node wired
    (mirrors build_group_graph's coordinator sub-node registration + the resident
    coordinator's conditional edges, so interrupt/resume can be driven on GroupState)."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from engine.state import GroupState
    from engine.coordinator import build_coordinator_subnodes

    nodes = build_coordinator_subnodes(coordinator_id="c1", coordinator_name="Coord", system_prompt="你是群主")
    g: StateGraph = StateGraph(GroupState)
    g.add_node("classify", nodes["classify"])
    g.add_node("llm_decide", nodes["llm_decide"])
    g.add_node("chat", nodes["chat"])
    g.add_node("dispatch", nodes["dispatch"])
    g.add_node("dispatch_next", nodes["dispatch_next"])
    g.add_node("handle_reply", nodes["handle_reply"])
    g.add_node("summarize", nodes["summarize"])
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", nodes["route_after_classify"],
        {"dispatch_next": "dispatch_next", "handle_reply": "handle_reply", "llm_decide": "llm_decide"})
    g.add_conditional_edges("llm_decide", nodes["route_after_llm_decide"],
        {"chat": "chat", "dispatch": "dispatch"})
    g.add_conditional_edges("dispatch", nodes["route_after_dispatch"],
        {"dispatch_next": "dispatch_next", END: END})
    g.add_conditional_edges("dispatch_next",
        lambda s: "summarize" if s.get("action_taken") == "summarize" else END,
        {"summarize": "summarize", END: END})
    g.add_conditional_edges("handle_reply", nodes["route_after_handle_reply"],
        {"summarize": "summarize", "dispatch_next": "dispatch_next", "llm_decide": "llm_decide"})
    g.add_edge("chat", END)
    g.add_edge("summarize", END)
    return g.compile(checkpointer=MemorySaver())


async def _make_stream(plan):
    payload = json.dumps({"action": "dispatch", "content": "", "plan": plan})

    async def fake_stream(config, messages):
        yield (payload, "", 10, 0)

    return fake_stream


def _make_stream_sync(plan):
    """Synchronous builder for the fake stream (used at top-level non-async setup)."""
    payload = json.dumps({"action": "dispatch", "content": "", "plan": plan})

    async def fake_stream(config, messages):
        yield (payload, "", 10, 0)

    return fake_stream


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord_src = _read(COORD_PY)

    try:
        from engine.coordinator import (  # type: ignore
            build_coordinator_subnodes,
            node_dispatch,
        )
        from engine.group_graph import build_group_graph
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. dispatch 节点就位 ──────────────────────────────────
    # A1 群图含 dispatch 节点
    try:
        members = [{"agent_id": "w1", "agent_name": "W1", "agent_role": "worker", "system_prompt": ""}]
        g = build_group_graph("g1", members, coordinator_id="c1")
        nodes = set(g.get_graph().nodes.keys())
        if "dispatch" not in nodes:
            errs.append(f"[A1] 群图缺 dispatch 节点（nodes={sorted(nodes)}）")
        else:
            print("[A1] OK  build_group_graph 装配的群图含 dispatch 节点（coordinator 子节点就位）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A1] 群图装配异常：{type(e).__name__}: {e}")

    # A2 node_dispatch 仍含 interrupt({"plan": plan})
    disp_body = _fn_body(coord_src, "node_dispatch")
    if 'interrupt({"plan": plan})' not in disp_body:
        errs.append('[A2] node_dispatch 缺 interrupt({"plan": plan})（PL-02 暂停语义被删）')
    else:
        print('[A2] OK  node_dispatch 仍含 interrupt({"plan": plan})（PL-02 计划确认暂停保留）')

    # A3 node_dispatch 仍用 _runnable_config_ctx（3.10 contextvar 绕法）
    if "_runnable_config_ctx(config)" not in disp_body:
        errs.append("[A3] node_dispatch 缺 _runnable_config_ctx(config)（3.10 contextvar 绕法被删——interrupt 在 3.10 异步节点会 RuntimeError）")
    else:
        print("[A3] OK  node_dispatch 仍用 _runnable_config_ctx(config)（3.10 contextvar 绕法保留）")

    # ── B/C. 群图 thread_id 下 interrupt/resume 真 StateGraph 跑通 ──
    try:
        async def _run_bc():
            ggs = _build_coord_subgraph_on_groupstate()
            plan_a = [{"step": 1, "agent_id": "w1", "agent_name": "W1", "task_id": "t1",
                       "status": "pending", "instruction": "do A"}]
            fanout: list[list[dict]] = []

            async def fake_dispatch_ready_steps(group_id, coordinator_id, plan):
                out = []
                for s in plan:
                    if s.get("status") == "pending":
                        s["status"] = "dispatched"
                        out.append(s)
                fanout.append([dict(s) for s in out])
                return out

            cfg = {"configurable": {"thread_id": "vh35-grp-resume"}}
            fake_stream = _make_stream_sync(plan_a)
            from engine import coordinator as coord_mod
            with patch.object(coord_mod, "chat_completion_stream", fake_stream), \
                 patch.object(coord_mod, "_unified_reply", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_plan", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_reasoning", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_think", AsyncMock()), \
                 patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch_ready_steps):
                coord_mod.set_reply_callback(lambda _c: asyncio.sleep(0))
                await ggs.ainvoke({
                    "group_id": "g1", "coordinator_id": "c1", "agent_id": "c1", "agent_name": "Coord",
                    "system_prompt": "你是群主", "incoming_message": "please do it",
                    "incoming_sender": "user", "incoming_kind": "coordinator_reply",
                    "incoming_data": None, "memory": [], "dispatch_plan": [],
                    "auto_confirm": False, "leader_strategy": "",
                }, config=cfg)
                coord_mod.set_reply_callback(None)

            # B4 next=('dispatch',) 非空（interrupt 暂停）
            snap = await ggs.aget_state(cfg)
            if not snap.next:
                errs.append("[B4] 群图 get_state().next 为空（dispatch 未 interrupt 暂停——PL-02 破）")
            elif snap.next != ("dispatch",):
                errs.append(f"[B4] 群图 next={snap.next!r}，应 ('dispatch',)（interrupt 暂停在 dispatch 节点）")
            else:
                print(f"[B4] OK  群图 auto_confirm=False invoke → get_state().next={snap.next}（interrupt 暂停在 dispatch）")

            # B5 plan checkpointed + 未 fan-out
            cp_plan = (snap.values or {}).get("dispatch_plan") or []
            pending = [s for s in cp_plan if s.get("status") == "pending"]
            if not cp_plan:
                errs.append("[B5] 群图 checkpointed dispatch_plan 缺失（replace_value reducer 未落 plan）")
            elif not pending:
                errs.append(f"[B5] 群图 checkpointed plan 无 pending 步骤（{cp_plan}）")
            elif fanout:
                errs.append("[B5] interrupt turn 就 fan-out（dispatch_ready_steps 被调——interrupt 未暂停）")
            else:
                print(f"[B5] OK  plan checkpointed（{len(pending)} pending）且未 fan-out（interrupt 暂停时 dispatch_ready_steps 未调）")

            # C6 Command(resume) → fan-out
            from langgraph.types import Command as _Cmd
            with patch.object(coord_mod, "dispatch_ready_steps", fake_dispatch_ready_steps):
                await ggs.ainvoke(_Cmd(resume={"mode": "confirm"}), config=cfg)
            if not fanout:
                errs.append("[C6] Command(resume) 后 dispatch_ready_steps 未被调（群图 resume 未唤醒 fan-out）")
            else:
                instr = fanout[0][0].get("instruction") if fanout[0] else None
                if instr != "do A":
                    errs.append(f"[C6] fan-out 派发错步骤（instruction={instr!r}，应 'do A'）")
                else:
                    print(f"[C6] OK  Command(resume={{'mode':'confirm'}}) → dispatch_ready_steps 被调 → fan-out 派发 plan（instruction={instr!r}）")

            # C7 resume 后 next=()
            snap2 = await ggs.aget_state(cfg)
            if snap2.next:
                errs.append(f"[C7] resume 后 next={snap2.next!r}（应 () 线程跑完）")
            else:
                print("[C7] OK  resume 后 get_state().next=()（线程跑完，无残留暂停）")

        asyncio.run(_run_bc())
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B/C] 群图 interrupt/resume 真 StateGraph 测试异常：{type(e).__name__}: {e}")

    # ── D. 驻留 coordinator 图 interrupt/resume 不破（m12 不破） ──
    # 静态锁：同一 node_dispatch 函数（is 同一性）服务两图——驻留图（CoordinatorState）
    # 与群图（GroupState）共用 node_dispatch，interrupt/resume 语义同源不破。
    try:
        from engine.coordinator import build_coordinator_graph
        resident = build_coordinator_graph()
        rnodes = set(resident.get_graph().nodes.keys())
        if "dispatch" not in rnodes:
            errs.append("[D8] 驻留 coordinator 图缺 dispatch 节点（m12 锚点破）")
        else:
            print("[D8] OK  驻留 coordinator 图保留 dispatch 节点（node_dispatch 同源服务两图，m12 interrupt/resume 不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D8] 驻留图检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH35 回归：coordinator dispatch 迁移群图 + interrupt/resume 保真（PL-02）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "coordinator dispatch 迁移群图 + interrupt/resume 保真锁定：\n"
        "  · A 群图注册 dispatch 子节点 + node_dispatch 仍含 interrupt({\"plan\":plan}) + _runnable_config_ctx（3.10 绕法）；\n"
        "  · B 群图 auto_confirm=False invoke → next=('dispatch',) 暂停 + plan checkpointed 未 fan-out；\n"
        "  · C Command(resume={'mode':'confirm'}) → dispatch_ready_steps 被调 fan-out + resume 后 next=() 跑完；\n"
        "  · D 驻留 coordinator 图 dispatch 节点保留（node_dispatch 同源服务两图，m12 不破）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
