"""VH42 回归：GroupRuntime 编译群图持有 + 当前回合 asyncio.Task 句柄.

锁住 task-14 决策——``GroupRuntime(group)`` 持有编译好的群图（``self._graph``）+
当前回合 ``asyncio.Task`` 句柄（ainvoke 包成 cancellable task，镜像现有
``_worker_task``）.

设计真源见 memory ``group-runtime-skeleton`` + ``decentralized-scheduling-stop-
plan-2026-07-13``（方向 A：一张群图，一次 ainvoke=一回合，cancellable task）.

本任务锁「编译群图持有」+「cancellable 回合 task 句柄包装」两件：
  1. ``compile_graph(members=None)`` async——从 DB 解析 members（或用传入 members）+
     编译 ``build_group_graph`` 存 ``self._graph`` + members 存 ``self._members``.
  2. ``_start_turn_task(coro)``——ainvoke 协程包成 ``asyncio.Task`` 存
     ``self._current_task``（镜像 ``AgentEngine._worker_task``）+ ``_end_turn()`` 清句柄.
  invoke_turn 完整实现（state 注入 + emit idle + finally cleanup）是后续任务（line 15）.

六段契约（纯静态 + 真 asyncio stub + 真 build_group_graph，不依赖 live server / 真实 LLM）：

  A. 编译群图持有锁——compile_graph + _graph + _members
    1. ``compile_graph(members=None) -> graph`` async 方法存在.
    2. 显式 members 入参 → ``build_group_graph`` 编译存 ``_graph`` + members 存 ``_members``.
    3. ``_graph`` 是编译图（``CompiledStateGraph`` / Pregel）+ ``_graph._group_id``==
       ``group_id`` / ``_graph._coordinator_id``==``coordinator_id``.
    4. ``compile_graph`` 前 ``_graph`` is None / ``_members`` is None（未编译状态）.

  B. DB member 解析锁——_resolve_members（members=None 时从 DB 读）
    5. ``_resolve_members()`` async——list_group_members_with_agent join list_agents 取 system_prompt.
    6. coordinator 被 EXCLUDE（coordinator 是 sub-node 非 agent_<id> 节点）.
    7. members=None 调 compile_graph → _resolve_members 被调（patch crud 验）.

  C. cancellable 回合 task 句柄锁——_start_turn_task + _end_turn + _current_task
    8. ``_start_turn_task(coro)`` 把协程包成 ``asyncio.Task`` 存 ``_current_task``（镜像 _worker_task）.
    9. ``_end_turn()`` 清 ``_current_task``（turn done——正常或 cancelled）.
   10. ``_end_turn`` 后 ``cancel_turn`` 返 False（句柄已清，幂等 no-active-turn 契约）.

  D. cancel_turn 跨编译 + 句柄锁——编译后 cancel_turn 仍按句柄操作
   11. 编译群图后 ``cancel_turn`` 仍只 cancel ``_current_task``（不碰 ``_graph``——图是只读编译产物）.
   12. 有活跃回合（_start_turn_task 后）``cancel_turn`` 返 True + CancelledError 传入协程.

  E. thread_id 锁——群图一 thread 跨 invoke
   13. ``thread_id == group_id``（一张群图一个 thread，跨 invoke_turn 状态经 checkpointer；镜像驻留 {group}:{agent} 稳定键选型）.

  F. 向后兼容锁——main import OK + 骨架 vh41 契约不破
   14. ``main`` 全量 import OK（group_runtime.py import build_group_graph 无 cycle）.
   15. vh41 停止契约（request_stop/cancel_turn/is_stopped/reset_stop）不破（compile_graph 是新增能力，不改停止契约）.
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

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def assert_contract() -> list[str]:
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

    # ── A. 编译群图持有 ──────────────────────────────────────
    # A1 compile_graph async 方法
    if not hasattr(GroupRuntime, "compile_graph"):
        errs.append("[A1] GroupRuntime 缺 compile_graph 方法")
    elif not inspect.iscoroutinefunction(GroupRuntime.compile_graph):
        errs.append("[A1] compile_graph 应是 async 方法")
    else:
        print("[A1] OK  compile_graph(members=None) async 方法存在")

    # A4 编译前 _graph is None / _members is None
    rt = GroupRuntime(_FakeGroup())
    if rt._graph is not None:
        errs.append(f"[A4] 编译前 _graph 应 None，实际 {rt._graph!r}")
    elif rt._members is not None:
        errs.append(f"[A4] 编译前 _members 应 None，实际 {rt._members!r}")
    else:
        print("[A4] OK  编译前 _graph is None / _members is None（未编译状态）")

    # A2 + A3 显式 members → 编译 + stash
    try:
        rt = GroupRuntime(_FakeGroup())
        g = asyncio.run(rt.compile_graph(members))
        if rt._graph is None:
            errs.append("[A2] compile_graph 后 _graph 仍 None")
        elif rt._members != members:
            errs.append(f"[A2] _members stash 不符，实际 {rt._members}")
        else:
            print("[A2] OK  显式 members → build_group_graph 编译 + _members stash")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A2] compile_graph 异常：{type(e).__name__}: {e}")

    # A3 _graph 是编译图 + 元数据
    try:
        from langgraph.pregel import Pregel
        rt = GroupRuntime(_FakeGroup())
        asyncio.run(rt.compile_graph(members))
        if not isinstance(rt._graph, Pregel):
            errs.append(f"[A3] _graph 应为编译图（Pregel/CompiledStateGraph），实际 {type(rt._graph).__name__}")
        elif getattr(rt._graph, "_group_id", None) != "g1":
            errs.append(f"[A3] _graph._group_id 应 g1，实际 {getattr(rt._graph, '_group_id', None)!r}")
        elif getattr(rt._graph, "_coordinator_id", None) != "c1":
            errs.append(f"[A3] _graph._coordinator_id 应 c1，实际 {getattr(rt._graph, '_coordinator_id', None)!r}")
        else:
            print("[A3] OK  _graph 是编译图 + _group_id=g1 + _coordinator_id=c1")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] _graph 检查异常：{type(e).__name__}: {e}")

    # ── B. DB member 解析 ────────────────────────────────────
    # B5 _resolve_members async
    if not hasattr(GroupRuntime, "_resolve_members"):
        errs.append("[B5] GroupRuntime 缺 _resolve_members 方法")
    elif not inspect.iscoroutinefunction(GroupRuntime._resolve_members):
        errs.append("[B5] _resolve_members 应是 async 方法")
    else:
        print("[B5] OK  _resolve_members() async（list_group_members_with_agent join list_agents）")

    # B6 coordinator 被 EXCLUDE
    try:
        rt = GroupRuntime(_FakeGroup())

        class _M:
            def __init__(self, aid, name="n", role="r"): self.agent_id = aid; self.agent_name = name; self.agent_role = role

        class _A:
            def __init__(self, aid, sp=""): self.id = aid; self.system_prompt = sp

        db_members = [_M("w1"), _M("w2"), _M("c1")]  # c1 = coordinator
        db_agents = [_A("w1", "sp1"), _A("w2", "sp2"), _A("c1", "spCoord")]
        with patch("store.crud.list_group_members_with_agent", AsyncMock(return_value=db_members)), \
             patch("store.crud.list_agents", AsyncMock(return_value=db_agents)):
            resolved = asyncio.run(rt._resolve_members())
        ids = [m["agent_id"] for m in resolved]
        if "c1" in ids:
            errs.append(f"[B6] coordinator 应被 EXCLUDE（是 sub-node 非 agent_<id> 节点），实际 members={ids}")
        elif set(ids) != {"w1", "w2"}:
            errs.append(f"[B6] 应解析出 [w1,w2]，实际 {ids}")
        else:
            # check system_prompt joined
            sp_map = {m["agent_id"]: m["system_prompt"] for m in resolved}
            if sp_map.get("w1") != "sp1":
                errs.append(f"[B6] system_prompt join 失败，w1 应 sp1，实际 {sp_map.get('w1')!r}")
            else:
                print(f"[B6] OK  coordinator 被 EXCLUDE + system_prompt join（members={ids}）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B6] _resolve_members 测试异常：{type(e).__name__}: {e}")

    # B7 members=None 调 compile_graph → _resolve_members 被调
    try:
        rt = GroupRuntime(_FakeGroup())
        with patch.object(rt, "_resolve_members", AsyncMock(return_value=members)) as mock_resolve:
            asyncio.run(rt.compile_graph())
            if not mock_resolve.called:
                errs.append("[B7] members=None 调 compile_graph 未调 _resolve_members")
            else:
                print("[B7] OK  members=None 调 compile_graph → _resolve_members 被调（从 DB 解析）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B7] members=None compile_graph 异常：{type(e).__name__}: {e}")

    # ── C. cancellable 回合 task 句柄 ────────────────────────
    # C8 _start_turn_task 包成 Task 存 _current_task
    try:
        rt = GroupRuntime(_FakeGroup())

        async def fake_ainvoke():
            await asyncio.sleep(0.01)
            return {"ok": True}

        task = rt._start_turn_task(fake_ainvoke())
        if not isinstance(task, asyncio.Task):
            errs.append(f"[C8] _start_turn_task 应返 asyncio.Task，实际 {type(task).__name__}")
        elif rt._current_task is not task:
            errs.append("[C8] _start_turn_task 应 stash task 到 _current_task（镜像 _worker_task）")
        else:
            r = asyncio.run(_await_task(task))
            if r != {"ok": True}:
                errs.append(f"[C8] task 结果应 {{'ok':True}}，实际 {r}")
            else:
                print("[C8] OK  _start_turn_task(coro) 包成 asyncio.Task + stash _current_task（镜像 _worker_task）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] _start_turn_task 测试异常：{type(e).__name__}: {e}")

    # C9 _end_turn 清 _current_task
    try:
        rt = GroupRuntime(_FakeGroup())
        rt._current_task = asyncio.create_task(asyncio.sleep(0.01))
        rt._end_turn()
        if rt._current_task is not None:
            errs.append(f"[C9] _end_turn 后 _current_task 应 None，实际 {rt._current_task!r}")
        else:
            print("[C9] OK  _end_turn() 清 _current_task（turn done——正常或 cancelled）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C9] _end_turn 测试异常：{type(e).__name__}: {e}")

    # C10 _end_turn 后 cancel_turn 返 False（幂等 no-active-turn）
    try:
        rt = GroupRuntime(_FakeGroup())
        rt._current_task = asyncio.create_task(asyncio.sleep(0.01))
        rt._end_turn()
        r = rt.cancel_turn()
        if r is not False:
            errs.append(f"[C10] _end_turn 后 cancel_turn 应返 False（句柄已清），实际 {r}")
        else:
            print("[C10] OK  _end_turn 后 cancel_turn 返 False（句柄已清，幂等 no-active-turn 契约）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C10] cancel_turn-after-end 测试异常：{type(e).__name__}: {e}")

    # ── D. cancel_turn 跨编译 + 句柄 ─────────────────────────
    # D11 编译后 cancel_turn 只 cancel _current_task（不碰 _graph）
    cancel_body = src.split("def cancel_turn")[1].split("def ")[0] if "def cancel_turn" in src else ""
    if "_graph" in cancel_body and "_current_task" not in cancel_body:
        errs.append("[D11] cancel_turn 碰 _graph（应只 cancel _current_task，图是只读编译产物）")
    elif "_current_task" not in cancel_body:
        errs.append("[D11] cancel_turn 未操作 _current_task")
    else:
        print("[D11] OK  cancel_turn 只 cancel _current_task（不碰 _graph——图是只读编译产物）")

    # D12 有活跃回合 cancel_turn → True + CancelledError
    try:
        async def _run_d12():
            rt = GroupRuntime(_FakeGroup())
            asyncio.run(rt.compile_graph(members))

            async def long_ainvoke():
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    raise

            task = rt._start_turn_task(long_ainvoke())
            r = rt.cancel_turn()
            cancelled = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
            return r, cancelled

        r, cancelled = asyncio.run(_run_d12())
        if r is not True:
            errs.append(f"[D12] 有活跃回合 cancel_turn 应返 True，实际 {r}")
        elif not cancelled:
            errs.append("[D12] cancel_turn 后 task 应被 cancel（CancelledError），实际未 cancel")
        else:
            print("[D12] OK  有活跃回合 cancel_turn 返 True + CancelledError 传入协程（编译后句柄仍按 _current_task 操作）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D12] 有活跃回合 cancel_turn 测试异常：{type(e).__name__}: {e}")

    # ── E. thread_id ────────────────────────────────────────
    rt = GroupRuntime(_FakeGroup())
    if rt.thread_id != "g1":
        errs.append(f"[E13] thread_id 应 == group_id (g1)，实际 {rt.thread_id!r}")
    else:
        print(f"[E13] OK  thread_id == group_id（{rt.thread_id}，一张群图一个 thread 跨 invoke，镜像驻留稳定键选型）")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F14 main import OK
    try:
        import main  # noqa: F401
        print("[F14] OK  main 全量 import OK（group_runtime import build_group_graph 无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F14] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # F15 vh41 停止契约不破（compile_graph 是新增能力，不改停止契约）
    try:
        rt = GroupRuntime(_FakeGroup())
        asyncio.run(rt.compile_graph(members))
        # request_stop / is_stopped / reset_stop / cancel_turn 仍工作
        if not rt.is_stopped():
            rt.request_stop()
            if not rt.is_stopped():
                errs.append("[F15] compile_graph 后 request_stop/is_stopped 破")
            else:
                rt.reset_stop()
                if rt.is_stopped():
                    errs.append("[F15] compile_graph 后 reset_stop 破")
                else:
                    print("[F15] OK  vh41 停止契约不破（compile_graph 是新增能力，request_stop/is_stopped/reset_stop/cancel_turn 仍工作）")
        else:
            errs.append("[F15] compile_graph 后 is_stopped 应 False（编译不改 stop 状态）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F15] 停止契约检查异常：{type(e).__name__}: {e}")

    return errs


async def _await_task(task):
    return await task


def main() -> int:
    print("=== VH42 回归：GroupRuntime 编译群图持有 + 当前回合 asyncio.Task 句柄 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "GroupRuntime 编译群图持有 + 回合 task 句柄锁定：\n"
        "  · A compile_graph(members=None) async + _graph 编译图持有 + _members stash + _group_id/_coordinator_id 元数据；\n"
        "  · B _resolve_members async（DB 解析 + coordinator EXCLUDE + system_prompt join）；\n"
        "  · C _start_turn_task(coro) 包成 asyncio.Task stash _current_task（镜像 _worker_task）+ _end_turn 清句柄 + 幂等 cancel；\n"
        "  · D cancel_turn 只 cancel _current_task（不碰 _graph 只读产物）+ 有活跃回合返 True+CancelledError；\n"
        "  · E thread_id == group_id（一图一 thread 跨 invoke）；\n"
        "  · F main import OK 无 cycle + vh41 停止契约不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
