"""VH54 回归：group 路径派工事件断层修复（node_dispatch_next_group emit_task_dispatched）.

锁住 task-19 前置 BUG 修复——群图派工路径 ``node_dispatch_next_group`` 之前只
``emit_coordinator_plan``（重宣布 plan），**未 ``emit_task_dispatched``**
（``build_dispatch_sends`` 只产 ``Send`` 不 emit 派工事件）。后果：MT-14/MT-15
e2e 探针抓到 ``task_dispatch=0``（worker execute 路径的 ``emit_task_completed``
没有匹配的 ``task_dispatch`` 配对）→ 串行链路断在第 2 步、两步不全完成。

修复：``node_dispatch_next_group`` 在 ``build_dispatch_sends`` 返回后，对每个
``dispatched`` step 调一次 ``emit_task_dispatched``（mirror resident
``_dispatch_one``:145，单一真源——同样的 WS 事件形状 ``task_dispatch``，同样的
``task_id``/``step``/``agent_id``/``agent_name``/``instruction`` 字段）。``task_id``
是 ``build_dispatch_sends`` 已 mint 的（存 step.task_id），与后续
``emit_task_completed``（worker execute 路径，registry._run_worker_task:418）的
``task_id`` 同源——e2e 串行顺序断言（步骤1 task_complete 早于步骤2 task_dispatch）
靠这个 id 配对定位时序。

本任务只是 task-19（live e2e 验证）的前置 BUG 修复 + 契约锁，不含 live 验证
（live 由 task-19 自己跑 va1/mt14/mt15/m12_plan_confirm）。纯静态 + 真 StateGraph
mock，不依赖 live server / 真实 LLM，与 vh36 同款风格（vh36 锁 Send fan-out 语义，
vh54 锁派工事件 emit 不再断层）。

六段契约：

  A. emit_task_dispatched 已 import 到 coordinator.py
    1. ``coordinator.py`` 顶部 ``from events import (...)`` 含 ``emit_task_dispatched``。

  B. node_dispatch_next_group 函数体含 emit_task_dispatched 调用
    2. ``node_dispatch_next_group`` 函数体（静态读源码）含 ``emit_task_dispatched(``。
    3. 该调用在 fan-out 成功分支（``if not dispatched`` 的 else / ``Command(goto=sends)``
       之前），不在 no-dispatchable 的 summarize/END 早退分支（那俩分支无派工，不该 emit）。
    4. 对每个 dispatched step emit 一次（循环 ``for step in dispatched`` 或等价），不是
       只 emit 第一个。

  C. emit 字段镜像 _dispatch_one（单一真源）
    5. emit 调用传 6 个参数（group_id, task_id, step, agent_id, agent_name, instruction），
       与 ``_dispatch_one:145`` ``emit_task_dispatched(group_id, pushed["id"], step_num,
       agent_id, agent_name, instruction)`` 同款。
    6. ``task_id`` 取 step 自带的（``step.get("task_id")``），与
       ``build_dispatch_sends`` mint + ``emit_task_completed`` 同源（非另 mint 新 id）。

  D. 真 StateGraph 跑通：fan-out 时 emit task_dispatch（mock bus 抓事件）
    7. 真 StateGraph（镜像 vh36-B6 拓扑 dispatch_next_group→agent_*）跑一次双 ready step
       plan → 抓到 2 条 ``task_dispatch`` 事件（每个 ready step 一条），task_id 与
       step.task_id 相等，step/agent_id/agent_name/instruction 字段对齐。
    8. no-dispatchable + all-done 分支（``summarize`` goto）不 emit task_dispatch（无派工）。
    9. no-dispatchable + not-all-done 分支（END goto）不 emit task_dispatch（in-flight）。

  E. 向后兼容：resident 路径不破
   10. ``_dispatch_one`` 仍 ``emit_task_dispatched``（resident 派发链单一真源不破，vh11 锁）。
   11. ``emit_task_dispatched`` helper 签名未变（vh12 锁的 13 emit_* 之一不变）。

  F. 错误隔离：单个 emit 失败不阻断其余派工 emit
   12. emit 包 try/except（best-effort，单个 step emit 失败不 raise 阻断 fan-out）。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
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
    coord_src = _read(COORD_PY)

    try:
        from engine.coordinator import (  # type: ignore
            node_dispatch_next_group,
            route_after_dispatch_next,
        )
        from engine.dispatcher import _dispatch_one, build_dispatch_sends  # type: ignore
        from events import emit_task_dispatched  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    # ── A. emit_task_dispatched 已 import ──────────────────────
    # A1 coordinator.py from events import (...) 含 emit_task_dispatched
    imp_match = re.search(r"from events import \(([^)]*)\)", coord_src, re.S)
    imp_block = imp_match.group(1) if imp_match else ""
    if "emit_task_dispatched" not in imp_block:
        errs.append("[A1] coordinator.py 顶部 from events import 未含 emit_task_dispatched")
    else:
        print("[A1] OK  coordinator.py 已 import emit_task_dispatched（群图派工 emit 真源到位）")

    # ── B. node_dispatch_next_group 函数体含 emit_task_dispatched 调用 ──
    ndng_body = _fn_body(coord_src, "node_dispatch_next_group")
    # B2 函数体含 emit_task_dispatched( 调用
    if "emit_task_dispatched(" not in ndng_body:
        errs.append("[B2] node_dispatch_next_group 函数体未调 emit_task_dispatched（派工事件仍断层）")
    else:
        print("[B2] OK  node_dispatch_next_group 函数体含 emit_task_dispatched 调用（派工事件不再断层）")

    # B3 调用在 fan-out 成功分支（Command(goto=sends) 之前），不在 no-dispatchable 早退分支
    # 函数体含 docstring（prose 里也会提 Command(goto=sends) / emit_task_dispatched），
    # 故用 LAST 出现的 ``Command(goto=sends`` 定位真实 return 语句（docstring 的 prose 提及
    # 在前，code 的 return 在后），用 FIRST 带括号的 ``emit_task_dispatched(``` 定位真实
    # 调用（docstring 的 ``emit_task_dispatched`` 无括号，匹配不到带括号模式）。
    fanout_idx = ndng_body.rfind("Command(goto=sends")
    first_emit_idx = ndng_body.find("emit_task_dispatched(")
    if fanout_idx < 0:
        errs.append("[B3] node_dispatch_next_group 未找到 Command(goto=sends)（fan-out 路由破，vh36-D12 也锁）")
    elif first_emit_idx < 0:
        # emit 缺失已由 B2 报，不重复报
        pass
    elif first_emit_idx > fanout_idx:
        errs.append(
            f"[B3] emit_task_dispatchent 出现在 Command(goto=sends) 之后（应在 fan-out 成功分支内、goto 之前 emit）"
        )
    else:
        print("[B3] OK  emit_task_dispatched 在 fan-out 成功分支（goto=sends 之前），非 no-dispatchable 早退分支")

    # B4 对每个 dispatched step emit 一次（循环 for step in dispatched 或等价）
    if "for step in dispatched" not in ndng_body and "for s in dispatched" not in ndng_body:
        errs.append(
            "[B4] node_dispatch_next_group 未对每个 dispatched step 循环 emit（只 emit 第一个会漏多 ready step）"
        )
    else:
        print("[B4] OK  对每个 dispatched step 循环 emit_task_dispatched（多 ready step 全覆盖）")

    # ── C. emit 字段镜像 _dispatch_one（单一真源）─────────────
    # C5 emit 调用传 6 参数（group_id, task_id, step, agent_id, agent_name, instruction）
    # 静态：截 emit_task_dispatched(...) 调用块看参数。调用跨多行且含嵌套括号
    # （``step.get("task_id") or ""``），``[^)]*`` 会在第一个嵌套 ``)`` 截断 → 取调用
    # 起点到之后 400 字符窗口（足够覆盖 6 个多行参数），在该窗口里查每个参数名。
    emit_call_start = ndng_body.find("emit_task_dispatched(")
    if emit_call_start < 0:
        errs.append("[C5] 未截到 emit_task_dispatched(...) 调用块（C5-C6 跳过）")
        emit_args = ""
    else:
        emit_args = ndng_body[emit_call_start: emit_call_start + 400]
    if emit_args:
        # 6 个位置参数（与 _dispatch_one 同款），允许 step.get(...) 形式
        expected_substrings = [
            "group_id",
            "task_id",
            "step",
            "agent_id",
            "agent_name",
            "instruction",
        ]
        missing = [s for s in expected_substrings if s not in emit_args]
        if missing:
            errs.append(f"[C5] emit_task_dispatched 调用缺参数 {missing}（应 6 参数镜像 _dispatch_one:145）")
        else:
            print("[C5] OK  emit_task_dispatched 传 6 参数（group_id/task_id/step/agent_id/agent_name/instruction），镜像 _dispatch_one")

    # C6 task_id 取 step 自带（step.get("task_id")），与 build_dispatch_sends mint 同源
    if emit_args:
        if "task_id" not in emit_args:
            errs.append("[C6] emit 调用未取 task_id（task_complete 配对会破）")
        elif "step.get(\"task_id\")" not in ndng_body and "step.get('task_id')" not in ndng_body:
            errs.append(
                "[C6] emit 的 task_id 未取 step.get('task_id')（与 build_dispatch_sends mint 的 step.task_id 不同源 → 与 emit_task_completed 的 task_id 不配对）"
            )
        else:
            print("[C6] OK  task_id 取 step.get('task_id')（build_dispatch_sends mint 同源，与 emit_task_completed task_id 配对）")

    # ── D. 真 StateGraph 跑通：fan-out 时 emit task_dispatch（mock bus 抓事件）──
    # D7 双 ready step → 抓 2 条 task_dispatch，task_id 与 step.task_id 相等
    try:
        async def _run_d7():
            from langgraph.graph import END, START, StateGraph
            from langgraph.checkpoint.memory import MemorySaver
            from langgraph.types import Command  # noqa: F401
            from engine.state import GroupState
            from langchain_core.messages import AIMessage

            emitted: list[tuple] = []

            async def fake_emit_task_dispatched(
                group_id, task_id, step, agent_id, agent_name, instruction,
            ):
                emitted.append((group_id, task_id, step, agent_id, agent_name, instruction))

            async def agent_w1(state):
                return {"messages": [AIMessage(content="w1", id="w1r", name="W1")]}
            async def agent_w2(state):
                return {"messages": [AIMessage(content="w2", id="w2r", name="W2")]}

            with patch(
                "engine.coordinator.emit_task_dispatched",
                fake_emit_task_dispatched,
            ):
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
                r = await app.ainvoke(
                    {"group_id": "g1", "coordinator_id": "c1",
                     "dispatch_plan": plan, "turn_count": 0},
                    config={"configurable": {"thread_id": "vh54-d7"}},
                )
            return emitted, r

        emitted, r = asyncio.run(_run_d7())
        # emitted 应 2 条（w1+w2 各一）
        if len(emitted) != 2:
            errs.append(f"[D7] fan-out 应 emit 2 条 task_dispatch（w1+w2），实际 {len(emitted)}：{emitted}")
        else:
            # task_id 与 step.task_id 相等（build_dispatch_sends mint 的）
            step_ids = {s.get("task_id") for s in r.get("dispatch_plan", []) if s.get("task_id")}
            emit_ids = {e[1] for e in emitted}
            if emit_ids != step_ids:
                errs.append(f"[D7] emit 的 task_id {emit_ids} 与 step.task_id {step_ids} 不等（配对破）")
            elif not all(e[0] == "g1" for e in emitted):
                errs.append(f"[D7] emit 的 group_id 应全 g1：{emitted}")
            elif not all(e[3] in ("w1", "w2") for e in emitted):
                errs.append(f"[D7] emit 的 agent_id 应 w1/w2：{emitted}")
            elif not all(e[4] in ("W1", "W2") for e in emitted):
                errs.append(f"[D7] emit 的 agent_name 应 W1/W2：{emitted}")
            elif not all(e[5] in ("do A", "do B") for e in emitted):
                errs.append(f"[D7] emit 的 instruction 应 do A/do B：{emitted}")
            else:
                print("[D7] OK  fan-out 双 ready step → emit 2 条 task_dispatch（task_id 与 step.task_id 相等，字段对齐）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D7] 真 StateGraph emit 测试异常：{type(e).__name__}: {e}")

    # D8 no-dispatchable + all-done（summarize）分支不 emit task_dispatch
    try:
        async def _run_d8():
            from langgraph.graph import END, START, StateGraph
            from langgraph.checkpoint.memory import MemorySaver
            from engine.state import GroupState

            emitted: list = []

            async def fake_emit(*args, **kwargs):
                emitted.append(args)

            with patch(
                "engine.coordinator.emit_task_dispatched", fake_emit
            ):
                g: StateGraph = StateGraph(GroupState)
                g.add_node("dispatch_next_group", node_dispatch_next_group)
                g.add_edge(START, "dispatch_next_group")
                g.add_edge("dispatch_next_group", END)
                app = g.compile(checkpointer=MemorySaver())
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                     "status": "completed", "depends_on": [], "task_id": "t1", "result": "ok"},
                    {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                     "status": "completed", "depends_on": [], "task_id": "t2", "result": "ok"},
                ]
                await app.ainvoke(
                    {"group_id": "g1", "coordinator_id": "c1",
                     "dispatch_plan": plan, "turn_count": 0},
                    config={"configurable": {"thread_id": "vh54-d8"}},
                )
            return emitted

        emitted_d8 = asyncio.run(_run_d8())
        if emitted_d8:
            errs.append(f"[D8] no-dispatchable+all-done 分支不应 emit task_dispatch，实际 emit {len(emitted_d8)} 条")
        else:
            print("[D8] OK  no-dispatchable + all-done（summarize goto）不 emit task_dispatch（无派工）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D8] all-done 分支测试异常：{type(e).__name__}: {e}")

    # D9 no-dispatchable + not-all-done（END）分支不 emit task_dispatch
    try:
        async def _run_d9():
            from langgraph.graph import END, START, StateGraph
            from langgraph.checkpoint.memory import MemorySaver
            from engine.state import GroupState

            emitted: list = []

            async def fake_emit(*args, **kwargs):
                emitted.append(args)

            with patch(
                "engine.coordinator.emit_task_dispatched", fake_emit
            ):
                g: StateGraph = StateGraph(GroupState)
                g.add_node("dispatch_next_group", node_dispatch_next_group)
                g.add_edge(START, "dispatch_next_group")
                g.add_edge("dispatch_next_group", END)
                app = g.compile(checkpointer=MemorySaver())
                # step1 dispatched (in-flight) + step2 pending deps step1 → 无 ready + 非全完成 → END
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "W1", "instruction": "do A",
                     "status": "dispatched", "depends_on": [], "task_id": "t1"},
                    {"step": 2, "agent_id": "w2", "agent_name": "W2", "instruction": "do B",
                     "status": "pending", "depends_on": [1], "task_id": None},
                ]
                await app.ainvoke(
                    {"group_id": "g1", "coordinator_id": "c1",
                     "dispatch_plan": plan, "turn_count": 0},
                    config={"configurable": {"thread_id": "vh54-d9"}},
                )
            return emitted

        emitted_d9 = asyncio.run(_run_d9())
        if emitted_d9:
            errs.append(f"[D9] no-dispatchable+not-all-done 分支不应 emit task_dispatch，实际 emit {len(emitted_d9)} 条")
        else:
            print("[D9] OK  no-dispatchable + not-all-done（END goto）不 emit task_dispatch（in-flight，无新派工）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] not-all-done 分支测试异常：{type(e).__name__}: {e}")

    # ── E. 向后兼容：resident 路径不破 ────────────────────────
    # E10 _dispatch_one 仍 emit_task_dispatched（vh11 锁，resident 单一真源不破）
    disp_py = BACKEND / "engine" / "dispatcher.py"
    disp_src = _read(disp_py)
    do_body = _fn_body(disp_src, "_dispatch_one")
    if "emit_task_dispatched(" not in do_body:
        errs.append("[E10] _dispatch_one 丢失 emit_task_dispatched（resident 派发链断，vh11 回归）")
    else:
        print("[E10] OK  _dispatch_one 仍 emit_task_dispatched（resident 派发链单一真源不破）")

    # E11 emit_task_dispatched helper 签名未变（6 参数，vh12 锁的 13 emit_* 之一）
    try:
        sig = inspect.signature(emit_task_dispatched)
        params = list(sig.parameters)
        expected = ["group_id", "task_id", "step", "agent_id", "agent_name", "instruction"]
        if params != expected:
            errs.append(f"[E11] emit_task_dispatched 签名变 {params}（应 {expected}，vh12 回归）")
        else:
            print("[E11] OK  emit_task_dispatched 签名未变（6 参数，vh12 的 13 emit_* 之一不变）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E11] 签名检查异常：{type(e).__name__}: {e}")

    # ── F. 错误隔离：单个 emit 失败不阻断其余派工 emit ────────
    # F12 emit 包 try/except（best-effort）
    if "except Exception" not in ndng_body or "emit_task_dispatched" not in ndng_body:
        errs.append("[F12] node_dispatch_next_group emit 未包 try/except（单个 emit 失败会 raise 阻断 fan-out）")
    else:
        # 进一步：emit 在 try 块内（except 紧随 emit 调用）
        # 简化断言：函数体里 emit_task_dispatched 调用前有 try
        # 取 emit 调用块的上下文窗口
        ei = ndng_body.find("for step in dispatched")
        if ei < 0:
            ei = ndng_body.find("emit_task_dispatched(")
        window = ndng_body[max(0, ei - 200): ei + 400]
        if "try:" in window and "except Exception" in window:
            print("[F12] OK  emit_task_dispatched 包 try/except（单个 step emit 失败 best-effort 不阻断其余派工 emit + fan-out）")
        else:
            errs.append("[F12] emit_task_dispatched 调用未包在 try/except 内（单个 emit 失败会阻断 fan-out）")

    return errs


def main() -> int:
    print("=== VH54 回归：group 路径派工事件断层修复（node_dispatch_next_group emit_task_dispatched）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "group 路径派工事件断层修复锁定：\n"
        "  · A coordinator.py 已 import emit_task_dispatched；\n"
        "  · B node_dispatch_next_group 函数体含 emit_task_dispatched 调用（在 fan-out 成功分支、goto=sends 前、循环每个 dispatched step）；\n"
        "  · C emit 传 6 参数 + task_id 取 step.task_id（镜像 _dispatch_one:145，与 build_dispatch_sends mint + emit_task_completed 同源配对）；\n"
        "  · D 真 StateGraph：双 ready step → emit 2 条 task_dispatch（task_id 对齐）；no-dispatchable 两分支不 emit；\n"
        "  · E 向后兼容（_dispatch_one 仍 emit、helper 签名未变）；\n"
        "  · F 错误隔离（emit 包 try/except，单个失败不阻断 fan-out）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
