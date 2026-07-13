"""VH41 回归：GroupRuntime 骨架 + stop_event/request_stop/cancel_turn 契约.

锁住 task-13 决策——新建 ``engine/group_runtime.py`` 骨架：``GroupRuntime(group)``
类持 ``self._stop_event = asyncio.Event()``（默认 clear，游离于 GroupState 不进
checkpointer）+ ``request_stop()``（协作式软停）+ ``cancel_turn()``（双层强切兜底，
幂等无活跃回合返回 False）。本任务只写骨架 + 契约 docstring，图编译与 invoke_turn
填实现由后续任务做.

设计真源见 memory ``stop-signal-cooperative-cancel-design``（参考 AutoGen
ExternalTermination：终止做成一等公民且可外部注入，默认协作式非强切）.

六段契约（纯静态 + 真 asyncio stub，不依赖 live server / 真实 LLM）：

  A. 模块 API 锁——GroupRuntime 类 + 构造
    1. ``engine.group_runtime.GroupRuntime`` 类存在.
    2. ``GroupRuntime(group)`` 接受 Group 对象（读 group.id + group.coordinator_id）+
       多态 group_id str（coordinator_id 空，deferred）.
    3. ``_stop_event`` 是 ``asyncio.Event`` 实例，默认 clear（``is_stopped()`` False）.

  B. 协作式软停锁——request_stop
    4. ``request_stop()`` 只 ``_stop_event.set()``，不 cancel（无 ``_current_task.cancel``）.
    5. 调后 ``is_stopped()`` True（route_entry/agent 节点入口将据此 yield，后续任务接线）.
    6. 幂等：重复调 ``request_stop()`` 不报错（set 已 set 的 event 是 no-op）.

  C. 双层强切兜底锁——cancel_turn
    7. ``cancel_turn()`` 先 ``_stop_event.set()`` 再 ``_current_task.cancel()``（双层）.
    8. 无活跃回合（``_current_task is None``）→ 返 ``False``（幂等 no-op，event 仍 set）.
    9. 有活跃回合（``_current_task`` 是真 Task）→ 返 ``True`` + task 被 cancel（CancelledError）.

  D. stop_event 不进 GroupState 锁——游离于图状态外
   10. ``_stop_event`` 是 ``asyncio.Event``（运行时对象，不可序列化），是 GroupRuntime
       实例属性，**不**在 ``GroupState`` 的字段集里（checkpointer 不会序列化它）.
   11. ``_current_task`` + ``_graph`` 也是 GroupRuntime 实例属性（None 默认，后续任务填）.

  E. stop-signal 探查/重置锁——is_stopped + reset_stop
   12. ``is_stopped()`` 返 ``_stop_event.is_set()``（route_entry/agent 节点入口将调此，
       后续任务接线）.
   13. ``reset_stop()`` clear event（invoke_turn 回合开始将调，防 stale stop 抑制新回合）.

  F. 向后兼容锁——main import OK + 不破既有引擎
   14. ``main`` 全量 import OK（group_runtime.py 无 import cycle）.
   15. ``AgentEngine`` / ``AgentRegistry`` 不受影响（GroupRuntime 是新增骨架，不替换驻留引擎）.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

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

    # ── A. 模块 API + 构造 ──────────────────────────────────
    if not isinstance(GroupRuntime, type) or GroupRuntime.__name__ != "GroupRuntime":
        errs.append("[A1] GroupRuntime 类不存在")
        return errs
    print("[A1] OK  engine.group_runtime.GroupRuntime 类存在")

    # A2 Group 对象 + 多态 str
    try:
        rt = GroupRuntime(_FakeGroup())
        if rt.group_id != "g1" or rt.coordinator_id != "c1":
            errs.append(f"[A2] Group 对象入参应 group_id=g1/coordinator_id=c1，实际 {rt.group_id}/{rt.coordinator_id}")
        else:
            rt_str = GroupRuntime("g2")
            if rt_str.group_id != "g2" or rt_str.coordinator_id != "":
                errs.append(f"[A2] group_id str 入参应 group_id=g2/coordinator_id=''，实际 {rt_str.group_id}/{rt_str.coordinator_id!r}")
            else:
                print("[A2] OK  GroupRuntime(group) 接受 Group 对象（读 id+coordinator_id）+ 多态 group_id str（coordinator_id 空 deferred）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A2] 构造异常：{type(e).__name__}: {e}")

    # A3 _stop_event 是 asyncio.Event，默认 clear
    rt = GroupRuntime(_FakeGroup())
    if not isinstance(rt._stop_event, asyncio.Event):
        errs.append(f"[A3] _stop_event 应为 asyncio.Event，实际 {type(rt._stop_event).__name__}")
    elif rt.is_stopped() is not False:
        errs.append(f"[A3] _stop_event 默认应 clear（is_stopped False），实际 {rt.is_stopped()}")
    else:
        print("[A3] OK  _stop_event 是 asyncio.Event，默认 clear（is_stopped False）")

    # ── B. 协作式软停 request_stop ───────────────────────────
    rt = GroupRuntime(_FakeGroup())
    # B4 request_stop 只 set，不 cancel（无 _current_task.cancel 调用）
    request_stop_body = src.split("def request_stop")[1].split("def ")[0] if "def request_stop" in src else ""
    if ".cancel()" in request_stop_body:
        errs.append("[B4] request_stop 体内含 .cancel()（应只 _stop_event.set()，协作式非强切）")
    else:
        # B5 调后 is_stopped True
        rt.request_stop()
        if rt.is_stopped() is not True:
            errs.append(f"[B5] request_stop 后 is_stopped 应 True，实际 {rt.is_stopped()}")
        else:
            # B6 幂等
            try:
                rt.request_stop()
                rt.request_stop()
                print("[B4/B5/B6] OK  request_stop 只 set（不 cancel）+ 调后 is_stopped True + 幂等")
            except Exception as e:  # noqa: BLE001
                errs.append(f"[B6] request_stop 重复调不幂等：{type(e).__name__}: {e}")

    # ── C. 双层强切兜底 cancel_turn ──────────────────────────
    # C7 cancel_turn 先 set 再 cancel（双层）
    cancel_body = src.split("def cancel_turn")[1].split("def ")[0] if "def cancel_turn" in src else ""
    if "_stop_event.set()" not in cancel_body:
        errs.append("[C7] cancel_turn 体内未 _stop_event.set()（双层缺第一层协作式让步）")
    if ".cancel()" not in cancel_body:
        errs.append("[C7] cancel_turn 体内未 .cancel()（双层缺第二层强切）")
    if not any(e.startswith("[C7]") for e in errs):
        print("[C7] OK  cancel_turn 双层：先 _stop_event.set()（协作让步）再 _current_task.cancel()（强切兜底）")

    # C8 无活跃回合 → False（幂等）
    rt = GroupRuntime(_FakeGroup())
    rt._current_task = None
    r = rt.cancel_turn()
    if r is not False:
        errs.append(f"[C8] 无活跃回合 cancel_turn 应返 False，实际 {r}")
    elif not rt.is_stopped():
        errs.append("[C8] 无活跃回合 cancel_turn 后 event 应 set（仍记录 stop 意图），实际未 set")
    else:
        print("[C8] OK  无活跃回合 cancel_turn 返 False（幂等 no-op，event 仍 set）")

    # C9 有活跃回合 → True + task 被 cancel
    try:
        async def _run_c9():
            rt = GroupRuntime(_FakeGroup())

            async def long_task():
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    raise

            rt._current_task = asyncio.create_task(long_task())
            r = rt.cancel_turn()
            cancelled = False
            try:
                await rt._current_task
            except asyncio.CancelledError:
                cancelled = True
            return r, cancelled

        r, cancelled = asyncio.run(_run_c9())
        if r is not True:
            errs.append(f"[C9] 有活跃回合 cancel_turn 应返 True，实际 {r}")
        elif not cancelled:
            errs.append("[C9] 有活跃回合 cancel_turn 后 task 应被 cancel（CancelledError），实际未 cancel")
        else:
            print("[C9] OK  有活跃回合 cancel_turn 返 True + task 被 cancel（CancelledError 传入流式 async for）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C9] 有活跃回合 cancel_turn 测试异常：{type(e).__name__}: {e}")

    # ── D. stop_event 不进 GroupState ────────────────────────
    # D10 _stop_event 是 asyncio.Event（运行时对象，不可序列化）+ 是实例属性非 GroupState 字段
    rt = GroupRuntime(_FakeGroup())
    if not isinstance(rt._stop_event, asyncio.Event):
        errs.append("[D10] _stop_event 非 asyncio.Event（应运行时对象，不进 checkpointer）")
    else:
        from typing import get_type_hints
        from engine.state import GroupState
        gs_hints = set(get_type_hints(GroupState, include_extras=True).keys())
        # asyncio.Event / _stop_event 不应在 GroupState 字段集里（checkpointer 不会序列化它）
        if "_stop_event" in gs_hints or "stop_event" in gs_hints:
            errs.append("[D10] _stop_event 不应在 GroupState 字段集里（asyncio.Event 不可序列化，进 checkpointer 会报错）")
        else:
            print("[D10] OK  _stop_event 是 asyncio.Event（实例属性，游离于 GroupState 不进 checkpointer）")

    # D11 _current_task + _graph 是实例属性（None 默认）
    rt = GroupRuntime(_FakeGroup())
    if rt._current_task is not None:
        errs.append(f"[D11] _current_task 默认应 None（后续 invoke_turn 填），实际 {rt._current_task}")
    elif rt._graph is not None:
        errs.append(f"[D11] _graph 默认应 None（后续编译任务填），实际 {rt._graph}")
    else:
        print("[D11] OK  _current_task + _graph 是实例属性（None 默认，后续任务填）")

    # ── E. is_stopped + reset_stop ───────────────────────────
    rt = GroupRuntime(_FakeGroup())
    # E12 is_stopped 返 _stop_event.is_set()
    if not inspect.ismethod(rt.is_stopped):
        errs.append("[E12] is_stopped 不可调用")
    elif rt.is_stopped() != rt._stop_event.is_set():
        errs.append("[E12] is_stopped 应返 _stop_event.is_set()，实际不符")
    else:
        rt.request_stop()
        if not rt.is_stopped():
            errs.append("[E12] request_stop 后 is_stopped 应 True")
        else:
            print("[E12] OK  is_stopped() 返 _stop_event.is_set()（route_entry/agent 节点入口将调此）")

    # E13 reset_stop clear event
    rt = GroupRuntime(_FakeGroup())
    rt.request_stop()
    if not inspect.ismethod(rt.reset_stop):
        errs.append("[E13] reset_stop 不可调用")
    else:
        rt.reset_stop()
        if rt.is_stopped():
            errs.append("[E13] reset_stop 后 is_stopped 应 False（clear event）")
        else:
            print("[E13] OK  reset_stop() clear event（invoke_turn 回合开始将调，防 stale stop 抑制新回合）")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F14 main import OK（无 import cycle）
    try:
        import main  # noqa: F401
        print("[F14] OK  main 全量 import OK（group_runtime.py 无 import cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F14] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # F15 AgentEngine / AgentRegistry 不受影响
    try:
        from engine.registry import AgentEngine, AgentRegistry, registry  # type: ignore
        if not (isinstance(AgentEngine, type) and isinstance(AgentRegistry, type)):
            errs.append("[F15] AgentEngine/AgentRegistry 类缺失（GroupRuntime 骨架不应破驻留引擎）")
        else:
            print("[F15] OK  AgentEngine/AgentRegistry 不受影响（GroupRuntime 是新增骨架，不替换驻留引擎）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F15] AgentEngine 导入异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH41 回归：GroupRuntime 骨架 + stop_event/request_stop/cancel_turn 契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "GroupRuntime 骨架 + stop 契约锁定：\n"
        "  · A GroupRuntime(group) 类 + Group 对象/多态 str 构造 + _stop_event=asyncio.Event(默认 clear)；\n"
        "  · B request_stop() 只 set（不 cancel）+ 调后 is_stopped True + 幂等；\n"
        "  · C cancel_turn() 双层（先 set 再 cancel）+ 无活跃回合返 False（幂等）+ 有活跃回合返 True+CancelledError；\n"
        "  · D _stop_event 是 asyncio.Event 实例属性（游离于 GroupState 不进 checkpointer）+ _current_task/_graph None 默认；\n"
        "  · E is_stopped() + reset_stop()（invoke_turn 回合开始将调 reset_stop 防 stale stop）；\n"
        "  · F main import OK 无 import cycle + AgentEngine/AgentRegistry 不受影响。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
