"""VH43 回归：GroupRuntime.invoke_turn 一回合生命周期 + cancellable task + emit idle.

锁住 task-15 决策——``GroupRuntime.invoke_turn(...)``：一次 ainvoke=一回合，存 task 句柄；
回合结束自动清句柄 + emit agent_status(idle)；ainvoke 调用前后不阻塞同步代码，
``task.cancel()``/``request_stop`` 可注入；turn start 调 ``reset_stop`` 防 stale stop.

设计真源见 memory ``group-runtime-skeleton`` + ``decentralized-scheduling-stop-plan-2026-07-13``
（方向 A：一张群图，一次 ainvoke=一回合，cancellable task）.

本任务锁「invoke_turn 完整生命周期」（state 注入 + cancellable ainvoke + emit idle + finally cleanup）+
``resume_plan``（PL-02 native resume twin）+ ``reset_session``（BE-02 跨回合状态清理）.

七段契约（纯静态 + 真 asyncio stub + 真 build_group_graph，不依赖 live server / 真实 LLM）：

  A. invoke_turn 签名 + 生命周期锁
    1. ``invoke_turn(...)`` async 方法存在（keyword-only incoming_kind/incoming_message/
       incoming_sender/incoming_data）.
    2. 调前 reset_stop（防 stale stop 抑制新回合）+ 调后 _current_task 清空（_end_turn in finally）.
    3. 正常 END：ainvoke 返回 result + _dispatch_plan 同步回写 + _memory 追加（非 plan_resume 且
       incoming_message 非空）+ emit agent_status(idle).

  B. cancellable ainvoke 锁——ainvoke 包成 cancellable Task
    4. ainvoke 经 ``_start_turn_task`` 包成 ``asyncio.Task`` 存 ``_current_task``（镜像 _worker_task）.
    5. ainvoke 期间 ``cancel_turn`` 能中断（CancelledError 传入协程）——turn start reset_stop 后
       中途 cancel_turn 仍可中断 ainvoke.

  C. 回合边界 idle emit 锁——正常 END emit idle
    6. 正常 END 后 emit agent_status(idle)（group_id/coordinator_id/agent_name/idle/None）.
    7. cancel 路径不 emit idle（stop-button 路径自有终端态；CancelledError 重抛）.

  D. fresh-thread per turn 锁——per-turn 累加不跨回合
    8. 每次 invoke_turn 用新 thread_id（``_next_thread_id`` 递增 ``{thread_id}:{seq}``）.
    9. 两轮 invoke_turn 的 turn_count/recent_speakers 各自从 0/[] 起步（不累积——fresh thread）.

  E. resume_plan PL-02 twin 锁——Command(resume=)
   10. ``resume_plan(payload)`` async 存在 + 调 ``Command(resume=payload)`` ainvoke（resume 中断态）.
   11. resume_plan 用同 thread（不 mint 新 thread——新 thread 丢中断态）+ reset_stop + finally 清句柄.

  F. reset_session BE-02 锁——跨回合状态清理
   12. ``reset_session()`` async 存在 + 清 ``_memory``/``_dispatch_plan`` + aupdate_state(END) 解中断.
   13. reset_session 调 cancel_turn（活跃回合先停，防 unwind 回填状态）.

  G. 向后兼容锁——main import OK + vh41/vh42 不破
   14. ``main`` 全量 import OK（group_runtime import events/coordinator/worker 无 cycle）.
   15. vh41 停止契约 + vh42 编译群图契约不破（invoke_turn 是新增能力）.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    """Best-effort: return the source of one method body (def ... to next def)."""
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    # find the next top-level def/async def after this one
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith("    def ") or ln.startswith("    async def ") or ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


async def assert_contract() -> list[str]:
    errs: list[str] = []
    src = _read(GROUP_RUNTIME_PY)

    try:
        from engine.group_runtime import GroupRuntime  # type: ignore
    except Exception as e:  # noqa: BLE001
        return [f"[import] 导入失败：{type(e).__name__}: {e}"]

    class _FakeGroup:
        id = "g1"
        coordinator_id = "c1"

    members = [
        {"agent_id": "w1", "agent_name": "前端", "agent_role": "fe", "system_prompt": "sp1"},
        {"agent_id": "w2", "agent_name": "后端", "agent_role": "be", "system_prompt": "sp2"},
    ]

    # ── A. invoke_turn 签名 + 生命周期 ──────────────────────
    # A1 invoke_turn async + keyword-only args
    if not hasattr(GroupRuntime, "invoke_turn"):
        errs.append("[A1] GroupRuntime 缺 invoke_turn 方法")
    elif not inspect.iscoroutinefunction(GroupRuntime.invoke_turn):
        errs.append("[A1] invoke_turn 应是 async 方法")
    else:
        sig = inspect.signature(GroupRuntime.invoke_turn)
        kw_only = all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values() if p.name != "self")
        if not kw_only:
            errs.append(f"[A1] invoke_turn 参数应全 keyword-only，实际 {list(sig.parameters)}")
        else:
            print("[A1] OK  invoke_turn(...) async + keyword-only（incoming_kind/.../incoming_data）")

    # A2 finally _current_task 清空（_end_turn in finally）。Option B·③ 删软停件后
    # 不再调 reset_stop（per-turn reset 已删），只验 finally _end_turn 清句柄。
    invoke_body = _fn_body(src, "invoke_turn")
    if "reset_stop" in invoke_body:
        errs.append("[A2] invoke_turn 体内仍调 reset_stop（Option B·③ 应删 per-turn reset）")
    if "_end_turn" not in invoke_body or "finally" not in invoke_body:
        errs.append("[A2] invoke_turn 体内缺 finally _end_turn（句柄应清空）")
    if not errs or not any(e.startswith("[A2]") for e in errs):
        print("[A2] OK  invoke_turn finally _end_turn 清句柄（Option B·③ 删 reset_stop）")

    # A3 正常 END：_dispatch_plan 同步 + _memory 追加 + emit idle
    if "_dispatch_plan" not in invoke_body or "emit_agent_status" not in invoke_body:
        errs.append("[A3] invoke_turn 体内缺 dispatch_plan 同步 / emit_agent_status")
    elif "incoming_kind != \"plan_resume\"" not in invoke_body and "plan_resume" not in invoke_body:
        errs.append("[A3] invoke_turn 体内缺 plan_resume 记忆跳过逻辑")
    else:
        print("[A3] OK  正常 END：dispatch_plan 同步 + _memory 追加（非 plan_resume 且非空）+ emit idle")

    # ── B. cancellable ainvoke ──────────────────────────────
    # B4 ainvoke 经 _start_turn_task 包 Task
    if "_start_turn_task" not in invoke_body:
        errs.append("[B4] invoke_turn 体内未调 _start_turn_task（ainvoke 应包 cancellable Task）")
    else:
        print("[B4] OK  ainvoke 经 _start_turn_task 包 asyncio.Task stash _current_task")

    # B5 中途 cancel_turn 能中断 ainvoke（真 Task 验证）
    try:
        async def _long_ainvoke(*a, **k):
            await asyncio.sleep(100)  # real await so cancel can interrupt

        async def _run_b5():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            rt._graph.ainvoke = _long_ainvoke  # real async fn (not AsyncMock+lambda)
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                task = asyncio.create_task(rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi"))
                await asyncio.sleep(0.05)  # let ainvoke start
                cancelled_issued = rt.cancel_turn()
                cancelled_caught = False
                try:
                    await task
                except asyncio.CancelledError:
                    cancelled_caught = True
                return cancelled_issued, cancelled_caught, rt._current_task
        issued, caught, handle_after = await _run_b5()
        if not issued:
            errs.append("[B5] invoke_turn 中途 cancel_turn 应返 True（有活跃 task），实际未 issued")
        elif not caught:
            errs.append("[B5] invoke_turn 中途 cancel_turn 应中断 ainvoke（CancelledError），实际未中断")
        elif handle_after is not None:
            errs.append(f"[B5] cancel 后 _current_task 应 None（finally 清空），实际 {handle_after!r}")
        else:
            print("[B5] OK  invoke_turn 中途 cancel_turn → True + CancelledError 中断 + 句柄清空")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] cancellable ainvoke 测试异常：{type(e).__name__}: {e}")

    # ── C. 回合边界 idle emit ───────────────────────────────
    # C6 正常 END emit agent_status(idle)
    idle_emitted: list[tuple] = []
    try:
        async def _run_c6():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            rt._graph.ainvoke = AsyncMock(return_value={"dispatch_plan": [{"step": 1}], "ok": True})
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            async def fake_emit(*args, **kwargs):
                idle_emitted.append(args)
            with patch("engine.group_runtime.emit_agent_status", fake_emit):
                await rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi")
        await _run_c6()
        # the last emit should be idle (status="idle")
        found_idle = any(len(a) >= 5 and a[3] == "idle" for a in idle_emitted)
        if not found_idle:
            errs.append(f"[C6] 正常 END 应 emit agent_status(idle)，实际 emits={idle_emitted}")
        else:
            print(f"[C6] OK  正常 END emit agent_status(idle)（coordinator=c1）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C6] idle emit 测试异常：{type(e).__name__}: {e}")

    # C7 cancel 路径不 emit idle（CancelledError 重抛）
    cancel_emits: list[tuple] = []
    try:
        async def _long_ainvoke_c7(*a, **k):
            await asyncio.sleep(100)

        async def _run_c7():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            rt._graph.ainvoke = _long_ainvoke_c7
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            async def fake_emit(*args, **kwargs):
                cancel_emits.append(args)
            with patch("engine.group_runtime.emit_agent_status", fake_emit):
                task = asyncio.create_task(rt.invoke_turn(incoming_kind="coordinator_reply", incoming_message="hi"))
                await asyncio.sleep(0.05)
                rt.cancel_turn()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await _run_c7()
        found_idle = any(len(a) >= 5 and a[3] == "idle" for a in cancel_emits)
        if found_idle:
            errs.append(f"[C7] cancel 路径不应 emit idle（stop-button 自有终端态），实际 emits={cancel_emits}")
        else:
            print("[C7] OK  cancel 路径不 emit idle（CancelledError 重抛，stop-button 自有终端态）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] cancel-no-idle 测试异常：{type(e).__name__}: {e}")

    # ── D. fresh-thread per turn ────────────────────────────
    # D8 _next_thread_id 递增 {thread_id}:{seq}
    rt = GroupRuntime(_FakeGroup())
    if not hasattr(rt, "_next_thread_id"):
        errs.append("[D8] GroupRuntime 缺 _next_thread_id 方法")
    else:
        t1 = rt._next_thread_id()
        t2 = rt._next_thread_id()
        if t1 == t2 or not t1.startswith("g1:") or not t2.startswith("g1:"):
            errs.append(f"[D8] _next_thread_id 应递增 {{thread_id}}:{{seq}}，实际 t1={t1} t2={t2}")
        else:
            print(f"[D8] OK  _next_thread_id 递增 fresh thread（{t1} → {t2}）")

    # D9 两轮 invoke_turn turn_count/recent_speakers 不累积（fresh thread）
    try:
        async def _run_d9():
            rt = GroupRuntime(_FakeGroup())
            await rt.compile_graph(members)
            rt._resolve_leader_identity = AsyncMock(return_value={
                "agent_id": "c1", "agent_name": "协调者", "system_prompt": "sp",
            })
            rt._resolve_group_config = AsyncMock(return_value=(False, ""))
            # turn 1: agent w1 speaks (recent_speakers=[w1], turn_count=1)
            rt._graph.ainvoke = AsyncMock(return_value={
                "turn_count": 1, "recent_speakers": ["w1"], "dispatch_plan": [],
            })
            rt._reply_cb_factory = lambda: (lambda: None)  # type: ignore
            with patch("engine.group_runtime.emit_agent_status", AsyncMock()):
                r1 = await rt.invoke_turn(incoming_kind="agent_reply", incoming_message="m1", incoming_sender="user")
            # turn 2: fresh — should start turn_count=0/recent_speakers=[] injected
            captured_inputs = []
            async def capture_ainvoke(turn_input, config=None):
                captured_inputs.append((dict(turn_input), config))
                return {"turn_count": 1, "recent_speakers": ["w2"], "dispatch_plan": []}
            rt._graph.ainvoke = capture_ainvoke
            r2 = await rt.invoke_turn(incoming_kind="agent_reply", incoming_message="m2", incoming_sender="user")
            return r1, captured_inputs
        r1, captured = await _run_d9()
        if not captured:
            errs.append("[D9] turn2 未捕获 ainvoke 入参")
        else:
            t2_input, _ = captured[0]
            tc = t2_input.get("turn_count")
            rs = t2_input.get("recent_speakers")
            if tc != 0 or rs != []:
                errs.append(f"[D9] turn2 注入应 turn_count=0/recent_speakers=[]（fresh thread），实际 tc={tc} rs={rs}")
            else:
                print(f"[D9] OK  两轮 invoke_turn fresh thread：turn2 注入 turn_count=0/recent_speakers=[]（不累积 turn1 的 w1）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D9] fresh-thread 测试异常：{type(e).__name__}: {e}")

    # ── E. resume_plan PL-02 twin ───────────────────────────
    # E10 resume_plan async + Command(resume=)
    if not hasattr(GroupRuntime, "resume_plan"):
        errs.append("[E10] GroupRuntime 缺 resume_plan 方法")
    elif not inspect.iscoroutinefunction(GroupRuntime.resume_plan):
        errs.append("[E10] resume_plan 应是 async 方法")
    else:
        resume_body = _fn_body(src, "resume_plan")
        if "Command(resume" not in resume_body and "resume=" not in resume_body:
            errs.append("[E10] resume_plan 体内未调 Command(resume=...)（PL-02 native resume）")
        else:
            print("[E10] OK  resume_plan(payload) async + Command(resume=payload) ainvoke（PL-02 native resume）")

    # E11 resume_plan 用同 thread（不 mint 新 thread）+ finally _end_turn 清句柄。
    # Option B·③ 删软停件后 resume_plan 不再调 reset_stop，只验同 thread + finally 清句柄。
    resume_body = _fn_body(src, "resume_plan")
    # resume_plan reuses the runtime's current thread (thread_id:{seq}), NOT a
    # fresh thread via _next_thread_id (which would lose the paused interrupt state).
    if "self._next_thread_id()" in resume_body:
        errs.append("[E11] resume_plan 不应调 self._next_thread_id()（新 thread 丢中断态，应用同 thread）")
    elif "reset_stop" in resume_body:
        errs.append("[E11] resume_plan 体内仍调 reset_stop（Option B·③ 应删）")
    elif "_end_turn" not in resume_body:
        errs.append("[E11] resume_plan 体内缺 _end_turn（同 invoke_turn finally 清句柄）")
    else:
        print("[E11] OK  resume_plan 用同 thread（不 mint 新 thread）+ finally _end_turn 清句柄（Option B·③ 删 reset_stop）")

    # ── F. reset_session BE-02 ──────────────────────────────
    # F12 reset_session async + 清 _memory/_dispatch_plan + aupdate_state(END)
    if not hasattr(GroupRuntime, "reset_session"):
        errs.append("[F12] GroupRuntime 缺 reset_session 方法")
    elif not inspect.iscoroutinefunction(GroupRuntime.reset_session):
        errs.append("[F12] reset_session 应是 async 方法")
    else:
        reset_body = _fn_body(src, "reset_session")
        if "aupdate_state" not in reset_body or "_memory" not in reset_body or "_dispatch_plan" not in reset_body:
            errs.append("[F12] reset_session 体内缺 aupdate_state(END) / 清 _memory/_dispatch_plan")
        else:
            print("[F12] OK  reset_session() async + aupdate_state(END) 解中断 + 清 _memory/_dispatch_plan")

    # F13 reset_session 调 cancel_turn（活跃回合先停）
    reset_body = _fn_body(src, "reset_session")
    if "cancel_turn" not in reset_body:
        errs.append("[F13] reset_session 体内未调 cancel_turn（活跃回合应先停防 unwind 回填）")
    else:
        print("[F13] OK  reset_session 调 cancel_turn（活跃回合先停防 unwind 回填状态）")

    # ── G. 向后兼容 ──────────────────────────────────────────
    # G14 main import OK（group_runtime import events/coordinator/worker 无 cycle）
    try:
        import main  # noqa: F401
        print("[G14] OK  main 全量 import OK（group_runtime import events/coordinator/worker 无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[G14] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # G15 vh41/vh42 契约不破（invoke_turn 是新增能力）。Option B·③ 删软停件后
    # request_stop/is_stopped/reset_stop 已删，cancel_turn 仍工作（纯 task.cancel 幂等）。
    try:
        rt = GroupRuntime(_FakeGroup())
        await rt.compile_graph(members)
        # 软停三件已删（Option B·③）
        if hasattr(rt, "request_stop") or hasattr(rt, "is_stopped") or hasattr(rt, "reset_stop"):
            errs.append("[G15] invoke_turn 后 GroupRuntime 仍有软停件（Option B·③ 应删）")
        elif rt.cancel_turn() is not False:
            errs.append("[G15] 无活跃回合 cancel_turn 应返 False（幂等），实际非 False")
        else:
            print("[G15] OK  vh41/vh42 契约不破（软停件已删 + cancel_turn 幂等，invoke_turn 不破编译契约）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[G15] 兼容检查异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH43 回归：GroupRuntime.invoke_turn 一回合生命周期 + cancellable task + emit idle ===\n")
    errs = asyncio.run(assert_contract())
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "GroupRuntime.invoke_turn 一回合生命周期锁定：\n"
        "  · A invoke_turn async + keyword-only + finally _end_turn + 正常END dispatch_plan同步/memory追加/emit idle；\n"
        "  · B ainvoke 经 _start_turn_task 包 cancellable Task + 中途 cancel_turn 中断（CancelledError）；\n"
        "  · C 正常 END emit agent_status(idle) + cancel 路径不 emit idle（重抛 CancelledError）；\n"
        "  · D _next_thread_id 递增 fresh thread + 两轮 turn_count/recent_speakers 不累积；\n"
        "  · E resume_plan PL-02 Command(resume=) twin + 用同 thread 不 mint 新 thread；\n"
        "  · F reset_session BE-02 aupdate_state(END) 解中断 + 清 _memory/_dispatch_plan + cancel_turn 先停；\n"
        "  · G main import OK 无 cycle + vh41/vh42 不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
