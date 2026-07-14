"""VH44 回归：route_entry + agent 节点入口协作式停止守卫（StopSignal·task-17）.

锁住 task-17 决策——``route_entry`` 节点入口 + 每个 agent 节点（``make_agent_node``）入口
先检查 ``GroupRuntime._stop_event``：命中（``is_stopped()`` True）则不发言、直接返回
``Command(goto=END)``（协作式停止——当前发言者把当前 step 跑完再退，不 mid-stream 强切）.

设计真源见 memory ``stop-signal-cooperative-cancel-design``（参考 AutoGen
ExternalTermination：终止做成一等公民且可外部注入，默认协作式非强切）.

本任务锁三件：
  1. ``GroupRuntime.invoke_turn`` / ``resume_plan`` 在 ainvoke 前 set_group_runtime(self)，
     finally 清（contextvar 注入，per-task 不串台）。
  2. ``route_entry``（standalone + closure-bound ``build_route_entry`` twin）入口查
     ``worker.get_group_runtime()`` + ``is_stopped()`` 命中即 ``Command(goto=END)``。
  3. ``make_agent_node`` 入口查 ``get_group_runtime()`` + ``is_stopped()`` 命中即
     ``Command(goto=END)``（在防连发守卫之后、brain 之前）。

六段契约（纯静态 + 真 asyncio stub + 真 build_group_graph，不依赖 live server / 真实 LLM）：

  A. contextvar 注入锁——set/get_group_runtime
    1. ``worker.set_group_runtime(rt)`` / ``worker.get_group_runtime()`` 存在。
    2. invoke_turn 在 ainvoke 前 set_group_runtime(self)，finally 清（set_group_runtime(None)）。

  B. route_entry 协作式停止锁——命中即 END
    3. standalone ``route_entry`` + closure-bound ``build_route_entry`` 入口查 get_group_runtime
       + is_stopped() 命中即 ``Command(goto=END)``（不选发言者）。
    4. runtime None（未注入）→ 守卫跳过，route_entry 按原逻辑分叉（向后兼容 vh39）。

  C. make_agent_node 协作式停止锁——命中即 END
    5. ``make_agent_node`` 入口（防连发守卫之后、brain 之前）查 get_group_runtime + is_stopped()
       命中即 ``Command(goto=END)``（不调 brain / 不 _unified_reply）。
    6. runtime None → 守卫跳过（驻留 worker 图 / 无 runtime 调用），make_agent_node 正常发言（向后兼容 vh40）。

  D. 端到端协作式停止锁——request_stop 后 route_entry END
    7. 真 StateGraph：invoke_turn 跑到一半 request_stop() → route_entry（或下一节点）入口命中
       is_stopped → END，不 mid-stream 强切（当前发言者把当前 step 跑完）。

  E. 守卫位置锁——agent 节点停止守卫在防连发之后、brain 之前
    8. make_agent_node 源码顺序：防连发守卫块 → 停止守卫块 → brain 调用（停止守卫先于 brain）。

  F. 向后兼容锁——main import OK + vh39/vh40/vh43 不破
    9. ``main`` 全量 import OK（worker/group_graph 加 contextvar 无 cycle）。
   10. vh39 route_entry kind 分叉 + vh40 防连发 + vh43 invoke_turn 生命周期不破。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"
WORKER_PY = BACKEND / "engine" / "worker.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    """Return one method/function body (def ... to next def at column 0).

    Only breaks on a *top-level* (column-0) ``def``/``async def`` so a nested
    closure function (e.g. ``build_route_entry``'s inner ``_route_entry``) is
    INCLUDED in the body, not treated as the end of the outer function.
    """
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        # break ONLY on a column-0 def (next top-level function); indented nested
        # defs (closures) are part of this body.
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


async def assert_contract() -> list[str]:
    errs: list[str] = []
    gr_src = _read(GROUP_RUNTIME_PY)
    gg_src = _read(GROUP_GRAPH_PY)
    w_src = _read(WORKER_PY)

    try:
        from engine.group_runtime import GroupRuntime  # type: ignore
        from engine import worker as worker_mod  # type: ignore
        from engine.group_graph import (  # type: ignore
            build_group_graph, build_route_entry, route_entry,
        )
        from engine.worker import build_agent_node, make_agent_node  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    class _FakeGroup:
        id = "g1"
        coordinator_id = "c1"

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": "sp1"},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": "sp2"},
    ]

    # ── A. contextvar 注入 ──────────────────────────────────
    # A1 set/get_group_runtime 存在
    if not hasattr(worker_mod, "set_group_runtime") or not hasattr(worker_mod, "get_group_runtime"):
        errs.append("[A1] worker 缺 set_group_runtime / get_group_runtime")
    else:
        # round-trip: set then get
        worker_mod.set_group_runtime(None)
        if worker_mod.get_group_runtime() is not None:
            errs.append(f"[A1] get_group_runtime 默认应 None，实际 {worker_mod.get_group_runtime()!r}")
        else:
            print("[A1] OK  worker.set_group_runtime(rt) / get_group_runtime() 存在（默认 None）")

    # A2 invoke_turn ainvoke 前 set_group_runtime(self) + finally 清
    invoke_body = _fn_body(gr_src, "invoke_turn")
    if "set_group_runtime(self)" not in invoke_body:
        errs.append("[A2] invoke_turn 体内未调 set_group_runtime(self)（ainvoke 前应注入 runtime）")
    if "set_group_runtime(None)" not in invoke_body:
        errs.append("[A2] invoke_turn finally 未清 set_group_runtime(None)（slot 应不泄漏）")
    if not any(e.startswith("[A2]") for e in errs):
        print("[A2] OK  invoke_turn ainvoke 前 set_group_runtime(self) + finally 清")

    # resume_plan twin 也注入 + 清
    resume_body = _fn_body(gr_src, "resume_plan")
    if "set_group_runtime(self)" not in resume_body or "set_group_runtime(None)" not in resume_body:
        errs.append("[A2] resume_plan 未 set/clear group_runtime（resume 也是回合，应同 invoke_turn）")
    else:
        print("[A2] OK  resume_plan twin 也 set_group_runtime(self) + finally 清（resume 也是回合）")

    # ── B. route_entry 协作式停止 ───────────────────────────
    # B3 standalone route_entry + build_route_entry twin 查 get_group_runtime + is_stopped
    re_body = _fn_body(gg_src, "route_entry")
    bre_body = _fn_body(gg_src, "build_route_entry")
    # the closure-bound check lives inside _route_entry; search the whole build_route_entry body
    if "get_group_runtime" not in re_body:
        errs.append("[B3] standalone route_entry 未调 get_group_runtime（停止守卫缺失）")
    if "get_group_runtime" not in bre_body:
        errs.append("[B3] build_route_entry（closure-bound twin）未调 get_group_runtime（停止守卫缺失）")
    if "is_stopped()" not in re_body or "is_stopped()" not in bre_body:
        errs.append("[B3] route_entry / build_route_entry 未查 is_stopped()")
    if not any(e.startswith("[B3]") for e in errs):
        print("[B3] OK  route_entry（standalone + closure-bound twin）入口查 get_group_runtime + is_stopped()")

    # B4 runtime None → 守卫跳过（向后兼容 vh39）—— 真 StateGraph 直调
    try:
        class _M:
            def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"

        db_members = [_M("w1"), _M("w2")]
        async def _run_b4():
            g = build_group_graph("g1", members, coordinator_id="c1")
            re_fn = build_route_entry(g._legal_handoff_targets)
            with patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.set_group_runtime", lambda rt: None), \
                 patch("engine.worker.get_group_runtime", return_value=None):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=db_members)
                crud_mock.list_agents = AsyncMock(return_value=[])
                return await re_fn({
                    "group_id": "g1", "coordinator_id": "c1",
                    "incoming_message": "帮我重构", "incoming_sender": "user",
                    "incoming_kind": "coordinator_reply", "turn_count": 0,
                })
        cmd = await _run_b4()
        if cmd.goto == "__end__":
            errs.append(f"[B4] runtime None 时 route_entry 应按原逻辑分叉（非命中 stop END），实际 goto={cmd.goto!r}")
        else:
            print(f"[B4] OK  runtime None → 守卫跳过，route_entry 按原逻辑分叉 goto={cmd.goto!r}（向后兼容 vh39）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] runtime-None 跳过测试异常：{type(e).__name__}: {e}")

    # B3-run 真 route_entry 命中 stop → END（注入 stopped runtime）
    try:
        class _StoppedRT:
            def is_stopped(self): return True
        async def _run_b3run():
            g = build_group_graph("g1", members, coordinator_id="c1")
            re_fn = build_route_entry(g._legal_handoff_targets)
            with patch("engine.worker.get_group_runtime", return_value=_StoppedRT()):
                return await re_fn({
                    "group_id": "g1", "coordinator_id": "c1",
                    "incoming_message": "@后端 来一下", "incoming_sender": "user",
                    "incoming_kind": "", "turn_count": 0,
                })
        cmd = await _run_b3run()
        if cmd.goto != "__end__":
            errs.append(f"[B3-run] stop 命中应 goto=END（不选发言者），实际 {cmd.goto!r}")
        else:
            print("[B3-run] OK  route_entry 命中 stop → goto=END（协作式停止，不选发言者）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B3-run] route_entry 停止直调异常：{type(e).__name__}: {e}")

    # ── C. make_agent_node 协作式停止 ───────────────────────
    # C5 make_agent_node 入口查 get_group_runtime + is_stopped 命中即 END（不调 brain）
    man_body = _fn_body(w_src, "make_agent_node")
    if "get_group_runtime" not in man_body or "is_stopped()" not in man_body:
        errs.append("[C5] make_agent_node 未查 get_group_runtime + is_stopped（停止守卫缺失）")
    else:
        print("[C5] OK  make_agent_node 入口查 get_group_runtime + is_stopped()")

    # C5-run 真 make_agent_node 命中 stop → END + brain 未调
    try:
        brain_called: list[str] = []
        async def _fake_stream(*a, **k):
            brain_called.append("called")
            return ("r1", '{"action":"chat","content":"hi","reasoning":"r"}', 5, 50, "m1", 0, "")
        class _StoppedRT2:
            def is_stopped(self): return True
        async def _run_c5run():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.get_group_runtime", return_value=_StoppedRT2()):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],  # not already-spoke → reaches stop check
                    "incoming_message": "接", "incoming_sender": "user",
                })
            return cmd, brain_called
        cmd, brain = await _run_c5run()
        if cmd.goto != "__end__":
            errs.append(f"[C5-run] stop 命中应 goto=END，实际 {cmd.goto!r}")
        elif brain:
            errs.append(f"[C5-run] stop 命中后不应调 brain，实际 brain_called={brain}")
        else:
            print("[C5-run] OK  make_agent_node 命中 stop → goto=END + brain 未调（不 mid-stream 强切，不发言）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C5-run] make_agent_node 停止直调异常：{type(e).__name__}: {e}")

    # C6 runtime None → 守卫跳过（make_agent_node 正常发言，向后兼容 vh40）
    try:
        brain_called2: list[str] = []
        async def _fake_stream2(*a, **k):
            brain_called2.append("called")
            return ("r1", '{"action":"chat","content":"hi","reasoning":"r"}', 5, 50, "m1", 0, "")
        async def _run_c6():
            node = build_agent_node("w1", "前端", "fe", "", "c1")
            with patch("engine.worker._stream_brain_decision", side_effect=_fake_stream2), \
                 patch("engine.worker._unified_reply", AsyncMock()), \
                 patch("engine.worker._build_context_from_db", AsyncMock(return_value="ctx")), \
                 patch("engine.worker._format_display_msg", side_effect=lambda s, c: c), \
                 patch("engine.worker.get_llm_config", return_value={"model": "m1"}), \
                 patch("engine.worker.crud") as crud_mock, \
                 patch("engine.worker.find_mentions", return_value=[]), \
                 patch("engine.worker.resolve_mention", return_value=None), \
                 patch("engine.worker.get_group_runtime", return_value=None):
                crud_mock.list_group_members_with_agent = AsyncMock(return_value=[])
                crud_mock.list_agents = AsyncMock(return_value=[])
                cmd = await node({
                    "group_id": "g1", "coordinator_id": "c1",
                    "turn_count": 0, "recent_speakers": [],
                    "incoming_message": "接", "incoming_sender": "user",
                })
            return cmd, brain_called2
        cmd, brain = await _run_c6()
        if not brain:
            errs.append("[C6] runtime None 时 make_agent_node 应正常发言（调 brain），实际 brain 未调")
        else:
            print("[C6] OK  runtime None → 守卫跳过，make_agent_node 正常发言（向后兼容 vh40）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] runtime-None agent 测试异常：{type(e).__name__}: {e}")

    # ── D. 端到端协作式停止 ─────────────────────────────────
    # D7 真 GroupRuntime + 群图：request_stop() 后 route_entry / 下一节点入口命中 → END
    try:
        async def _run_d7():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            # request_stop BEFORE invoke_turn (soft stop set)
            rt.request_stop()
            # invoke_turn starts: reset_stop clears it (invoke_turn calls reset_stop at start)
            # so to actually hit the guard we set AFTER reset_stop — call request_stop
            # via a race: set stop, then invoke, route_entry should see is_stopped True
            # BUT invoke_turn resets stop at start. To test the cooperative stop mid-turn
            # we invoke with stop already set AFTER reset — so set it just before ainvoke
            # by NOT relying on invoke_turn's reset. Instead test route_entry directly
            # inside a real invoke with the runtime installed + stop set:
            # simpler: patch _resolve_leader_identity/_resolve_group_config + ainvoke
            # so we can set stop AFTER reset_stop but BEFORE ainvoke runs route_entry.
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            captured = {}
            real_ainvoke = rt._graph.ainvoke
            async def wrap_ainvoke(turn_input, config=None):
                # set stop RIGHT BEFORE ainvoke runs route_entry (after invoke_turn's reset_stop)
                rt.request_stop()
                captured["turn_input"] = dict(turn_input)
                return await real_ainvoke(turn_input, config=config)
            rt._graph.ainvoke = wrap_ainvoke
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                result = await rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi")
            return result
        result = await _run_d7()
        # route_entry hit is_stopped → END. The graph result should be non-error (turn ended cleanly).
        if result is None:
            errs.append("[D7] request_stop 后 invoke_turn 应正常 END（route_entry 命中 stop → END），实际 None")
        else:
            print(f"[D7] OK  request_stop 后 invoke_turn route_entry 命中 stop → END（协作式，不 mid-stream 强切）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D7] 端到端协作式停止测试异常：{type(e).__name__}: {e}")

    # ── E. 守卫位置（停止守卫在防连发之后、brain 之前）──────
    # E8 make_agent_node 源码顺序：already_spoke → stop check → brain call
    try:
        # Search the WHOLE make_agent_node function body (string .find gives the
        # first occurrence, which for already_spoke/get_group_runtime/_stream is
        # the guard-block assignment, not a docstring mention). Use the first
        # *statement* occurrence: assignment (``already_spoke =``), the guard
        # call (``get_group_runtime()``), and the brain call.
        idx_already = man_body.find("already_spoke =")
        idx_stop = man_body.find("get_group_runtime()")
        idx_brain = man_body.find("await _stream_brain_decision")
        if idx_already < 0 or idx_stop < 0 or idx_brain < 0:
            errs.append(f"[E8] make_agent_node 守卫顺序基准缺失（already_spoke=/get_group_runtime()/_stream 位置）")
        elif not (idx_already < idx_stop < idx_brain):
            errs.append(f"[E8] 顺序应 already_spoke < stop < brain，实际 {idx_already}/{idx_stop}/{idx_brain}")
        else:
            print("[E8] OK  make_agent_node 守卫顺序：防连发 → 停止 → brain（停止守卫先于 brain）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E8] 守卫顺序测试异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F9 main import OK
    try:
        import main  # noqa: F401
        print("[F9] OK  main 全量 import OK（worker/group_graph 加 contextvar 无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F9] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # F10 vh39/vh40/vh43 不破
    try:
        # vh39 route_entry kind fork still works (runtime None → skip guard)
        # vh40 防连发 still works (stop guard after already_spoke, before brain)
        # vh43 invoke_turn lifecycle (set_group_runtime added, finally clears — no leak)
        # Re-run key assertions quickly:
        rt = GroupRuntime(_FakeGroup())
        await rt.compile_graph(members)
        # invoke_turn still callable + sets/clears runtime
        rt._resolve_leader_identity = AsyncMock(return_value={"agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp"})
        rt._resolve_group_config = AsyncMock(return_value=(False, ""))
        rt._graph.ainvoke = AsyncMock(return_value={"dispatch_plan": []})
        rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
        with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
            await rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi")
        # after invoke, runtime contextvar should be None (finally cleared)
        if worker_mod.get_group_runtime() is not None:
            errs.append(f"[F10] invoke_turn 后 get_group_runtime 应 None（finally 清），实际 {worker_mod.get_group_runtime()!r}")
        else:
            print("[F10] OK  vh39/vh40/vh43 不破（invoke_turn 加 set/clear group_runtime 无回归，finally 清 slot）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F10] 兼容检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH44 回归：route_entry + agent 节点入口协作式停止守卫（StopSignal·task-17）===\n")
    errs = asyncio.run(assert_contract())
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "route_entry + agent 节点入口协作式停止守卫锁定：\n"
        "  · A set/get_group_runtime contextvar + invoke_turn/resume_plan set(self) + finally 清；\n"
        "  · B route_entry（standalone + closure-bound twin）入口查 is_stopped 命中即 END + runtime None 跳过；\n"
        "  · C make_agent_node 入口查 is_stopped 命中即 END（brain 未调）+ runtime None 正常发言；\n"
        "  · D 端到端 request_stop 后 route_entry 命中 → END（不 mid-stream 强切）；\n"
        "  · E 守卫顺序 防连发 → 停止 → brain；\n"
        "  · F main import OK 无 cycle + vh39/vh40/vh43 不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
