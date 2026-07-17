"""VH41 回归：GroupRuntime 骨架 + cancel_turn 契约（Option B·③ 删软停件后）.

锁住 Option B·③ 决策——删除底层软停机制（``request_stop`` / ``is_stopped`` /
``reset_stop`` / ``_stop_event``），``cancel_turn`` 简化为纯 ``task.cancel``。
理由：``request_stop`` 唯一生产调用方是已删的停关键词入站（Option B·①），删后软停三件
零生产调用方 = 死代码；``cancel_turn`` 的 ``task.cancel`` 已覆盖 fan-out 兄弟节点窗口
（CancelledError 传播），删 event 不回退。

停止只剩两入口（Option B 后）：
  · ``cancel_turn()`` 硬切（纯 ``task.cancel`` mid-stream 断流）；
  · ``SESSION_SPEECH_CAP=50`` 跨回合封顶（``is_session_capped`` / ``record_speech``）。

注意 contextvar ``worker.set_group_runtime`` / ``get_group_runtime`` 保留（record_speech /
is_session_capped 还用）。

设计真源见 memory ``converge-turn-design`` + ``stop-signal-cooperative-cancel-design``.

六段契约（纯静态 + 真 asyncio stub，不依赖 live server / 真实 LLM）：

  A. 模块 API 锁——GroupRuntime 类 + 构造
    1. ``engine.group_runtime.GroupRuntime`` 类存在.
    2. ``GroupRuntime(group)`` 接受 Group 对象（读 group.id + group.coordinator_id）+
       多态 group_id str（coordinator_id 空，deferred）.
    3. GroupRuntime **无** ``_stop_event`` 属性（Option B·③ 删）+ **无** ``request_stop`` /
       ``is_stopped`` / ``reset_stop`` 方法.

  B. 软停件已删锁——request_stop/is_stopped/reset_stop 不存在
    4. ``request_stop`` 方法已删（不再是 GroupRuntime 方法）.
    5. ``is_stopped`` 方法已删.
    6. ``reset_stop`` 方法已删.

  C. cancel_turn 简化为纯 task.cancel 锁
    7. ``cancel_turn()`` 体内**无** ``_stop_event.set()``（删了「先 set 再 cancel」前半句），
       只留 ``_current_task.cancel()`` 强切.
    8. 无活跃回合（``_current_task is None``）→ 返 ``False``（幂等 no-op）.
    9. 有活跃回合（``_current_task`` 是真 Task）→ 返 ``True`` + task 被 cancel（CancelledError）.

  D. 软停件引用已清锁——invoke_turn/resume_plan 不调 reset_stop
   10. ``invoke_turn`` 体内**无** ``self.reset_stop()`` 调用（删了 per-turn reset）.
   11. ``resume_plan`` 体内**无** ``self.reset_stop()`` 调用.

  E. 50 封顶保留锁——is_session_capped/record_speech 仍在
   12. ``is_session_capped()`` / ``record_speech()`` / ``SESSION_SPEECH_CAP`` 仍在（停的兜底入口之一）.
   13. contextvar ``worker.set_group_runtime`` / ``get_group_runtime`` 仍在（record_speech/cap 用）.

  F. 向后兼容锁——main import OK + 不破既有引擎
   14. ``main`` 全量 import OK（group_runtime.py 删软停件无 import cycle）.
   15. ``AgentEngine`` / ``AgentRegistry`` 不受影响.
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


def _fn_body(src: str, fn_name: str) -> str:
    """Return one method body (def ... to next def at column 0)."""
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


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

    # A3 GroupRuntime 无 _stop_event 属性 + 无 request_stop/is_stopped/reset_stop 方法
    rt = GroupRuntime(_FakeGroup())
    has_stop_event = hasattr(rt, "_stop_event")
    if has_stop_event:
        errs.append("[A3] GroupRuntime 仍有 _stop_event 属性（Option B·③ 应删）")
    if hasattr(rt, "request_stop"):
        errs.append("[A3] GroupRuntime 仍有 request_stop 方法（Option B·③ 应删）")
    if hasattr(rt, "is_stopped"):
        errs.append("[A3] GroupRuntime 仍有 is_stopped 方法（Option B·③ 应删）")
    if hasattr(rt, "reset_stop"):
        errs.append("[A3] GroupRuntime 仍有 reset_stop 方法（Option B·③ 应删）")
    if not any(e.startswith("[A3]") for e in errs):
        print("[A3] OK  GroupRuntime 无 _stop_event/request_stop/is_stopped/reset_stop（Option B·③ 删软停件）")

    # ── B. 软停件已删（request_stop/is_stopped/reset_stop 不存在）──
    # B4/B5/B6 三方法已删
    if "def request_stop" in src:
        errs.append("[B4] request_stop 仍定义在源码（Option B·③ 应删）")
    if "def is_stopped" in src:
        errs.append("[B5] is_stopped 仍定义在源码（Option B·③ 应删）")
    if "def reset_stop" in src:
        errs.append("[B6] reset_stop 仍定义在源码（Option B·③ 应删）")
    if not any(e.startswith("[B4]") or e.startswith("[B5]") or e.startswith("[B6]") for e in errs):
        print("[B4/B5/B6] OK  request_stop/is_stopped/reset_stop 三方法已删（死代码清理）")

    # ── C. cancel_turn 简化为纯 task.cancel ──────────────────
    # C7 cancel_turn 体内无 _stop_event.set()（删了前半句），只留 .cancel()
    cancel_body = src.split("def cancel_turn")[1].split("def ")[0] if "def cancel_turn" in src else ""
    if "_stop_event.set()" in cancel_body:
        errs.append("[C7] cancel_turn 体内仍有 _stop_event.set()（Option B·③ 应删前半句，纯 task.cancel）")
    if ".cancel()" not in cancel_body:
        errs.append("[C7] cancel_turn 体内无 .cancel()（应留 task.cancel 强切）")
    if not any(e.startswith("[C7]") for e in errs):
        print("[C7] OK  cancel_turn 简化为纯 task.cancel（删了 _stop_event.set() 前半句）")

    # C8 无活跃回合 → False（幂等）
    rt = GroupRuntime(_FakeGroup())
    rt._current_task = None
    r = rt.cancel_turn()
    if r is not False:
        errs.append(f"[C8] 无活跃回合 cancel_turn 应返 False，实际 {r}")
    else:
        print("[C8] OK  无活跃回合 cancel_turn 返 False（幂等 no-op）")

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

    # ── D. 软停件引用已清（invoke_turn/resume_plan 不调 reset_stop）──
    invoke_body = _fn_body(src, "invoke_turn")
    resume_body = _fn_body(src, "resume_plan")
    if "self.reset_stop()" in invoke_body:
        errs.append("[D10] invoke_turn 体内仍有 self.reset_stop()（Option B·③ 应删 per-turn reset）")
    else:
        print("[D10] OK  invoke_turn 不调 reset_stop（删了 per-turn reset）")
    if "self.reset_stop()" in resume_body:
        errs.append("[D11] resume_plan 体内仍有 self.reset_stop()（Option B·③ 应删）")
    else:
        print("[D11] OK  resume_plan 不调 reset_stop")

    # ── E. 50 封顶保留（is_session_capped/record_speech 仍在）──
    from engine.group_runtime import SESSION_SPEECH_CAP  # type: ignore
    if not hasattr(GroupRuntime, "is_session_capped"):
        errs.append("[E12] is_session_capped 缺失（50 封顶应保留）")
    if not hasattr(GroupRuntime, "record_speech"):
        errs.append("[E12] record_speech 缺失（50 封顶计数应保留）")
    if not isinstance(SESSION_SPEECH_CAP, int) or SESSION_SPEECH_CAP < 1:
        errs.append(f"[E12] SESSION_SPEECH_CAP 应 >=1 int，实际 {SESSION_SPEECH_CAP!r}")
    # E13 contextvar set/get_group_runtime 仍在（record_speech/cap 用）
    from engine import worker as worker_mod  # type: ignore
    if not hasattr(worker_mod, "set_group_runtime") or not hasattr(worker_mod, "get_group_runtime"):
        errs.append("[E13] worker 缺 set_group_runtime/get_group_runtime（record_speech/cap 还用，应保留）")
    else:
        print("[E12/E13] OK  SESSION_SPEECH_CAP + is_session_capped + record_speech 仍在 + contextvar set/get_group_runtime 保留")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F14 main import OK（无 import cycle）
    try:
        import main  # noqa: F401
        print("[F14] OK  main 全量 import OK（group_runtime.py 删软停件无 import cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F14] main import 异常（import cycle？）：{type(e).__name__}: {e}")

    # F15 AgentEngine / AgentRegistry 不受影响
    try:
        from engine.registry import AgentEngine, AgentRegistry, registry  # type: ignore
        if not (isinstance(AgentEngine, type) and isinstance(AgentRegistry, type)):
            errs.append("[F15] AgentEngine/AgentRegistry 类缺失（删软停件不应破驻留引擎）")
        else:
            print("[F15] OK  AgentEngine/AgentRegistry 不受影响")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F15] AgentEngine 导入异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH41 回归：GroupRuntime 骨架 + cancel_turn 契约（Option B·③ 删软停件后）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "GroupRuntime 骨架 + cancel_turn 契约锁定（Option B·③ 删软停件后）：\n"
        "  · A GroupRuntime(group) 类 + Group 对象/多态 str 构造 + 无 _stop_event/request_stop/is_stopped/reset_stop；\n"
        "  · B request_stop/is_stopped/reset_stop 三方法已删（死代码清理）；\n"
        "  · C cancel_turn 简化为纯 task.cancel（删 _stop_event.set() 前半句）+ 无活跃返 False + 有活跃返 True+CancelledError；\n"
        "  · D invoke_turn/resume_plan 不调 reset_stop（删 per-turn reset）；\n"
        "  · E SESSION_SPEECH_CAP + is_session_capped + record_speech 仍在 + contextvar set/get_group_runtime 保留；\n"
        "  · F main import OK 无 cycle + AgentEngine/AgentRegistry 不受影响。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
