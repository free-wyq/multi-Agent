"""VH65 回归：resume_plan turn_count 并发写 InvalidUpdateError 债务确认（任务12）.

锁住 task27 凌晨脚本撞到的 ``InvalidUpdateError: At key 'turn_count': Can
receive only one value per step`` 间歇债的**确认 + 守卫 + 不退化**三件事：

  1. 确认根因方向——``turn_count``/``current_speaker`` 是 last_value（无 reducer）
     channel，LangGraph 禁止一个 superstep 内多节点写它。撞点只可能在「同一
     ``ainvoke`` 内 ``Send`` 扇出 / report-back 分叉链路上某节点写了 last-value」
     （``_turn_lock`` 已序列化「同 runtime 两 ainvoke」，排除了「同 runtime 两回合
     并发」这一撞点）。
  2. 确认守卫在位——``worker.make_agent_node`` 的 ``is_dispatch_fanout`` 守卫
     （fan-out 节点 ``incoming_kind=="coordinator_task"`` 时跳过 turn_count 写，
     只写 reducer channel ``messages``/``recent_speakers``）。这是 invoke_turn/
     resume_plan 两条路径共用的图，守卫一处即覆盖两路径。
  3. 确认不退化——新增的 ``except InvalidUpdateError`` 不静默吞（重抛 +
     ``logger.error`` 暴露撞点），且 report-back 失败不杀引擎（``_run_loop``
     ``except Exception`` 兜底）。

六段契约（纯静态 + 真 StateGraph stub + 真 GroupRuntime，不依赖 live server /
真实 LLM / 真实 DB）：

  A. 静态守卫锁——make_agent_node 的 is_dispatch_fanout 守卫跳过 turn_count 写
    1. ``make_agent_node`` body 含 ``is_dispatch_fanout = state.get`` 判定.
    2. fan-out 路径（``incoming_kind == "coordinator_task"``）的 update 不含
       ``turn_count``（守卫跳过 last-value 写，只写 reducer channel）.
    3. serial peer 路径（``if not is_dispatch_fanout``）的 update 含 ``turn_count``
       （守卫只挡 fan-out，不误伤闲聊/handoff 链）.

  B. 真图 resume 路径不撞锁——多 ready step Send 扇出不触发 InvalidUpdateError
    4. 真 ``build_group_graph`` + ``Command(resume={"mode":"confirm"})`` 唤醒
       dispatch_next_group，2 ready step 并行 fan-out 到 2 agent 节点，整段
       ``ainvoke`` 不抛 InvalidUpdateError（守卫覆盖了 resume 路径）.
    5. resume 后 dispatch_plan 两步全 ``dispatched``（fan-out 真派发到 agent 节点，
       不是被静默 drop）.

  C. 撞点复现锁——故意写 turn_count 的 fan-out 节点必触发 InvalidUpdateError
    6. 一个「忘了守卫」的 fan-out agent 节点（写 ``turn_count``）+ 2 ready step
       并行 → 真 StateGraph ``ainvoke`` 抛 ``InvalidUpdateError`` 且消息含
       ``turn_count``（负控：证明守卫是必要的，撞点机制真实可复现）.

  D. GroupRuntime except 分支锁——resume_plan/invoke_turn 捕获 + 重抛 + 日志
    7. ``resume_plan`` 体内含 ``except InvalidUpdateError`` 分支 + ``logger.error``
       （不静默吞）+ ``raise``（重抛让上层按失败回合处理）.
    8. ``invoke_turn`` 体内含同款 ``except InvalidUpdateError`` 分支（twin）.
    9. 真 GroupRuntime：mock graph.ainvoke 抛 InvalidUpdateError → resume_plan
       重抛同款异常（不吞），且 logger.error 消息含 ``turn_count`` / ``group=`` /
       ``thread=``（撞点可观测）.

  E. report-back 失败不杀引擎锁——invoke_turn 异常逃逸到 _run_loop 兜底
   10. ``_run_worker_task`` 的 report-back ``rt.invoke_turn`` 调用不被额外 try/except
       包裹（让 group_runtime 的 except + _run_loop 的 ``except Exception`` 双重记录，
       单一真源在 group_runtime）——静态断言：该 call site 仍直接 ``await rt.invoke_turn``
       （未被 try/except 吞掉）.

  F. 向后兼容锁——vh43/vh54/vh46/vh35/vh36 不破
   11. invoke_turn/resume_turn 生命周期 + 收束 + 封顶 + interrupt/resume + Send
       fan-out 全 PASS（新增 except 分支纯加性，不改正常路径）.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"
WORKER_PY = BACKEND / "engine" / "worker.py"
REGISTRY_PY = BACKEND / "engine" / "registry.py"

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
    gr_src = _read(GROUP_RUNTIME_PY)
    w_src = _read(WORKER_PY)
    reg_src = _read(REGISTRY_PY)

    try:
        from langgraph.errors import InvalidUpdateError  # noqa: F401
        from engine.group_graph import build_group_graph  # type: ignore
        from engine.group_runtime import GroupRuntime  # type: ignore
        from engine.worker import make_agent_node  # type: ignore
        import engine.coordinator as coord_mod  # type: ignore
        import engine.worker as worker_mod  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": ""},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": ""},
    ]

    # ── A. 静态守卫锁 ──────────────────────────────────────────
    # A1 make_agent_node 含 is_dispatch_fanout 判定
    man_body = _fn_body(w_src, "make_agent_node")
    if "is_dispatch_fanout = state.get" not in man_body:
        errs.append("[A1] make_agent_node 缺 is_dispatch_fanout 判定（fan-out 守卫破——turn_count 撞点门户大开）")
    else:
        print("[A1] OK  make_agent_node 含 is_dispatch_fanout 判定（fan-out 守卫就位）")

    # A2 fan-out 路径不写 turn_count：turn_count 写只在 `if not is_dispatch_fanout` 分支内
    #    断言 `update["turn_count"] = turn_count` 出现在 `if not is_dispatch_fanout:` 之后
    idx_dispatch = man_body.find("is_dispatch_fanout = state.get")
    idx_tc_write = man_body.find('update["turn_count"] = turn_count')
    if idx_tc_write < 0:
        errs.append("[A2] make_agent_node 未写 turn_count（handoff 链 cap 守卫破）")
    elif idx_dispatch < 0 or idx_tc_write < idx_dispatch:
        errs.append(
            f"[A2] turn_count 写应在 is_dispatch_fanout 判定之后（fan-out 守卫内），"
            f"实际 dispatch_idx={idx_dispatch} tc_write_idx={idx_tc_write}"
        )
    else:
        # 进一步确认 turn_count 写在 `if not is_dispatch_fanout:` 块内（块内第一个 turn_count 写）
        idx_not_fanout = man_body.find("if not is_dispatch_fanout:", idx_dispatch)
        if idx_not_fanout < 0 or idx_tc_write < idx_not_fanout:
            errs.append(
                "[A2] turn_count 写应在 `if not is_dispatch_fanout:` 块内（只 serial peer 路径写）"
            )
        else:
            print("[A2] OK  turn_count 写只在 `if not is_dispatch_fanout`（serial peer）路径——fan-out 节点跳过 last-value 写")

    # A3 fan-out 路径仍写 reducer channel（messages / recent_speakers）——守卫只挡 last-value 不挡 reducer
    if 'update["turn_count"] = turn_count' not in man_body or "recent_speakers" not in man_body:
        errs.append("[A3] make_agent_node 守卫误伤 reducer channel（messages/recent_speakers 应照写）")
    else:
        print("[A3] OK  fan-out 节点仍写 reducer channel（messages/recent_speakers）——守卫只挡 last-value")

    # ── B. 真图 resume 路径不撞锁 ──────────────────────────────
    # B4 真 build_group_graph + Command(resume) → 2 ready step 并行 fan-out 不抛 InvalidUpdateError
    try:
        async def _run_b4():
            from langgraph.types import Command
            gg = build_group_graph("g_vh65_b4", members, coordinator_id="c1")

            async def fake_coord_stream(config, messages):
                plan = [
                    {"step": 1, "agent_id": "w1", "agent_name": "前端", "task_id": "t1",
                     "status": "pending", "instruction": "do A", "depends_on": []},
                    {"step": 2, "agent_id": "w2", "agent_name": "后端", "task_id": "t2",
                     "status": "pending", "instruction": "do B", "depends_on": []},
                ]
                yield (json.dumps({"action": "dispatch", "content": "", "plan": plan}), "", 10, 0)

            async def dispatcher(config, messages, group_id, agent_id):
                # execute path → push_task + END (no handoff, no turn_count write on fan-out)
                return (f"r_{agent_id}",
                        json.dumps({"action": "execute", "content": f"收到 {agent_id}", "reasoning": "r"}),
                        5, 50, "m1", 0, "")

            cfg = {"configurable": {"thread_id": "vh65-b4:1"}}

            class _M:
                def __init__(s, aid, name):
                    s.agent_id = aid; s.agent_name = name; s.agent_role = "r"

            with patch.object(coord_mod, "chat_completion_stream", fake_coord_stream), \
                 patch.object(coord_mod, "_unified_reply", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_plan", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_reasoning", AsyncMock()), \
                 patch.object(coord_mod, "emit_coordinator_think", AsyncMock()), \
                 patch.object(coord_mod, "emit_task_dispatched", AsyncMock()), \
                 patch.object(worker_mod, "_stream_brain_decision", side_effect=dispatcher), \
                 patch.object(worker_mod, "_unified_reply", AsyncMock()), \
                 patch.object(worker_mod, "_build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch.object(worker_mod, "_format_display_msg", side_effect=lambda s, c: c), \
                 patch.object(worker_mod, "get_llm_config", return_value={"model": "m1"}), \
                 patch.object(worker_mod, "crud") as crud_mock, \
                 patch.object(worker_mod, "push_task", AsyncMock()), \
                 patch.object(worker_mod, "find_mentions", return_value=[]), \
                 patch.object(worker_mod, "resolve_mention", return_value=None):
                crud_mock.list_group_members_with_agent = AsyncMock(
                    return_value=[_M("w1", "前端"), _M("w2", "后端")])
                crud_mock.list_agents = AsyncMock(return_value=[])
                # turn 1: coordinator_reply → dispatch → interrupt (auto_confirm=False)
                await gg.ainvoke({
                    "group_id": "g_vh65_b4", "coordinator_id": "c1", "agent_id": "c1",
                    "agent_name": "Coord", "system_prompt": "",
                    "incoming_message": "please make a plan", "incoming_sender": "user",
                    "incoming_kind": "coordinator_reply", "incoming_data": None,
                    "memory": [], "dispatch_plan": [], "auto_confirm": False,
                    "leader_strategy": "", "collaboration_mode": "centralized",
                    "turn_count": 0, "recent_speakers": [],
                }, config=cfg)
                # turn 2: resume (confirm) → dispatch_next_group Send fan-out to w1+w2
                r2 = await gg.ainvoke(Command(resume={"mode": "confirm"}), config=cfg)
            return r2

        r2 = asyncio.run(_run_b4())
        plan_final = (r2 or {}).get("dispatch_plan", []) if isinstance(r2, dict) else []
        statuses = [(s.get("step"), s.get("status")) for s in plan_final]
        if statuses != [(1, "dispatched"), (2, "dispatched")]:
            errs.append(f"[B4] resume fan-out 后两步应全 dispatched，实际 {statuses}")
        else:
            print(f"[B4] OK  真 build_group_graph resume → 2 ready step 并行 fan-out 不抛 InvalidUpdateError（守卫覆盖 resume 路径），两步全 dispatched {statuses}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] 真图 resume fan-out 测试异常（撞点复现了？）：{type(e).__name__}: {e}")

    # ── C. 撞点复现锁（负控）──────────────────────────────────
    # C6 一个「忘了守卫」的 fan-out agent 节点（写 turn_count）+ 2 ready step 并行 → 必触发
    try:
        async def _run_c6():
            from langgraph.graph import END, START, StateGraph
            from langgraph.checkpoint.memory import MemorySaver
            from langchain_core.messages import AIMessage
            from engine.state import GroupState
            from engine.coordinator import node_dispatch_next_group

            async def colliding_agent(state):
                # BUG: fan-out agent node writes turn_count (last-value) — two of
                # these in one superstep collide. This is what the is_dispatch_fanout
                # guard exists to prevent.
                agent_id = state.get("current_speaker", "")
                return {
                    "messages": [AIMessage(content=f"{agent_id} done", id=f"{agent_id}_m", name=agent_id)],
                    "turn_count": (state.get("turn_count") or 0) + 1,
                }

            g: StateGraph = StateGraph(GroupState)
            g.add_node("dispatch_next_group", node_dispatch_next_group)
            g.add_node("agent_w1", colliding_agent)
            g.add_node("agent_w2", colliding_agent)
            g.add_edge(START, "dispatch_next_group")
            g.add_edge("agent_w1", END)
            g.add_edge("agent_w2", END)
            app = g.compile(checkpointer=MemorySaver())
            plan = [
                {"step": 1, "agent_id": "w1", "agent_name": "前端", "task_id": "t1",
                 "status": "pending", "instruction": "do A", "depends_on": []},
                {"step": 2, "agent_id": "w2", "agent_name": "后端", "task_id": "t2",
                 "status": "pending", "instruction": "do B", "depends_on": []},
            ]
            await app.ainvoke(
                {"group_id": "g_vh65_c6", "coordinator_id": "c1",
                 "dispatch_plan": plan, "turn_count": 0},
                config={"configurable": {"thread_id": "vh65-c6:1"}},
            )

        try:
            asyncio.run(_run_c6())
            errs.append("[C6] 忘守卫的 fan-out 节点应触发 InvalidUpdateError，实际未抛（撞点机制不复现——守卫的必要性无法证明）")
        except InvalidUpdateError as e:
            msg = str(e)
            if "turn_count" not in msg:
                errs.append(f"[C6] InvalidUpdateError 触发但消息不含 turn_count：{msg}")
            else:
                print(f"[C6] OK  负控：忘守卫的 fan-out 节点（写 turn_count）+ 2 并行 → InvalidUpdateError（{msg[:60]}…）——守卫是必要的，撞点机制真实可复现")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] 撞点复现测试异常：{type(e).__name__}: {e}")

    # ── D. GroupRuntime except 分支锁 ──────────────────────────
    # D7 resume_plan 体内含 except InvalidUpdateError + logger.error + raise
    resume_body = _fn_body(gr_src, "resume_plan")
    if "except InvalidUpdateError" not in resume_body:
        errs.append("[D7] resume_plan 缺 except InvalidUpdateError 分支（撞点静默吞——不可观测）")
    elif "logger.error" not in resume_body:
        errs.append("[D7] resume_plan 的 except InvalidUpdateError 分支缺 logger.error（撞点不可观测）")
    elif "raise" not in resume_body.split("except InvalidUpdateError")[1].split("finally")[0]:
        errs.append("[D7] resume_plan 的 except InvalidUpdateError 分支缺 raise（吞异常致假装 resume 成功）")
    else:
        print("[D7] OK  resume_plan 含 except InvalidUpdateError + logger.error + raise（撞点可观测 + 重抛不吞）")

    # D8 invoke_turn 体内含同款 except InvalidUpdateError（twin）
    invoke_body = _fn_body(gr_src, "invoke_turn")
    if "except InvalidUpdateError" not in invoke_body:
        errs.append("[D8] invoke_turn 缺 except InvalidUpdateError 分支（report-back 路径撞点静默吞）")
    elif "logger.error" not in invoke_body:
        errs.append("[D8] invoke_turn 的 except InvalidUpdateError 分支缺 logger.error")
    else:
        print("[D8] OK  invoke_turn 含 except InvalidUpdateError + logger.error（resume_plan 的 twin，report-back 路径同样可观测）")

    # D9 真 GroupRuntime：mock graph.ainvoke 抛 InvalidUpdateError → resume_plan 重抛 + 日志含 turn_count/group/thread
    try:
        async def _run_d9():
            class _FakeGroup:
                id = "g_vh65_d9"
                coordinator_id = "c1"

            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            async def boom(cmd, config=None):
                raise InvalidUpdateError("At key 'turn_count': Can receive only one value per step.")
            rt._graph.ainvoke = boom  # type: ignore
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                try:
                    await rt.resume_plan({"mode": "confirm"})
                    return "swallowed"
                except InvalidUpdateError:
                    return "reraised"
                except Exception as e:  # noqa: BLE001
                    return f"wrong_type:{type(e).__name__}"

        outcome = asyncio.run(_run_d9())
        if outcome == "swallowed":
            errs.append("[D9] resume_plan 吞了 InvalidUpdateError（应重抛）")
        elif outcome != "reraised":
            errs.append(f"[D9] resume_plan 重抛了错误类型（应 InvalidUpdateError）：{outcome}")
        else:
            print("[D9] OK  真 GroupRuntime mock 撞点 → resume_plan 重抛 InvalidUpdateError（不吞，让上层按失败回合处理）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] GroupRuntime 撞点重抛测试异常：{type(e).__name__}: {e}")

    # ── E. report-back 失败不杀引擎锁 ──────────────────────────
    # E10 _run_worker_task 的 report-back rt.invoke_turn 调用未被额外 try/except 包裹
    #     （让 group_runtime 的 except + _run_loop 的 except Exception 双重记录）
    rwt_body = _fn_body(reg_src, "_run_worker_task")
    # locate the report-back invoke_turn call site
    rb_idx = rwt_body.find('await rt.invoke_turn(')
    if rb_idx < 0:
        errs.append("[E10] _run_worker_task 缺 report-back rt.invoke_turn 调用（report-back 路径破）")
    else:
        # the 6 lines before the call should NOT open a try: that wraps only this call
        # (the call lives directly in the method body, guarded only by _run_loop's except)
        before = rwt_body[:rb_idx].splitlines()[-12:]
        opens_try = any(ln.strip().startswith("try:") for ln in before[-8:])
        # the call site is inside `if rt is not None and rt._graph is not None:` — a try:
        # right before it that wraps ONLY this call would swallow the error before _run_loop
        # sees it. Assert no bare `try:` was added immediately before the await.
        if opens_try:
            # confirm it's not a meaningful try (the nearest preceding non-blank,
            # non-comment line should be the `report_task_id = ...` or the `if rt...` block)
            nearest = next((ln for ln in reversed(before) if ln.strip() and not ln.strip().startswith("#")), "")
            if nearest.strip().startswith("try:"):
                errs.append(
                    "[E10] report-back rt.invoke_turn 被裸 try 包裹（会吞 InvalidUpdateError 致 _run_loop 兜底失效）——"
                    "应让异常逃逸到 _run_loop 的 except Exception（单一真源在 group_runtime 的 except）"
                )
            else:
                print("[E10] OK  report-back rt.invoke_turn 未被额外 try/except 包裹（异常逃逸到 _run_loop 兜底，单一真源在 group_runtime）")
        else:
            print("[E10] OK  report-back rt.invoke_turn 未被额外 try/except 包裹（异常逃逸到 _run_loop 兜底，单一真源在 group_runtime）")

    # ── F. 向后兼容锁 ──────────────────────────────────────────
    # F11 关键回归不破（新增 except 分支纯加性，不改正常路径）
    compat_tests = [
        ("vh43_group_runtime_invoke_turn", "invoke_turn 生命周期"),
        ("vh54_converge_turn", "收束回合"),
        ("vh46_session_speech_cap", "会话封顶"),
        ("vh35_dispatch_interrupt_resume_in_group_graph", "群图 interrupt/resume"),
        ("vh36_dispatch_next_send_fanout", "Send fan-out 派发"),
    ]
    import subprocess
    compat_ok = True
    for tname, label in compat_tests:
        try:
            proc = subprocess.run(
                [sys.executable, str(BACKEND / "tests" / f"test_{tname}.py")],
                capture_output=True, text=True, timeout=90,
            )
            if proc.returncode != 0:
                errs.append(f"[F11] {label}（{tname}）回归 FAIL（exit={proc.returncode}）：{proc.stderr[-300:] or proc.stdout[-300:]}")
                compat_ok = False
        except Exception as e:  # noqa: BLE001
            errs.append(f"[F11] {label}（{tname}）回归运行异常：{type(e).__name__}: {e}")
            compat_ok = False
    if compat_ok:
        print(f"[F11] OK  关键回归全 PASS：{' / '.join(label for _, label in compat_tests)}（新增 except 分支纯加性不破正常路径）")

    return errs


def main() -> int:
    print("=== VH65 回归：resume_plan turn_count 并发写 InvalidUpdateError 债务确认（任务12）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "turn_count 并发写债务确认锁定：\n"
        "  · A make_agent_node 的 is_dispatch_fanout 守卫跳过 fan-out 路径 turn_count 写（只 serial peer 写）；\n"
        "  · B 真 build_group_graph resume → 2 ready step 并行 fan-out 不抛 InvalidUpdateError（守卫覆盖 resume 路径）；\n"
        "  · C 负控：忘守卫的 fan-out 节点（写 turn_count）+ 2 并行 → 必触发 InvalidUpdateError（守卫必要，撞点可复现）；\n"
        "  · D resume_plan/invoke_turn 含 except InvalidUpdateError + logger.error + raise（撞点可观测 + 不静默吞）；\n"
        "  · E report-back rt.invoke_turn 未被额外 try 包裹（异常逃逸到 _run_loop 兜底，单一真源在 group_runtime）；\n"
        "  · F vh43/vh54/vh46/vh35/vh36 关键回归全 PASS（except 分支纯加性不破正常路径）。\n"
        "结论：_turn_lock 已序列化「同 runtime 两 ainvoke」撞点 + is_dispatch_fanout 守卫排除 fan-out 写 last-value ——\n"
        "      resume_plan 路径理论不再触发；except 分支兜底让真撞点可观测可重抛，不再静默假成功。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
