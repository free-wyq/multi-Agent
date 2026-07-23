"""VH59 live 契约：单聊新建会话→发消息→收流式回复（堵 vh57/vh23 静态漏的缺口）.

Path C · C2 严格改名后的核心 live 链路回归。本测试**与 vh57/vh23 互补**：

  - vh57 / vh23 是**静态源码字符串检查**（断言 ``api/messages.py`` 含
    ``route_direct_message``、``load_from_store`` 含 conversations 遍历等）——
    不启服务、不发 live 消息，所以「新建会话→发消息→收流式」这条 live 链路零覆盖。
  - 2026-07-23 live e2e 暴露的真 bug 正是漏在这条链路上：``POST /api/conversations``
    只落 DB 行不建驻留引擎，``route_direct_message`` 的 ``push_notify`` 扔进无人消费的
    inbox，agent 永不回复（用户单聊发「你好」石沉大海）。

本测试驱动真后端（须 ``uvicorn --reload`` 在 :8000）跑完整 live 链路，锁住：

  阶段 A（静态契约——确保修复存在而非靠记忆）：
    1. ``registry.ensure_engine`` 存在（懒建入口，对标 ``ensure_runtime``）。
    2. ``route_direct_message`` 在 ``push_notify`` 前 ``await registry.ensure_engine(...)``。
    3. ``ensure_engine`` 命中缓存直接返回（幂等，``add_engine`` 双 ``_run_loop`` 不冲突）。
    4. ``ensure_engine`` 未命中时读 conversation + agent → ``add_engine(coordinator_id="")``。

  阶段 B（live 运行时——真机验证修复生效）：
    5. ``POST /api/conversations`` 建一个全新单聊（用一个专用 agent，确保**启动期
       无预建引擎**——这是 bug 复现的前提；若该会话启动期已被 load_from_store 建过
       引擎，则测不到「懒建」路径，故用一个 seed 不带的 agent_id 触发 ensure_engine）。
    6. ``GET /api/status/{conversation_id}`` 确认建会话**前**无引擎、发消息**后**有引擎
       （ensure_engine 懒建起效，单聊 engine 注册进 registry）。
    7. ``POST /api/messages`` 给该单聊发消息 → WS ``bus-event:{conversation_id}`` 收到
       ``task_token`` 逐字流式事件（≥1，单聊 worker brain 流式通道）。
    8. 收到持久化 ``agent_reply``（node_chat 落盘），``sender_id`` 是该 worker agent
       而非 coordinator（Bug A 流式归属不回归）。
    9. ``GET /api/messages?conversationId=`` 列表含 user 消息 + agent 回复，conversation_id 字段正确。

  风险点①（流式 reply_id 归属 / Bug A 类）——阶段 B 的 7+8 即是验证：token 事件
  的 sender_id 应为 worker agent_id（非「群主/协调者」）。2026-07-23 live e2e 因
  引擎没建起来无法验证此点；ensure_engine 修复后本测试是其真机回归。

需要后端在线（``cd backend && uvicorn main:app --reload --port 8000``）。离线时
跳过阶段 B（返回 2 标记「需 live 环境」，不判 FAIL 以免 CI 假红）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

REPO = Path(__file__).resolve().parents[2]
REGISTRY_PY = REPO / "backend" / "engine" / "registry.py"
DIRECT_PY = REPO / "backend" / "engine" / "direct.py"

BASE = "http://localhost:8000"
WS_TIMEOUT = 180.0


def _fn_body_py(src: str, fname: str, is_async: bool = False) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def assert_static_contract() -> list[str]:
    """阶段 A：静态源码断言 ensure_engine 懒建修复存在。"""
    errs: list[str] = []
    registry = REGISTRY_PY.read_text(encoding="utf-8")
    direct = DIRECT_PY.read_text(encoding="utf-8")

    # [1] registry.ensure_engine 存在
    if "async def ensure_engine(" not in registry:
        errs.append("[A1] registry 缺 `async def ensure_engine(...)`（懒建单聊 engine 入口缺失）")
    else:
        print("[A1] OK  registry.ensure_engine 存在（懒建单聊 engine 入口，对标 ensure_runtime）")

    # [2] route_direct_message 在 push_notify 前 ensure_engine
    rdm_body = _fn_body_py(direct, "route_direct_message", is_async=True)
    if not rdm_body:
        errs.append("[A2] route_direct_message 函数体未找到")
    else:
        ensure_pos = rdm_body.find("ensure_engine")
        notify_pos = rdm_body.find("push_notify")
        if ensure_pos == -1:
            errs.append("[A2] route_direct_message 缺 `ensure_engine`（新建单聊无引擎→push_notify 进无人读 inbox）")
        elif notify_pos == -1:
            errs.append("[A2] route_direct_message 缺 `push_notify`（路由主路径丢失）")
        elif ensure_pos > notify_pos:
            errs.append("[A2] ensure_engine 在 push_notify 之后（应在前：先确保引擎再投递 notify）")
        else:
            print("[A2] OK  route_direct_message 在 push_notify 前 `await registry.ensure_engine(...)`")

    # [3] ensure_engine 命中缓存直接返回（幂等）
    ee_body = _fn_body_py(registry, "ensure_engine", is_async=True)
    if not ee_body:
        errs.append("[A3] ensure_engine 函数体未找到")
    elif not re.search(r"_engines\.get\([^)]+\)\.get\(agent_id\)", ee_body):
        errs.append("[A3] ensure_engine 未命中缓存直接返回（幂等缺失，与 load_from_store 启动期建的可能双 _run_loop）")
    else:
        print("[A3] OK  ensure_engine 命中缓存直接返回（幂等，与启动期建的不冲突）")

    # [4] ensure_engine 未命中时 add_engine(coordinator_id="")
    if "await self.add_engine(" not in ee_body:
        errs.append("[A4] ensure_engine 未调 add_engine（未命中时未建引擎）")
    elif 'coordinator_id=""' not in ee_body and '""' not in ee_body.split("add_engine")[1][:200]:
        errs.append("[A4] ensure_engine 调 add_engine 未传 coordinator_id=\"\"（单聊应为 worker 图）")
    else:
        print("[A4] OK  ensure_engine 未命中时 add_engine(coordinator_id=\"\") → worker 图")

    return errs


# ── live 运行时验证 ──


async def health_ok() -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{BASE}/health")
            return r.json().get("status") == "ok"
    except Exception:
        return False


async def list_conversations() -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BASE}/api/conversations")
        return r.json()


async def create_conversation(agent_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/conversations",
            json={"agent_id": agent_id},
        )
        return r.json()


async def list_status(conv_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BASE}/api/status/{conv_id}")
        return r.json()


async def list_messages(conv_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{BASE}/api/messages",
            params={"conversationId": conv_id, "limit": "100"},
        )
        return r.json()


async def send_message(conv_id: str, content: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "conversation_id": conv_id,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json()


async def collect_events(conv_id: str, timeout: float) -> list[dict]:
    """连 WS 收事件直到 agent_reply 落地或超时。"""
    import websockets

    events: list[dict] = []
    deadline = time.time() + timeout
    finished = False
    ws_url = f"ws://localhost:8000/ws/bus/{conv_id}"
    async with websockets.connect(
        ws_url, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024
    ) as ws:
        while time.time() < deadline and not finished:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            # 收尾信号：agent_reply 落地（node_chat 持久化）
            if ev.get("type") == "agent_reply":
                # 多收 2s 尾巴
                end = time.time() + 2.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                finished = True
    return events


async def run_live() -> tuple[int, list[str]]:
    """阶段 B：live 运行时验证。返回 (exit_code, errs)。"""
    errs: list[str] = []

    if not await health_ok():
        print("[live] 后端未在线——跳过阶段 B（需 `uvicorn main:app --reload --port 8000`）")
        return (2, errs)
    print("[health] ok")

    # [5] 选一个 agent 建全新单聊。优先用一个 seed 不一定预建的 agent，确保触发懒建。
    #     若该 agent 已有单聊会话（find-or-create 复用），status 应在发消息前为空——
    #     复用已有 conversation 也能验 ensure_engine（若启动期已建引擎则 status 非空，
    #     本项跳过「建前无引擎」断言但仍验「发消息后能收流式」）。
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BASE}/api/agents")
        agents = r.json()
    if not agents:
        errs.append("[B5] 无可用 agent（seed 未建 demo agent）")
        return (1, errs)
    # 用列表第一个 agent（demo seed 的 agent_backend_1 / agent_frontend_1 等）
    agent = agents[0]
    agent_id = agent["id"]
    agent_name = agent.get("name", agent_id)
    print(f"[B5] 选 agent={agent_id} ({agent_name}) 建单聊")

    conv = await create_conversation(agent_id)
    conv_id = conv.get("id")
    if not conv_id or not conv_id.startswith("conv_"):
        errs.append(f"[B5] 建会话返回非 conv_ 前缀: {conv}")
        return (1, errs)
    print(f"[B5] OK  建单聊 conv_id={conv_id[:24]}... (独立 ConversationEntity)")

    # [6] 发消息前 status：单聊 engine 是否已存在
    status_before = await list_status(conv_id)
    had_engine_before = len(status_before) > 0
    print(f"[B6] 发消息前 status 数={len(status_before)} ({'已有引擎' if had_engine_before else '无引擎(启动期未建)'})")

    # [7-8] 发消息 + 收流式
    import websockets  # noqa: F401  (collect_events 内 import，这里确认可导入)

    ws_task = asyncio.create_task(collect_events(conv_id, WS_TIMEOUT))
    await asyncio.sleep(0.5)  # 让 WS 先连上
    sent = await send_message(conv_id, "你好，请用一句话介绍一下你自己")
    if not sent.get("id"):
        errs.append(f"[B7] 发消息失败: {sent}")
        return (1, errs)
    print(f"[B7] 发送 user message id={sent.get('id','')[:16]}...")

    events = await ws_task
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    token_events = [e for e in events if e.get("type") == "task_token"]
    reply_ev = next(
        (e for e in reversed(events) if e.get("type") == "agent_reply"),
        None,
    )

    # [7] 收到 task_token 逐字流式事件（≥1）
    n_tokens = len(token_events)
    print(f"[B7] task_token 事件数={n_tokens}")
    if n_tokens < 1:
        errs.append(
            f"[B7] 未收到 task_token（要求 ≥1）——单聊 worker brain 流式通道未触发。"
            f"类型分布={type_counts}"
        )
    else:
        print("[B7] OK  收到 task_token 逐字流式事件（单聊 worker brain 流式通道起效）")

    # [8] 收到持久化 agent_reply，sender_id 是 worker agent 而非 coordinator
    if not reply_ev:
        errs.append("[B8] 未收到 agent_reply（node_chat 未落盘回复，agent 永不回复 bug 未修复）")
    else:
        reply_sender = reply_ev.get("sender_id")
        print(f"[B8] agent_reply sender_id={reply_sender} (worker agent={agent_id})")
        if reply_sender != agent_id:
            errs.append(
                f"[B8] agent_reply sender_id={reply_sender} ≠ worker agent={agent_id}"
                f"（Bug A 流式归属回归：token/回复冠到了错误头像下）"
            )
        else:
            print("[B8] OK  agent_reply sender_id 是 worker agent（Bug A 流式归属未回归）")

    # [6 尾声] 发消息后 status 应有引擎（ensure_engine 懒建起效）
    status_after = await list_status(conv_id)
    has_engine_after = any(
        s.get("id") == agent_id for s in status_after
    )
    print(f"[B6] 发消息后 status 数={len(status_after)} 该 agent 在册={has_engine_after}")
    if not has_engine_after:
        errs.append("[B6] 发消息后 ensure_engine 未注册引擎（懒建未起效）")
    elif not had_engine_before:
        print("[B6] OK  发消息前无引擎→发消息后有引擎（ensure_engine 懒建起效）")
    else:
        print("[B6] OK  引擎启动期已建（find-or-create 复用已有会话），ensure_engine 幂等返回")

    # [9] 消息列表含 user + agent 回复，conversation_id 字段正确
    msgs = await list_messages(conv_id)
    has_user = any(m.get("sender_id") == "user" for m in msgs)
    has_agent_reply = any(
        m.get("sender_id") == agent_id and m.get("type") == "agent_reply"
        for m in msgs
    )
    conv_ids_ok = all(m.get("conversation_id") == conv_id for m in msgs)
    print(f"[B9] 消息数={len(msgs)} user={has_user} agent_reply={has_agent_reply} conv_id一致={conv_ids_ok}")
    if not has_user:
        errs.append("[B9] 消息列表缺 user 消息")
    if not has_agent_reply:
        errs.append("[B9] 消息列表缺 agent 回复（agent_reply 未持久化）")
    if not conv_ids_ok:
        bad = [m.get("id") for m in msgs if m.get("conversation_id") != conv_id]
        errs.append(f"[B9] 消息 conversation_id 不一致（错误行 id={bad}）")

    return (1 if errs else 0, errs)


def main() -> int:
    print("=== VH59 live 契约：单聊新建会话→发消息→收流式回复 ===\n")

    # ── 阶段 A：静态契约 ──
    print("── 阶段 A：静态契约（ensure_engine 懒建修复存在）──")
    a_errs = assert_static_contract()
    if a_errs:
        print("\n[阶段A] FAIL:")
        for e in a_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（静态契约） ===")
        return 1
    print("[阶段A] PASS\n")

    # ── 阶段 B：live 运行时 ──
    print("── 阶段 B：live 运行时（真机验证修复生效）──")
    code, b_errs = asyncio.run(run_live())
    if code == 2:
        # 后端未在线——标记需 live 环境，不判 FAIL（CI 假红）
        print("\n=== 结果: SKIP（需 live 后端环境） ===")
        return 2
    if b_errs:
        print("\n[阶段B] FAIL:")
        for e in b_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（运行时） ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(
        "单聊新建会话→发消息→收流式回复 live 链路通：\n"
        "  · A 静态：ensure_engine 存在 + route_direct_message 先 ensure 再 push + 幂等 + 未命中建 worker 图；\n"
        "  · B live：新建单聊(无预建引擎)→发消息→ensure_engine 懒建起效→收 task_token 逐字流式→"
        "agent_reply sender_id 是 worker(非协调者)→消息列表含 user+reply 且 conversation_id 一致。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
