"""VH45 回归：POST /api/groups/{group_id}/stop-turn 端点 → GroupRuntime.cancel_turn.

锁住 task-23 决策——新增 backend/api 端点 ``POST /api/groups/{group_id}/stop-turn``
→ ``GroupRuntime.cancel_turn``，返回 ``{ok, message}``，cancel 后 emit
``agent_status(idle)`` 让 UI 自动更新.

设计真源见 memory ``stop-signal-cooperative-cancel-design``（双层停止·cancel_turn 是
硬停兜底：先 set event 协作让步，再 task.cancel 强切断流）+ ``group-runtime-skeleton``.

本任务锁三件：
  1. 路由 ``POST /api/groups/{group_id}/stop-turn`` 已注册（方法 POST）。
  2. 有活跃回合 → cancel_turn 返 True + 响应 ``{ok, message, cancelled=True}`` +
     emit agent_status(idle)（让 UI 自动更新）。
  3. 无活跃回合（cold/no-runtime/no-active-turn）→ cancelled=False + no-op 200
     （不与自然完成竞态），idle emit 仍发（runtime 存在时）/ 跳过（runtime None）。

六段契约（FastAPI TestClient + mock registry/cancel_turn/emit，不依赖 live server / 真实 LLM）：

  A. 路由注册锁
    1. ``POST /api/groups/{group_id}/stop-turn`` 路由已注册 + 方法 POST。

  B. 有活跃回合锁——cancel_turn True → cancelled=True + emit idle
    2. cancel_turn 返 True → 响应 cancelled=True + message 含「执行中已中断」.
    3. emit_agent_status 被调一次，status=idle，agent_id=coordinator_id.
    4. response 字段：ok=True / group_id / cancelled / message（{ok, message} 契约）.

  C. 无活跃回合锁——cancel_turn False → cancelled=False + no-op 200
    5. cancel_turn 返 False → 响应 cancelled=False + message 含「无活跃回合」.
    6. 无活跃回合仍 emit idle（runtime 存在时——让 UI 状态机归位）.

  D. runtime None 锁——ensure_runtime 返 None → no-op 200
    7. ensure_runtime 返 None（群组未运行/gone）→ cancelled=False + 不 emit +
       message 含「无活跃回合」.

  E. 与 PL-11 stop-task 区分锁
    8. stop-turn 调 GroupRuntime.cancel_turn（不经 registry.stop_task_by_id /
       request_cancel——那是驻留引擎 per-task 路径）.

  F. 向后兼容锁——main import OK + 既有路由不破
    9. ``main`` 全量 import OK（groups.py 加 emit_agent_status 导入无 cycle）.
   10. reset-session / files / files/{name} 等既有路由仍在.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# --- mocks BEFORE importing api.groups ---
import api.groups as groups_api  # noqa: E402
import engine.registry as registry_mod  # noqa: E402
from engine.registry import registry as _registry_instance  # noqa: E402

from fastapi import FastAPI  # noqa: E402


class _AsyncClient:
    """httpx.AsyncClient + ASGITransport adapter for route testing.

    starlette 0.27's ``TestClient`` passes ``app=`` to ``httpx.Client``, but
    httpx 0.28 removed that kwarg (it errors ``got an unexpected keyword
    argument 'app'``). Build the transport explicitly + drive the ASGI app
    through an ``AsyncClient`` so the route contract runs without a live server
    (mirrors ``test_pl11_stop_endpoint``'s intent, adapted to the installed
    httpx). All calls are ``await``-ed inside an event loop.
    """

    def __init__(self, app):
        import httpx
        self._app = app
        self._transport = httpx.ASGITransport(app=app)
        self._client = httpx.AsyncClient(transport=self._transport, base_url="http://testserver")

    def post(self, path, **kw):
        return asyncio.get_event_loop().run_until_complete(self._client.post(path, **kw))

    def get(self, path, **kw):
        return asyncio.get_event_loop().run_until_complete(self._client.get(path, **kw))


def _make_app():
    app = FastAPI()
    app.include_router(groups_api.router)
    return app


class _FakeRuntime:
    """Stand-in GroupRuntime: records cancel_turn calls + has a coordinator_id."""

    def __init__(self, group_id="group_demo_1", coordinator_id="c1", cancel_ret=True):
        self.group_id = group_id
        self.coordinator_id = coordinator_id
        self._cancel_ret = cancel_ret
        self.cancel_calls = 0

    def cancel_turn(self) -> bool:
        self.cancel_calls += 1
        return self._cancel_ret


def assert_contract() -> list[str]:
    errs: list[str] = []
    client = _AsyncClient(_make_app())

    # ── A. 路由注册 ──────────────────────────────────────────
    # A1 POST /api/groups/{group_id}/stop-turn registered + POST
    paths = {r.path: list(r.methods) for r in groups_api.router.routes}
    if "/api/groups/{group_id}/stop-turn" not in paths:
        errs.append(f"[A1] 路由 /api/groups/{{group_id}}/stop-turn 未注册，实际 paths={list(paths)}")
    elif "POST" not in paths["/api/groups/{group_id}/stop-turn"]:
        errs.append(f"[A1] stop-turn 应 POST，实际 {paths['/api/groups/{group_id}/stop-turn']}")
    else:
        print("[A1] OK  路由 POST /api/groups/{group_id}/stop-turn 已注册")

    # ── B. 有活跃回合 ────────────────────────────────────────
    # B2/B3/B4 cancel_turn True → cancelled=True + emit idle + response fields
    fake_rt = _FakeRuntime("group_demo_1", "c1", cancel_ret=True)
    emit_calls: list[tuple] = []

    async def _fake_ensure(group_id):
        return fake_rt

    async def _fake_get_agent(agent_id):
        # return a fake agent with name=协调者 for the leader
        m = MagicMock()
        m.name = "协调者"
        m.id = agent_id
        return m

    async def _fake_emit_agent_status(group_id, agent_id, agent_name, status, task_id):
        emit_calls.append((group_id, agent_id, agent_name, status, task_id))

    orig_ensure = _registry_instance.ensure_runtime
    orig_get_agent = groups_api.crud.get_agent
    orig_emit = groups_api.emit_agent_status
    _registry_instance.ensure_runtime = _fake_ensure
    groups_api.crud.get_agent = _fake_get_agent
    groups_api.emit_agent_status = _fake_emit_agent_status
    try:
        r = client.post("/api/groups/group_demo_1/stop-turn")
        if r.status_code != 200:
            errs.append(f"[B2] 有活跃回合 stop-turn 应 200，实际 {r.status_code}: {r.text}")
        else:
            body = r.json()
            if body.get("cancelled") is not True:
                errs.append(f"[B2] cancel_turn=True 应 cancelled=True，实际 {body}")
            elif "执行中已中断" not in body.get("message", ""):
                errs.append(f"[B2] message 应含「执行中已中断」，实际 {body.get('message')!r}")
            else:
                print(f"[B2] OK  有活跃回合 → cancelled=True + message 含「执行中已中断」（cancel 调 {fake_rt.cancel_calls} 次）")
            # B3 emit idle once, agent_id=coordinator
            idle_emits = [e for e in emit_calls if e[3] == "idle"]
            if len(idle_emits) != 1:
                errs.append(f"[B3] 应 emit 1 次 idle，实际 {len(idle_emits)} 次（emit_calls={emit_calls}）")
            elif idle_emits[0][1] != "c1":
                errs.append(f"[B3] emit idle 的 agent_id 应 coordinator=c1，实际 {idle_emits[0][1]!r}")
            elif idle_emits[0][2] != "协调者":
                errs.append(f"[B3] emit idle 的 agent_name 应「协调者」，实际 {idle_emits[0][2]!r}")
            else:
                print("[B3] OK  emit agent_status(idle) 一次（agent_id=c1 / agent_name=协调者）")
            # B4 response fields {ok, message} contract
            if body.get("ok") is not True:
                errs.append(f"[B4] response.ok 应 True，实际 {body.get('ok')!r}")
            elif "message" not in body:
                errs.append(f"[B4] response 应含 message，实际 {body}")
            elif body.get("group_id") != "group_demo_1":
                errs.append(f"[B4] response.group_id 应 group_demo_1，实际 {body.get('group_id')!r}")
            else:
                print("[B4] OK  response {ok, message, cancelled, group_id} 字段契约（{ok, message} 子集 + cancelled/group_id）")
    finally:
        _registry_instance.ensure_runtime = orig_ensure
        groups_api.crud.get_agent = orig_get_agent
        groups_api.emit_agent_status = orig_emit

    # ── C. 无活跃回合 ────────────────────────────────────────
    # C5/C6 cancel_turn False → cancelled=False + no-op 200 + 仍 emit idle
    fake_rt2 = _FakeRuntime("group_demo_1", "c1", cancel_ret=False)
    emit_calls2: list[tuple] = []

    async def _fake_ensure2(group_id):
        return fake_rt2

    async def _fake_emit2(group_id, agent_id, agent_name, status, task_id):
        emit_calls2.append((group_id, agent_id, agent_name, status, task_id))

    _registry_instance.ensure_runtime = _fake_ensure2
    groups_api.crud.get_agent = _fake_get_agent
    groups_api.emit_agent_status = _fake_emit2
    try:
        r = client.post("/api/groups/group_demo_1/stop-turn")
        if r.status_code != 200:
            errs.append(f"[C5] 无活跃回合 stop-turn 应 200 no-op，实际 {r.status_code}")
        else:
            body = r.json()
            if body.get("cancelled") is not False:
                errs.append(f"[C5] cancel_turn=False 应 cancelled=False，实际 {body}")
            elif "无活跃回合" not in body.get("message", ""):
                errs.append(f"[C5] message 应含「无活跃回合」，实际 {body.get('message')!r}")
            else:
                print(f"[C5] OK  无活跃回合 → cancelled=False + message 含「无活跃回合」（no-op 200，不与自然完成竞态）")
            # C6 无活跃回合仍 emit idle（runtime 存在——让 UI 状态机归位）
            idle_emits2 = [e for e in emit_calls2 if e[3] == "idle"]
            if len(idle_emits2) != 1:
                errs.append(f"[C6] 无活跃回合仍应 emit idle 1 次（runtime 存在·UI 归位），实际 {len(idle_emits2)}（{emit_calls2}）")
            else:
                print("[C6] OK  无活跃回合仍 emit idle（runtime 存在·让 UI 状态机归位）")
    finally:
        _registry_instance.ensure_runtime = orig_ensure
        groups_api.crud.get_agent = orig_get_agent
        groups_api.emit_agent_status = orig_emit

    # ── D. runtime None ─────────────────────────────────────
    # D7 ensure_runtime 返 None → cancelled=False + 不 emit + message 含「无活跃回合」
    emit_calls3: list[tuple] = []

    async def _fake_ensure_none(group_id):
        return None

    async def _fake_emit3(group_id, agent_id, agent_name, status, task_id):
        emit_calls3.append((group_id, agent_id, agent_name, status, task_id))

    _registry_instance.ensure_runtime = _fake_ensure_none
    groups_api.emit_agent_status = _fake_emit3
    try:
        r = client.post("/api/groups/group_ghost/stop-turn")
        if r.status_code != 200:
            errs.append(f"[D7] runtime None stop-turn 应 200 no-op，实际 {r.status_code}")
        else:
            body = r.json()
            if body.get("cancelled") is not False:
                errs.append(f"[D7] runtime None 应 cancelled=False，实际 {body}")
            elif "无活跃回合" not in body.get("message", ""):
                errs.append(f"[D7] message 应含「无活跃回合」，实际 {body.get('message')!r}")
            elif emit_calls3:
                errs.append(f"[D7] runtime None 不应 emit（无 coordinator），实际 {emit_calls3}")
            else:
                print("[D7] OK  runtime None → cancelled=False + 不 emit + message 含「无活跃回合」（cold/gone 群组 no-op）")
    finally:
        _registry_instance.ensure_runtime = orig_ensure
        groups_api.emit_agent_status = orig_emit

    # ── E. 与 PL-11 stop-task 区分 ──────────────────────────
    # E8 stop_turn 调 GroupRuntime.cancel_turn（不经 stop_task_by_id/request_cancel）
    src = Path(BACKEND / "api" / "groups.py").read_text(encoding="utf-8")
    stop_body = ""
    if "async def stop_turn(" in src:
        seg = src.split("async def stop_turn(")[1]
        # body up to next @router — strip the docstring first so a docstring
        # mention of ``request_cancel`` (explaining the PL-11 distinction) is
        # not mistaken for an actual call.
        stop_body = "async def stop_turn(" + seg.split("\n@router.")[0]
    # drop the triple-quoted docstring before scanning for calls
    import re as _re
    code_no_doc = _re.sub(r'"""[\s\S]*?"""', '', stop_body, count=1)
    if "cancel_turn" not in code_no_doc:
        errs.append("[E8] stop_turn 体内未调 cancel_turn（StopSignal 硬停）")
    elif "stop_task_by_id" in code_no_doc or "request_cancel(" in code_no_doc:
        errs.append("[E8] stop_turn 不应调 stop_task_by_id/request_cancel（那是 PL-11 驻留引擎 per-task 路径）")
    else:
        print("[E8] OK  stop-turn 调 GroupRuntime.cancel_turn（不经 stop_task_by_id/request_cancel——区分 PL-11 per-task）")

    # ── F. 向后兼容 ──────────────────────────────────────────
    # F9 main import OK
    try:
        import importlib
        import main  # noqa: F401
        importlib.reload(main)
        print("[F9] OK  main 全量 import OK（groups.py 加 emit_agent_status 导入无 cycle）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F9] main import 异常：{type(e).__name__}: {e}")

    # F10 既有路由仍在（reset-session / files / files/{name}）
    expected = [
        "/api/groups/{group_id}/reset-session",
        "/api/groups/{group_id}/files",
        "/api/groups/{group_id}/files/{file_name:path}",
    ]
    missing = [p for p in expected if p not in paths]
    if missing:
        errs.append(f"[F10] 既有路由缺失 {missing}，实际 {list(paths)}")
    else:
        print("[F10] OK  既有路由不破（reset-session / files / files/{{name}} 仍在）")

    return errs


def main() -> int:
    print("=== VH45 回归：POST /api/groups/{group_id}/stop-turn → GroupRuntime.cancel_turn ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "POST /api/groups/{group_id}/stop-turn 端点锁定：\n"
        "  · A 路由 POST /api/groups/{group_id}/stop-turn 注册；\n"
        "  · B 有活跃回合 → cancel_turn True → cancelled=True + message 含「执行中已中断」+ emit agent_status(idle)；\n"
        "  · C 无活跃回合 → cancelled=False + no-op 200 + 仍 emit idle（runtime 存在·UI 归位）；\n"
        "  · D runtime None → cancelled=False + 不 emit + no-op（cold/gone 群组）；\n"
        "  · E 调 GroupRuntime.cancel_turn（不经 PL-11 stop_task_by_id/request_cancel）；\n"
        "  · F main import OK + 既有路由不破。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
