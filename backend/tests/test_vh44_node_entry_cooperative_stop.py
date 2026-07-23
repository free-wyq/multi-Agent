"""VH44 回归：Option B·② 删 route_entry + make_agent_node 的 is_stopped 守卫 + cancel_turn live.

Option B·② 决策——删除协作式软停的「节点入口检查 is_stopped」守卫：
  · ``route_entry`` standalone + closure-bound twin 不再入口查 ``is_stopped()``；
  · ``make_agent_node`` 不再入口查 ``is_stopped()``；
  · 停止只留两入口：UI 终止按钮 ``cancel_turn``（硬切 task.cancel）+ ``SESSION_SPEECH_CAP=50`` 封顶。

设计真源见 memory ``converge-turn-design`` + ``stop-signal-cooperative-cancel-design``（Option B 删软停层）.

保留不动（本任务只删节点入口 is_stopped 守卫）：
  · contextvar ``get_group_runtime`` / ``set_group_runtime``（record_speech / is_session_capped 仍用）；
  · ``is_session_capped`` 守卫（50 封顶是保留的停的兜底入口之一）；
  · 「report-back 早返回」+「防连发守卫（recent_speakers）」+「turn_count 链 8」三道；
  · ``GroupRuntime`` 的 ``request_stop`` / ``is_stopped`` / ``reset_stop`` / ``_stop_event``（软停件由 Option B·③删，本任务不碰）。

八段契约（纯静态 + 真 asyncio stub + 真 build_group_graph + 真 cancel live，不依赖 live server / 真实 LLM）：

  A. contextvar 注入锁——set/get_group_runtime（保留，record_speech/cap 仍用）
    1. ``worker.set_group_runtime(rt)`` / ``get_group_runtime()`` 存在（默认 None）。
    2. invoke_turn / resume_plan 在 ainvoke 前 set_group_runtime(self)，finally 清（contextvar 注入，per-task 不串台）。

  B. route_entry is_stopped 守卫已删锁（standalone + closure-bound twin）
    3. standalone ``route_entry`` + closure-bound ``build_route_entry`` 体内**不再**查 ``is_stopped()``（Option B·② 删）。
    4. ``is_session_capped`` 守卫仍在（50 封顶保留）——route_entry 仍查 get_group_runtime + is_session_capped。
    5. runtime None → route_entry 按原逻辑分叉（cap 守卫跳过，向后兼容 vh39）。

  C. make_agent_node is_stopped 守卫已删锁
    6. ``make_agent_node`` 体内**不再**查 ``is_stopped()``（Option B·② 删）。
    7. ``is_session_capped`` 守卫仍在（50 封顶保留）。
    8. runtime None → make_agent_node 正常发言（cap 守卫跳过，向后兼容 vh40）。

  D. cancel_turn live 锁——硬切中断活跃回合（替代原 D7 request_stop live）
    9. 真 GroupRuntime + 群图：invoke_turn 跑到一半（ainvoke 阻塞中）cancel_turn() → 返 True（有活跃回合）+
       CancelledError 传入 ainvoke 断流 → invoke_turn 重抛 CancelledError → 回合终止 + _current_task 清空。
   10. cancel_turn 幂等：无活跃回合（_current_task=None）→ 返 False（no-op，不报错）。

  E. 守卫位置锁——make_agent_node 防连发 → cap → brain（is_stopped 已删）
   11. make_agent_node 源码顺序：already_spoke → is_session_capped → brain（is_stopped 守卫已不在该链上）。

  F. 向后兼容锁——main import OK + vh39/vh40/vh43 不破
   12. ``main`` 全量 import OK（删 is_stopped 守卫无 cycle）。
   13. vh39 route_entry kind 分叉 + vh40 防连发 + vh43 invoke_turn 生命周期不破。
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

    # ── A. contextvar 注入（保留：record_speech / is_session_capped 仍用）──────
    # A1 set/get_group_runtime 存在
    if not hasattr(worker_mod, "set_group_runtime") or not hasattr(worker_mod, "get_group_runtime"):
        errs.append("[A1] worker 缺 set_group_runtime / get_group_runtime")
    else:
        # round-trip: set then get
        worker_mod.set_group_runtime(None)
        if worker_mod.get_group_runtime() is not None:
            errs.append(f"[A1] get_group_runtime 默认应 None，实际 {worker_mod.get_group_runtime()!r}")
        else:
            print("[A1] OK  worker.set_group_runtime(rt) / get_group_runtime() 存在（默认 None，record_speech/cap 仍用）")

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

    # ── B. route_entry is_stopped 守卫已删（standalone + closure-bound twin）──
    re_body = _fn_body(gg_src, "route_entry")
    bre_body = _fn_body(gg_src, "build_route_entry")
    # B3 standalone route_entry + build_route_entry twin 不再查 is_stopped（Option B·② 删）
    #   注意：仅判定 ``is_stopped()`` 调用消失，``is_session_capped()`` 必须仍在。
    if "is_stopped()" in re_body:
        errs.append("[B3] standalone route_entry 仍查 is_stopped()（Option B·② 应删该守卫）")
    if "is_stopped()" in bre_body:
        errs.append("[B3] build_route_entry（closure-bound twin）仍查 is_stopped()（Option B·② 应删）")
    if not any(e.startswith("[B3]") for e in errs):
        print("[B3] OK  route_entry（standalone + closure-bound twin）已删 is_stopped() 守卫（Option B·②）")

    # B4 is_session_capped 守卫仍在（50 封顶保留）+ 仍查 get_group_runtime
    if "is_session_capped" not in re_body:
        errs.append("[B4] standalone route_entry 缺 is_session_capped（50 封顶守卫应保留）")
    if "is_session_capped" not in bre_body:
        errs.append("[B4] build_route_entry（closure-bound twin）缺 is_session_capped（应保留）")
    if "get_group_runtime" not in re_body or "get_group_runtime" not in bre_body:
        errs.append("[B4] route_entry / build_route_entry 缺 get_group_runtime（cap 守卫需取 runtime）")
    if not any(e.startswith("[B4]") for e in errs):
        print("[B4] OK  route_entry（standalone + twin）仍查 get_group_runtime + is_session_capped（50 封顶保留）")

    # B5 runtime None → route_entry 按原逻辑分叉（cap 守卫跳过，向后兼容 vh39）
    try:
        class _M:
            def __init__(self, aid): self.agent_id = aid; self.agent_name = aid; self.agent_role = "r"

        db_members = [_M("w1"), _M("w2")]
        async def _run_b5():
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
        cmd = await _run_b5()
        if cmd.goto == "__end__":
            errs.append(f"[B5] runtime None 时 route_entry 应按原逻辑分叉（非命中 cap END），实际 goto={cmd.goto!r}")
        else:
            print(f"[B5] OK  runtime None → cap 守卫跳过，route_entry 按原逻辑分叉 goto={cmd.goto!r}（向后兼容 vh39）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] runtime-None 跳过测试异常：{type(e).__name__}: {e}")

    # ── C. make_agent_node is_stopped 守卫已删 ──────────────
    man_body = _fn_body(w_src, "make_agent_node")
    # C6 make_agent_node 不再查 is_stopped（Option B·② 删）
    if "is_stopped()" in man_body:
        errs.append("[C6] make_agent_node 仍查 is_stopped()（Option B·② 应删该守卫）")
    else:
        print("[C6] OK  make_agent_node 已删 is_stopped() 守卫（Option B·②）")

    # C7 is_session_capped 守卫仍在（50 封顶保留）+ record_speech 仍在
    if "is_session_capped" not in man_body:
        errs.append("[C7] make_agent_node 缺 is_session_capped（50 封顶守卫应保留）")
    if "record_speech" not in man_body:
        errs.append("[C7] make_agent_node 缺 record_speech（发言计数应保留）")
    if not any(e.startswith("[C7]") for e in errs):
        print("[C7] OK  make_agent_node 仍查 is_session_capped + record_speech（50 封顶 + 计数保留）")

    # C8 runtime None → make_agent_node 正常发言（cap 守卫跳过，向后兼容 vh40）
    try:
        brain_called2: list[str] = []
        async def _fake_stream2(*a, **k):
            brain_called2.append("called")
            return ("r1", '{"action":"chat","content":"hi","reasoning":"r"}', 5, 50, "m1", 0, "")
        async def _run_c8():
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
        cmd, brain = await _run_c8()
        if not brain:
            errs.append("[C8] runtime None 时 make_agent_node 应正常发言（调 brain），实际 brain 未调")
        else:
            print("[C8] OK  runtime None → cap 守卫跳过，make_agent_node 正常发言（向后兼容 vh40）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] runtime-None agent 测试异常：{type(e).__name__}: {e}")

    # ── D. cancel_turn live——硬切中断活跃回合（替代原 D7 request_stop live）──
    # D9 真 GroupRuntime + 群图：invoke_turn 跑到一半 cancel_turn → CancelledError 断流 → 回合终止
    try:
        async def _run_d9():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, "", "centralized"))
            # 让 ainvoke 阻塞（模拟「活跃回合跑到一半」）——用 Event gate，cancel 后断流。
            entered = asyncio.Event()
            async def _blocking_ainvoke(turn_input, config=None):
                entered.set()  # 通知主流程：ainvoke 已进入（_current_task 已就位）
                # 阻塞直到被 cancel（CancelledError 传入此 await）
                await asyncio.sleep(3600)
            rt._graph.ainvoke = _blocking_ainvoke  # type: ignore
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                task = asyncio.create_task(
                    rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi")
                )
                # 等 ainvoke 进入（_current_task 已 set）
                await asyncio.wait_for(entered.wait(), timeout=5.0)
                # 此时回合活跃：cancel_turn 应返 True 并 task.cancel
                cancelled = rt.cancel_turn()
                # 等任务结束——应抛 CancelledError
                raised_cancel = False
                try:
                    await task
                except asyncio.CancelledError:
                    raised_cancel = True
            return cancelled, raised_cancel, rt._current_task
        cancelled, raised_cancel, cur_task = await _run_d9()
        if cancelled is not True:
            errs.append(f"[D9] 活跃回合 cancel_turn 应返 True，实际 {cancelled!r}")
        elif not raised_cancel:
            errs.append("[D9] cancel_turn 后 invoke_turn 应抛 CancelledError（硬切断流），实际未抛")
        elif cur_task is not None:
            errs.append(f"[D9] cancel 后 _current_task 应清空（finally _end_turn），实际 {cur_task!r}")
        else:
            print("[D9] OK  cancel_turn 中断活跃回合：返 True + CancelledError 断流 + _current_task 清空（硬切生效）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] cancel_turn live 测试异常：{type(e).__name__}: {e}")

    # D10 cancel_turn 幂等：无活跃回合 → 返 False（no-op，不报错）
    try:
        async def _run_d10():
            rt = GroupRuntime(_FakeGroup())
            # 无活跃回合（_current_task=None）
            return rt.cancel_turn()
        d10 = await _run_d10()
        if d10 is not False:
            errs.append(f"[D10] 无活跃回合 cancel_turn 应返 False（幂等 no-op），实际 {d10!r}")
        else:
            print("[D10] OK  无活跃回合 cancel_turn → 返 False（幂等 no-op，不报错）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] cancel_turn 幂等测试异常：{type(e).__name__}: {e}")

    # ── E. 守卫位置（is_stopped 已删，顺序：防连发 → cap → brain）──────────
    # E11 make_agent_node 源码顺序：already_spoke → is_session_capped → brain call
    try:
        idx_already = man_body.find("already_spoke =")
        idx_cap = man_body.find("is_session_capped")
        idx_brain = man_body.find("await _stream_brain_decision")
        if idx_already < 0 or idx_cap < 0 or idx_brain < 0:
            errs.append(f"[E11] make_agent_node 守卫顺序基准缺失（already_spoke=/is_session_capped/_stream 位置）")
        elif not (idx_already < idx_cap < idx_brain):
            errs.append(f"[E11] 顺序应 already_spoke < is_session_capped < brain，实际 {idx_already}/{idx_cap}/{idx_brain}")
        else:
            print("[E11] OK  make_agent_node 守卫顺序：防连发 → cap → brain（is_stopped 已删不在该链上）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E11] 守卫顺序测试异常：{type(e).__name__}: {e}")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F12 main import OK
    try:
        import main  # noqa: F401
        print("[F12] OK  main 全量 import OK（删 is_stopped 守卫无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F12] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # F13 vh39/vh40/vh43 不破
    try:
        # vh39 route_entry kind fork still works (runtime None → cap skip)
        # vh40 防连发 still works (cap guard after already_spoke, before brain)
        # vh43 invoke_turn lifecycle (set_group_runtime still set/clear — no leak)
        rt = GroupRuntime(_FakeGroup())
        await rt.compile_graph(members)
        rt._resolve_leader_identity = AsyncMock(return_value={"agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp"})
        rt._resolve_group_config = AsyncMock(return_value=(False, "", "centralized"))
        rt._graph.ainvoke = AsyncMock(return_value={"dispatch_plan": []})
        rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
        with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
            await rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi")
        # after invoke, runtime contextvar should be None (finally cleared)
        if worker_mod.get_group_runtime() is not None:
            errs.append(f"[F13] invoke_turn 后 get_group_runtime 应 None（finally 清），实际 {worker_mod.get_group_runtime()!r}")
        else:
            print("[F13] OK  vh39/vh40/vh43 不破（invoke_turn 仍 set/clear group_runtime 无回归，finally 清 slot）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F13] 兼容检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH44 回归：Option B·② 删 route_entry + make_agent_node 的 is_stopped 守卫 + cancel_turn live ===\n")
    errs = asyncio.run(assert_contract())
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "Option B·② 删节点入口 is_stopped 守卫锁定：\n"
        "  · A contextvar set/get_group_runtime 保留（record_speech/cap 仍用）+ invoke_turn/resume_plan set(self)+finally 清；\n"
        "  · B route_entry（standalone + closure-bound twin）已删 is_stopped() 守卫 + is_session_capped 仍在 + runtime None 跳过；\n"
        "  · C make_agent_node 已删 is_stopped() 守卫 + is_session_capped/record_speech 仍在 + runtime None 正常发言；\n"
        "  · D cancel_turn live 中断活跃回合（返 True + CancelledError 断流 + _current_task 清空）+ 幂等无活跃返 False；\n"
        "  · E 守卫顺序 防连发 → cap → brain（is_stopped 已删）；\n"
        "  · F main import OK 无 cycle + vh39/vh40/vh43 不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
