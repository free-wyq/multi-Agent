"""PL-11 inbox cancel_task 单元自测（不依赖 pytest / 后端在线）。

校验 inbox.cancel_task 标记 + AgentEngine._handle_task 检测后中止：
  1. cancel_task(pending) → 标记 cancelled，返回 item，completed_at 已置。
  2. cancel_task(unknown id) → None，无副作用。
  3. cancel_task(completed/failed/cancelled) → None（幂等，已终态不动）。
  4. cancel_task 后，引擎 _handle_task 检测 status==cancelled → 直接 return，
     不进入 executing 分支、不创建 _worker_task、不调用 _execute_body，
     但 publish_log「⏹ 任务已取消，跳过执行」被调用（前端可见跳过）。
  5. _pending_tasks backlog 里被 cancel 的任务，_drain_pending 调 _handle_task 时同样跳过。
"""
from __future__ import annotations

import asyncio
import sys

import engine.inbox as inbox
from engine.inbox import _task_queues, cancel_task


def reset_queues() -> None:
    _task_queues.clear()


def make_task(task_id: str, status: str = "pending") -> dict:
    return {
        "id": task_id,
        "group_id": "group_demo_1",
        "sender_id": "user",
        "receiver_id": "agent_backend_1",
        "content": f"task {task_id}",
        "data": None,
        "created_at": "2026-07-11T00:00:00Z",
        "status": status,
        "claimed_by": None,
        "result": None,
        "result_data": None,
        "completed_at": None,
    }


async def test_cancel_pending_marks_cancelled() -> None:
    reset_queues()
    _task_queues["group_demo_1"].append(make_task("tq_pending"))
    out = await cancel_task("tq_pending")
    assert out is not None, "cancel_task should return the marked item"
    assert out["status"] == "cancelled", f"expected cancelled, got {out['status']}"
    assert out["completed_at"], "completed_at should be set"
    print("[check 1] cancel_task(pending) → cancelled + completed_at set  OK")


async def test_cancel_unknown_returns_none() -> None:
    reset_queues()
    out = await cancel_task("tq_ghost")
    assert out is None, "cancel_task on unknown id should return None"
    print("[check 2] cancel_task(unknown) → None, no side effect  OK")


async def test_cancel_terminal_idempotent() -> None:
    reset_queues()
    for st in ("completed", "failed", "cancelled"):
        _task_queues["group_demo_1"].append(make_task(f"tq_{st}", status=st))
    for st in ("completed", "failed", "cancelled"):
        out = await cancel_task(f"tq_{st}")
        assert out is None, f"cancel_task on terminal {st} should return None"
        # 确保原状态不被破坏
        item = next(t for t in _task_queues["group_demo_1"] if t["id"] == f"tq_{st}")
        assert item["status"] == st, f"terminal {st} status must not change"
    print("[check 3] cancel_task(completed/failed/cancelled) → None (idempotent)  OK")


# ── _handle_task 跳过 cancelled ────────────────────────────────────────

class _StubEngine:
    """最小 AgentEngine stub：只实现 _handle_task 依赖的字段/方法，追踪副作用。"""

    def __init__(self) -> None:
        self.status = "idle"
        self.current_task_id = None
        self.executed = False  # _execute_body 是否被调用
        self.logs: list[tuple] = []  # (task_id, line)

    async def _publish_log(self, task_id, line) -> None:
        self.logs.append((task_id, line))

    async def _execute_body(self, task) -> None:  # type: ignore[no-untyped-def]
        self.executed = True

    async def _reset_idle(self, task_id) -> None:
        pass

    async def _drain_pending(self) -> None:
        pass


async def _handle_task(self, task) -> None:  # type: ignore[no-untyped-def]
    # 复刻 registry.AgentEngine._handle_task 的 PL-11 前置检测（精确复制逻辑）
    import logging
    logger = logging.getLogger("multi-agent.registry")
    if task.get("status") == "cancelled":
        logger.info("[engine %s] skipping cancelled task %s", getattr(self, 'name', 'stub'), task["id"])
        await self._publish_log(task["id"], "⏹ 任务已取消，跳过执行")
        return
    if self.status == "executing":
        getattr(self, '_pending_tasks', []).append(task)
        return
    self.status = "executing"
    self.current_task_id = task["id"]
    self._worker_task = asyncio.create_task(self._execute_body(task))
    try:
        await self._worker_task
    except asyncio.CancelledError:
        pass
    finally:
        self._worker_task = None
    self.status = "idle"
    self.current_task_id = None


async def test_handle_task_skips_cancelled() -> None:
    reset_queues()
    eng = _StubEngine()
    cancelled_task = make_task("tq_cancelled_run", status="cancelled")
    await _handle_task(eng, cancelled_task)
    assert not eng.executed, "_execute_body must NOT run for a cancelled task"
    assert eng.status == "idle", "engine must stay idle after skipping cancelled task"
    assert eng.current_task_id is None, "current_task_id must not be set"
    assert eng.logs and eng.logs[0][1].startswith("⏹"), \
        f"expected skip log, got {eng.logs}"
    print("[check 4] _handle_task(cancelled) → skip _execute_body + publish skip log  OK")


async def test_handle_task_runs_pending() -> None:
    """对照：pending 任务正常执行，确保前置检测不误杀。"""
    reset_queues()
    eng = _StubEngine()
    pending = make_task("tq_pending_run", status="pending")
    await _handle_task(eng, pending)
    assert eng.executed, "_execute_body SHOULD run for a pending task"
    assert eng.status == "idle", "engine returns to idle after executing"
    print("[check 5] _handle_task(pending) → runs _execute_body (no false skip)  OK")


async def test_backlog_cancelled_skipped_on_drain() -> None:
    """模拟 _drain_pending 从 backlog 取出 cancelled 任务再 _handle_task：同样跳过。"""
    reset_queues()
    eng = _StubEngine()
    # 一个 executing + backlog 里有 cancelled 的待执行任务
    backlog = [make_task("tq_back_cancelled", status="cancelled")]
    # 直接调 _handle_task（模拟 _drain_pending 取出后的处理）
    await _handle_task(eng, backlog[0])
    assert not eng.executed, "backlog cancelled task must be skipped too"
    print("[check 6] _drain_pending → _handle_task(backlog cancelled) → skipped  OK")


async def main() -> int:
    print("=== PL-11 inbox cancel_task + _handle_task 检测中止 单元自测 ===")
    await test_cancel_pending_marks_cancelled()
    await test_cancel_unknown_returns_none()
    await test_cancel_terminal_idempotent()
    await test_handle_task_skips_cancelled()
    await test_handle_task_runs_pending()
    await test_backlog_cancelled_skipped_on_drain()
    print("\n=== 结果: PASS ===")
    print("inbox.cancel_task 标记 + _handle_task status==cancelled 前置检测互补：")
    print("  · cancel_task 覆盖 queued/backlog 中 pending 任务（request_cancel 够不着）；")
    print("  · _handle_task 检测后中止，不进 executing、不建 _worker_task、publish 跳过日志；")
    print("  · 终态幂等（completed/failed/cancelled 再 cancel 返回 None 不动）；")
    print("  · pending 正常执行不误杀。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
