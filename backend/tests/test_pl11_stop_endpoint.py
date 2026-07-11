"""PL-11 tasks.py POST /api/tasks/{id}/stop 单元自测（不依赖 pytest / 在线后端）。

用 FastAPI TestClient + mock registry/inbox 校验：
  1. 路由 POST /api/tasks/{id}/stop 已注册，方法 POST。
  2. stop_task 签名带 Query groupId（可选），FastAPI 能正确注入。
  3. executing 任务：registry.stop_task_by_id 返回 cancelled=True → 响应 executing=True。
  4. queued 任务：cancel_task 返回 item → 响应 queued=True。
  5. 既不 executing 也不 queued（已完成/未知）→ executing=False queued=False，message 提示可能已完成（no-op 非错误）。
  6. groupId 可选（不传也能匹配，tq_ 全局唯一）。
"""
from __future__ import annotations

import asyncio
import sys

# --- mocks BEFORE importing api.tasks ---
import engine.registry as registry_mod
import engine.inbox as inbox_mod
from engine.registry import registry as _registry_instance

# fake task item shape returned by inbox.cancel_task
_FAKE_ITEM = {
    "id": "tq_queued",
    "group_id": "group_demo_1",
    "status": "cancelled",
    "completed_at": "2026-07-11T00:00:00Z",
}


async def _fake_stop_by_id(task_id, group_id=None):
    """Mock registry.stop_task_by_id — returns executing match for tq_exec."""
    if task_id == "tq_exec":
        return {"cancelled": True, "group_id": "group_demo_1", "agent_id": "agent_backend_1"}
    return {"cancelled": False, "group_id": None, "agent_id": None}


async def _fake_cancel_task(task_id):
    """Mock inbox.cancel_task — returns item for tq_queued, None otherwise."""
    if task_id == "tq_queued":
        return dict(_FAKE_ITEM)
    return None


# monkeypatch the instance method (api.tasks calls registry.stop_task_by_id)
_registry_instance.stop_task_by_id = _fake_stop_by_id
# monkeypatch the module-level function (api.tasks imports cancel_task by name)
inbox_mod.cancel_task = _fake_cancel_task

# now import api.tasks — it binds registry + cancel_task by name
import api.tasks as tasks_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(tasks_api.router)
client = TestClient(app)


def check_routes() -> None:
    paths = {r.path: list(r.methods) for r in tasks_api.router.routes}
    assert "/api/tasks/{task_id}/stop" in paths, "stop route missing"
    assert "POST" in paths["/api/tasks/{task_id}/stop"], "stop not POST"
    print("[check 1] route POST /api/tasks/{id}/stop registered  OK")


def test_executing() -> None:
    r = client.post("/api/tasks/tq_exec/stop")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["executing"] is True, body
    assert body["queued"] is False, body
    assert body["group_id"] == "group_demo_1"
    assert body["agent_id"] == "agent_backend_1"
    assert "执行中已中断" in body["message"], body
    print("[check 2] executing task → executing=True, message 含「执行中已中断」  OK")


def test_queued() -> None:
    r = client.post("/api/tasks/tq_queued/stop")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executing"] is False, body
    assert body["queued"] is True, body
    assert "队列中已标记跳过" in body["message"], body
    print("[check 3] queued task → queued=True, message 含「队列中已标记跳过」  OK")


def test_noop() -> None:
    r = client.post("/api/tasks/tq_ghost/stop")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executing"] is False, body
    assert body["queued"] is False, body
    assert "可能已完成" in body["message"], body
    print("[check 4] unknown/finished task → no-op 200, message 含「可能已完成」  OK")


def test_group_id_optional() -> None:
    # 带 groupId 参数也能跑通（query 注入不报错）
    r = client.post("/api/tasks/tq_exec/stop?groupId=group_demo_1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executing"] is True, body
    print("[check 5] groupId 可选参数注入正常  OK")


def main() -> int:
    print("=== PL-11 tasks.py POST /api/tasks/{id}/stop 单元自测 ===")
    check_routes()
    test_executing()
    test_queued()
    test_noop()
    test_group_id_optional()
    print("\n=== 结果: PASS ===")
    print("POST /api/tasks/{id}/stop 端点：")
    print("  · executing 任务 → registry.stop_task_by_id cancel _worker_task；")
    print("  · queued 任务 → inbox.cancel_task 打 cancelled 标记；")
    print("  · 两者都尝试（互补，互不抛错），响应分别报告 executing/queued；")
    print("  · 已完成/未知 task → 200 no-op（非错误，不与自然完成竞态）；")
    print("  · groupId 可选（tq_ 全局唯一）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
