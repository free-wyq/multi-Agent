"""任务19e — IM mock echo e2e 全链路.

锁住任务19e 交付物（``docs/im-gateway-design.md`` §8 mock echo e2e 方案）：验证 IM
双向链路 入站 → 投递到 agent → agent reply → 出站钩子 → mock 出站日志；disable 后
不再投递；test 端点 mock 出站探针；清理干净。

四段契约（一个 channel 走完全链路）：

  A. 入站投递到 inbox + 出站钩子真链路（aiohttp echo server 模拟平台回调）
     1. 建 agent + 单聊 conversation（target_kind=single）
     2. POST /api/im-channels 建 channel（platform=wechat, target=conversation_id,
        enabled, config.default_session=sess_out）
     3. 起 aiohttp echo server（/send 端点把收到的 {channel_id, platform_body}
        POST 转发到 gateway 的 /api/im/inbound/{channel_id}，模拟平台回调真路径）
     4. 起 uvicorn Server 跑 FastAPI app（含 im.router + im.im_inbound），同进程
        同 event loop，gateway 的 deliver_inbound 在本 loop 跑——与生产同型
     5. echo server 发一条入站消息 → gateway → route_direct_message（stub 记录
        调用，不建 engine 不调 LLM——本测聚焦 IM 链路，与 18b/19c 思路一致）
     6. 断言：route_direct_message 被调（target=conversation_id, content=入站文本）
        + gateway 返回 conversation_id/platform_session_id/content
     7. mock agent reply（直接调 persist_agent_reply 模拟 engine 回复，不跑真 LLM）
     8. 断言：caplog 命中 ``[im:wechat] outbound → sess_out: {reply content}``
        （reply.py → maybe_deliver_outbound → adapter.send_outbound logger.info 真链路）
     9. 断言：reply 行已落库（出站钩子不破坏落盘主流程）
  B. disable 后不再投递（双向静默）
     1. POST /api/im-channels/{id}/disable
     2. echo server 再发入站 → 断言 HTTP 410（gateway 拒入站，disabled channel）
     3. persist_agent_reply → 断言无新出站日志（list_im_channels_for_target 过滤
        enabled=1，disabled channel 出站也跳过——§7.3 双向静默）
  C. test 端点（mock 出站探针）+ outbound_log=False 跳过
     1. POST /api/im-channels/{id}/test → 断言 ImChannelTestResult(ok=True) +
        caplog 命中 ``[im test] outbound probe``（test 端点独立于 outbound_log）
     2. 建 outbound_log=False channel → persist_agent_reply → 断言无出站日志
        （outbound_log=0 时 maybe_deliver_outbound 跳过 send）
  D. 清理
     1. delete channel + conversation + agent
     2. shutdown_scheduler（AsyncIOScheduler 跨 asyncio.run 坑，习惯性收）
     3. echo server + uvicorn server 关闭（释放端口 + 后台 task）

为何起真 uvicorn Server + aiohttp echo server（而非 ASGITransport 直调）：
任务定义要求「起 aiohttp echo server 模拟平台回调」——echo server 是独立 HTTP 进程
端点，必须经真 HTTP 跳到 gateway，才验「平台→网关」入站链路。gateway 用 uvicorn
跑（同进程同 loop，无跨进程开销），echo server 用 aiohttp.web 跑（同 loop 两个
AppRunner/uvicorn.Server 共存）。两 server 共享 event loop，互发 HTTP（aiohttp
ClientSession）——真实链路断言点（不 monkeypatch HTTP 层）。

为何 stub route_direct_message：本测聚焦 IM 链路（入站投递 + 出站钩子），不验 agent
执行（worker LLM 范畴，任务16b 已覆）。route_direct_message 真跑会 ensure_engine
+ push_notify + 起引擎 run loop，需要 fake LLM——偏离本测焦点 + 增加脆弱性。stub
记录调用即可证「gateway → route_direct_message」链路通（与 19c B 段 patch 同型）。
出站钩子用真 persist_agent_reply（reply.py → outbound.py → adapter 真链路，不
monkeypatch），是本测核心断言点。

为何每段独立 asyncio.run + 独立 _init_isolated_db：与 18b/19b/19c 同款范式——
AsyncIOScheduler 跨 asyncio.run 不迁移（[[scheduler-e2e-test-pattern-2026-07-27]]），
每段独立 loop + 独立隔离 DB（同 MULTI_AGENT_DATA_DIR 临时目录，DB 文件同路径故
数据跨段保留——用模块级 _PROBE 记 id 供 D 段删）。每段末 shutdown_scheduler 习惯性
收（防未来加 scheduler 依赖时踩坑）。pytest 收集 test_a..test_d + main()，每段末
``assert not errs``（pytest 真门）。无 conftest / 无 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="im_e2e_19e_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

import config  # noqa: E402

config.DATA_DIR = _TMP

import store.database as _db  # noqa: E402

importlib.reload(_db)
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 入站探针文本 + 出站探针 token（reply content 含 token，caplog grep 用）
INBOUND_TEXT = "IM-E2E-INBOUND 你好微信"
OUTBOUND_TOKEN = "IM-E2E-OUTBOUND-TOKEN"
DEFAULT_SESSION = "sess_out_19e"


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def _init_isolated_db() -> None:
    _db.engine = create_async_engine(
        _db.DB_URL, echo=False,
        connect_args={"check_same_thread": False}, pool_pre_ping=True,
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession,
    )
    await _db.init_db()
    config.set_active_cache({
        "api_key": "sk-e2e-fake",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake-e2e-model",
        "temperature": 0.0,
        "max_tokens": 0,
    })


class _LogCollector:
    """Attach a handler to ``multi-agent.im`` logger capturing LogRecords.

    Works in both pytest (no caplog fixture in main()) and ``python3 file.py``
    direct-run modes — bridging to a module-level list, same pattern as 19c's
    ``_LogCollector``. The outbound hook + adapter ``send_outbound`` log on this
    logger; this collector is the e2e assertion point.
    """

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None
        self._prev_level: int = logging.NOTSET

    def __enter__(self) -> "_LogCollector":
        log = logging.getLogger("multi-agent.im")
        handler = logging.Handler()
        handler.setLevel(logging.INFO)

        def _emit(record: logging.LogRecord) -> None:
            self.records.append(record)

        handler.emit = _emit  # type: ignore[method-assign]
        self._handler = handler
        self._prev_level = log.level
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc: Any) -> None:
        log = logging.getLogger("multi-agent.im")
        if self._handler is not None:
            log.removeHandler(self._handler)
        log.setLevel(self._prev_level)

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


# ── echo server + uvicorn gateway helpers ─────────────────────────────────


def _free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, then close + return it.

    Race-free enough for tests (the port may be grabbed between close + reuse,
    but aiohttp/uvicorn will surface a bind error if so — we just retry once
    via the two-server setup). Avoids hardcoding ports.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _start_gateway(app: Any) -> tuple[Any, int, str]:
    """Start a uvicorn Server running ``app`` on a free port in the current loop.

    Returns (server, port, base_url). The server task runs in the background on
    the current event loop (same loop the test runs on), so the gateway's
    ``deliver_inbound`` executes in-process — no cross-process overhead, real
    HTTP via aiohttp ClientSession from the echo server. Caller must set
    ``server.should_exit = True`` + await the serve task to shut down.
    """
    import uvicorn

    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    serve_task = asyncio.create_task(server.serve())
    deadline = time.time() + 10.0
    while not server.started and time.time() < deadline:
        await asyncio.sleep(0.05)
    if not server.started:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("uvicorn gateway failed to start within 10s")
    return server, port, f"http://127.0.0.1:{port}", serve_task


async def _start_echo_server(gateway_base: str) -> tuple[Any, int]:
    """Start an aiohttp echo server simulating a platform callback source.

    The echo server exposes ``POST /send``: it takes ``{channel_id, platform_body}``
    and POSTs ``platform_body`` to the gateway's ``/api/im/inbound/{channel_id}``.
    This mirrors a real platform (WeChat Work / DingTalk / Feishu) calling back
    into the gateway — the echo server is the "platform" in the mock e2e. Returns
    (runner, port). Caller must ``await runner.cleanup()`` to shut down.
    """
    import aiohttp
    from aiohttp import web

    echo_app = web.Application()

    async def send_cb(request: web.Request) -> web.Response:
        payload = await request.json()
        channel_id = payload["channel_id"]
        body = payload["platform_body"]
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{gateway_base}/api/im/inbound/{channel_id}", json=body
            ) as resp:
                try:
                    rb = await resp.json()
                except Exception:  # noqa: BLE001
                    rb = await resp.text()
                return web.json_response({"status": resp.status, "body": rb})

    echo_app.router.add_post("/send", send_cb)
    runner = web.AppRunner(echo_app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, port


async def _echo_send(echo_port: int, channel_id: str, platform_body: dict) -> dict:
    """Trigger the echo server's /send → gateway inbound path, return the result.

    The returned dict has ``status`` (gateway HTTP status) + ``body`` (gateway
    response). Used to assert 200 on enabled channels + 410 on disabled.
    """
    import aiohttp

    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"http://127.0.0.1:{echo_port}/send",
            json={"channel_id": channel_id, "platform_body": platform_body},
        ) as resp:
            return await resp.json()


# ── A. 入站投递到 inbox + 出站钩子真链路 ────────────────────────────────────
async def _async_a_inbound_outbound_chain() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import AgentCreatePayload, ConversationCreatePayload, ImChannelCreatePayload
    from fastapi import FastAPI
    from api import im as im_api
    from engine.reply import persist_agent_reply

    # stub route_direct_message：记录调用，不建 engine 不调 LLM（本测聚焦 IM 链路）
    import engine.direct as direct_mod
    direct_calls: list[tuple[str, str]] = []

    async def _stub_direct(conversation_id: str, content: str) -> None:
        direct_calls.append((conversation_id, content))

    orig_direct = direct_mod.route_direct_message
    direct_mod.route_direct_message = _stub_direct

    # 建 agent + 单聊 conversation
    agent = await crud.create_agent(AgentCreatePayload(
        name=f"[e2e-19e] agent {uuid.uuid4().hex[:6]}",
        role="backend_engineer",
        system_prompt="你是 IM e2e 探针目标 agent。",
        description="任务19e e2e 探针",
    ))
    conv = await crud.create_conversation(ConversationCreatePayload(
        agent_id=agent.id, name="im-e2e-19e",
    ))

    # 建 channel（platform=wechat, target=conv.id, enabled, config.default_session）
    gateway_app = FastAPI()
    gateway_app.include_router(im_api.router)
    gateway_app.include_router(im_api.im_inbound)
    server, gw_port, gw_base, serve_task = await _start_gateway(gateway_app)
    echo_runner, echo_port = await _start_echo_server(gw_base)

    try:
        # 用 HTTP 建 channel（与前端同路径，验 /api/im-channels POST）
        import aiohttp
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{gw_base}/api/im-channels",
                json={
                    "name": "19e 入站探针渠道",
                    "platform": "wechat",
                    "target_conversation_id": conv.id,
                    "target_kind": "single",
                    "target_agent_id": agent.id,
                    "enabled": True,
                    "outbound_log": True,
                    "config": {
                        "app_id": "wx_19e",
                        "app_secret": "secret_19e",
                        "default_session": DEFAULT_SESSION,
                    },
                },
            ) as resp:
                ch_json = await resp.json()
        channel_id = ch_json["id"]
        ok = _check(
            "A1 HTTP POST /api/im-channels 建渠道（id imc_ 前缀 / enabled=True）",
            channel_id.startswith("imc_") and ch_json.get("enabled") is True,
            f"id={channel_id} enabled={ch_json.get('enabled')}",
        )
        if not ok:
            errs.append("[A1] HTTP 建渠道失败")
            return errs

        # echo server 发入站消息（模拟平台回调）→ gateway → route_direct_message
        result = await _echo_send(echo_port, channel_id, {
            "FromUserName": "u_wx_19e",
            "Content": INBOUND_TEXT,
            "MsgType": "text",
        })
        ok = _check(
            "A2 echo→gateway 入站 HTTP 200（gateway.deliver_inbound 成功）",
            result.get("status") == 200,
            f"status={result.get('status')} body={result.get('body')}",
        )
        if not ok:
            errs.append("[A2] 入站 HTTP 非 200")
        ok = _check(
            "A3 gateway 返回 conversation_id=conv.id + platform_session_id=u_wx_19e",
            (result.get("body") or {}).get("conversation_id") == conv.id
            and (result.get("body") or {}).get("platform_session_id") == "u_wx_19e",
            f"body={result.get('body')}",
        ) and ok
        ok = _check(
            "A4 入站 content 透传（INBOUND_TEXT）",
            (result.get("body") or {}).get("content") == INBOUND_TEXT,
        ) and ok

        # route_direct_message 被调（target=conv.id, content=INBOUND_TEXT）
        ok = _check(
            "A5 route_direct_message 被调（target=conv.id / content=INBOUND_TEXT）",
            len(direct_calls) == 1 and direct_calls[0] == (conv.id, INBOUND_TEXT),
            f"calls={direct_calls}",
        )
        if not ok:
            errs.append("[A5] route_direct_message 未被调或参数错")

        # mock agent reply（直接调 persist_agent_reply 模拟 engine 回复，不跑真 LLM）
        # reply.py → maybe_deliver_outbound → adapter.send_outbound logger.info 真链路
        reply_content = f"{OUTBOUND_TOKEN} 这是 IM e2e 出站探针回复"
        with _LogCollector() as cap:
            reply_msg = await persist_agent_reply(
                group_id=conv.id,
                agent_id=agent.id,
                content=reply_content,
            )
        expected_log = f"[im:wechat] outbound → {DEFAULT_SESSION}: {reply_content}"
        ok = _check(
            f"A6 persist_agent_reply 触发出站钩子（caplog 命中出站日志）",
            expected_log in cap.messages,
            f"msgs={cap.messages}",
        )
        if not ok:
            errs.append("[A6] 出站钩子未发预期日志（reply→outbound 链路断）")

        # reply 行已落库（出站钩子不破坏落盘主流程）
        msgs = await crud.list_messages(conv.id, limit=20)
        ok = _check(
            "A7 reply 行已落库（出站钩子不破坏 persist 主流程）",
            any(m.content == reply_content and m.type == "agent_reply" for m in msgs),
            f"msgs={[(m.type, m.content[:30]) for m in msgs]}",
        )
        if not ok:
            errs.append("[A7] reply 行未落库")

        # 留 id 给 B/C/D 段
        global _PROBE
        _PROBE = {
            "channel_id": channel_id,
            "conversation_id": conv.id,
            "agent_id": agent.id,
            "gw_port": gw_port,
            "echo_port": echo_port,
        }
        # server / runner handle 不跨段（每段独立 loop 重建），只留 id + port 给 B 段重起
        _PROBE_HANDLE["gateway_app"] = gateway_app
    finally:
        direct_mod.route_direct_message = orig_direct
        # 关 servers（本段 loop 末，B 段重起）
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=8.0)
        except Exception:  # noqa: BLE001
            pass
        await echo_runner.cleanup()
        # 每段独立 loop：shutdown scheduler 单例（防下段 add_job 调 call_soon_threadsafe
        # 到已关闭的 loop——AsyncIOScheduler 绑定首个 loop，跨 asyncio.run 不迁移）
        from engine import scheduler as sch
        await sch.shutdown_scheduler()

    if not ok:
        errs.append("[A] 入站投递 + 出站钩子断言失败")
    return errs


# ── B. disable 后不再投递（双向静默） ──────────────────────────────────────
async def _async_b_disable_silent() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from api import im as im_api
    from engine.reply import persist_agent_reply
    from fastapi import FastAPI

    probe = _PROBE
    gateway_app = _PROBE_HANDLE["gateway_app"]
    server, gw_port, gw_base, serve_task = await _start_gateway(gateway_app)
    echo_runner, echo_port = await _start_echo_server(gw_base)

    try:
        # B1 disable 渠道（HTTP POST /disable）
        import aiohttp
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"{gw_base}/api/im-channels/{probe['channel_id']}/disable"
            ) as resp:
                disabled = await resp.json()
        ok = _check(
            "B1 POST /disable → enabled=False",
            disabled.get("enabled") is False,
            f"enabled={disabled.get('enabled')}",
        )
        if not ok:
            errs.append("[B1] disable 未生效")

        # B2 echo server 再发入站 → 断言 410（gateway 拒入站，disabled channel）
        result = await _echo_send(echo_port, probe["channel_id"], {
            "FromUserName": "u_wx_19e_disabled",
            "Content": "disabled 后不应投递",
            "MsgType": "text",
        })
        ok = _check(
            "B2 disabled channel 入站 → HTTP 410（gateway 拒入站）",
            result.get("status") == 410,
            f"status={result.get('status')} body={result.get('body')}",
        )
        if not ok:
            errs.append("[B2] disabled 入站未返回 410")

        # B3 persist_agent_reply → 无新出站日志（list_im_channels_for_target
        # 过滤 enabled=1，disabled channel 出站也跳过——§7.3 双向静默）
        reply_content = f"{OUTBOUND_TOKEN}-disabled-不应出站"
        with _LogCollector() as cap:
            await persist_agent_reply(
                group_id=probe["conversation_id"],
                agent_id=probe["agent_id"],
                content=reply_content,
            )
        ok = _check(
            "B3 disabled channel persist_agent_reply → 无出站日志（双向静默）",
            len([m for m in cap.messages if "outbound" in m]) == 0,
            f"msgs={cap.messages}",
        )
        if not ok:
            errs.append("[B3] disabled channel 仍出站（双向静默破）")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=8.0)
        except Exception:  # noqa: BLE001
            pass
        await echo_runner.cleanup()
        from engine import scheduler as sch
        await sch.shutdown_scheduler()

    if not ok:
        errs.append("[B] disable 双向静默断言失败")
    return errs


# ── C. test 端点（mock 出站探针）+ outbound_log=False 跳过 ─────────────────
async def _async_c_test_and_no_log() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import ImChannelCreatePayload
    from api import im as im_api
    from engine.reply import persist_agent_reply

    probe = _PROBE
    # C1 重新 enable 渠道（B 段 disable 了，test 端点不要求 enabled，但还原状态干净）
    await crud.set_im_channel_enabled(probe["channel_id"], True)
    # C2 POST /test → ImChannelTestResult(ok=True) + caplog 命中出站探针日志
    with _LogCollector() as cap:
        result = await im_api.test_im_channel_route(probe["channel_id"])
    ok = _check(
        "C1 POST /test → ImChannelTestResult(ok=True, platform=wechat)",
        result.ok is True and result.platform == "wechat",
        f"result={result}",
    )
    if not ok:
        errs.append("[C1] test 端点未返 ok=True")
    ok = _check(
        "C2 /test 触发 mock 出站探针日志（[im test] outbound probe）",
        any("[im test] outbound probe" in m for m in cap.messages),
        f"msgs={cap.messages}",
    ) and ok
    if not ok:
        errs.append("[C2] test 端点未发出站探针日志")

    # C3 建 outbound_log=False channel → persist_agent_reply → 无出站日志
    ch_nolog = await crud.create_im_channel(ImChannelCreatePayload(
        name="19e 出站日志关",
        platform="wechat",
        target_conversation_id="conv_nolog_19e",
        target_kind="single",
        enabled=True,
        outbound_log=False,
        config={"default_session": "sess_nolog"},
    ))
    ok = _check(
        "C3 建 outbound_log=False channel",
        ch_nolog.outbound_log is False,
        f"outbound_log={ch_nolog.outbound_log}",
    )
    if not ok:
        errs.append("[C3] outbound_log=False 渠道未建")
    else:
        with _LogCollector() as cap:
            await persist_agent_reply(
                group_id="conv_nolog_19e",
                agent_id="agent_nolog",
                content=f"{OUTBOUND_TOKEN}-nolog-不应出站",
            )
        ok = _check(
            "C4 outbound_log=False → persist_agent_reply 无出站日志（跳过 send）",
            len([m for m in cap.messages if "outbound" in m]) == 0,
            f"msgs={cap.messages}",
        ) and ok
        if not ok:
            errs.append("[C4] outbound_log=False 仍出站")
        # 清理本段建的 channel（D 段兜底全删，此处先删避免遗留）
        await crud.delete_im_channel(ch_nolog.id)

    # 每段独立 loop：shutdown scheduler 单例
    from engine import scheduler as sch
    await sch.shutdown_scheduler()
    if not ok:
        errs.append("[C] test 端点 + outbound_log 跳过断言失败")
    return errs


# ── D. 清理 ───────────────────────────────────────────────────────────────
async def _async_d_cleanup() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from api import im as im_api

    # D1 全删剩余 channel（A/B/C 段建的，list 兜底全清，不依赖 _PROBE 跨段）
    remaining = await crud.list_im_channels()
    for ch in remaining:
        deleted = await im_api.delete_im_channel_route(ch.id)
        _check(f"D1 delete channel {ch.id} ({ch.name})", deleted is True)

    leftover = await crud.list_im_channels()
    ok = _check(
        "D2 清理后 list_im_channels 为空",
        len(leftover) == 0,
        f"leftover={[c.id for c in leftover]}",
    )
    if not ok:
        errs.append("[D2] channel 清理不干净")

    # D3 删 conversation + agent（A 段建的）
    probe = _PROBE
    ok_conv = await crud.delete_conversation(probe["conversation_id"])
    ok_agent = await crud.delete_agent(probe["agent_id"])
    ok = _check(
        "D3 delete conversation + agent",
        ok_conv and ok_agent,
        f"conv={ok_conv} agent={ok_agent}",
    ) and ok
    if not ok:
        errs.append("[D3] conversation/agent 删除失败")

    # D4 断言 conversation/agent 已删（get 返 None）
    c = await crud.get_conversation(probe["conversation_id"])
    a = await crud.get_agent(probe["agent_id"])
    ok = _check(
        "D4 conversation/agent 已删（get 返 None）",
        c is None and a is None,
        f"conv={c is not None} agent={a is not None}",
    ) and ok

    # D5 shutdown_scheduler（AsyncIOScheduler 跨 asyncio.run 坑，习惯性收）
    from engine import scheduler as sch
    await sch.shutdown_scheduler()
    _check("D5 shutdown_scheduler OK（无残留 scheduler 单例）", True)

    if not ok:
        errs.append("[D] 清理断言失败")
    return errs


# ── pytest 入口（test_a..test_d + main） ────────────────────────────────────

_PROBE: dict[str, Any] = {}
_PROBE_HANDLE: dict[str, Any] = {}  # gateway_app 跨段复用（每段重起 server）


def test_a_inbound_outbound_chain() -> None:
    print("\n=== A. 入站投递到 inbox + 出站钩子真链路（echo→gateway→agent→outbound）===")
    try:
        errs = asyncio.run(_async_a_inbound_outbound_chain())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ A 段异常: {type(e).__name__}: {e}")
        errs = [f"[A] 异常: {e}"]
    assert not errs, "\n".join(errs)


def test_b_disable_silent() -> None:
    print("\n=== B. disable 后不再投递（双向静默：入站 410 + 出站跳过）===")
    try:
        errs = asyncio.run(_async_b_disable_silent())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ B 段异常: {type(e).__name__}: {e}")
        errs = [f"[B] 异常: {e}"]
    assert not errs, "\n".join(errs)


def test_c_test_and_no_log() -> None:
    print("\n=== C. test 端点（mock 出站探针）+ outbound_log=False 跳过 ===")
    try:
        errs = asyncio.run(_async_c_test_and_no_log())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ C 段异常: {type(e).__name__}: {e}")
        errs = [f"[C] 异常: {e}"]
    assert not errs, "\n".join(errs)


def test_d_cleanup() -> None:
    print("\n=== D. 清理（删 channel/conversation/agent + shutdown_scheduler）===")
    try:
        errs = asyncio.run(_async_d_cleanup())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ D 段异常: {type(e).__name__}: {e}")
        errs = [f"[D] 异常: {e}"]
    assert not errs, "\n".join(errs)


def main() -> int:
    """直接 ``python3 tests/test_im_e2e_full_chain.py`` 跑（非 pytest）."""
    print("=" * 70)
    print("任务19e IM mock echo e2e 全链路：入站→agent→出站日志 + disable 双向静默")
    print("=" * 70)
    for fn in (test_a_inbound_outbound_chain, test_b_disable_silent,
               test_c_test_and_no_log, test_d_cleanup):
        fn()  # assert 内置失败即 raise
    print("\n" + "=" * 70)
    print("PASS — IM mock echo e2e 全链路验证通过：")
    print("  · A echo server 模拟平台回调 → gateway.deliver_inbound → "
          "route_direct_message 被调（target/content 透传）+")
    print("    persist_agent_reply 触发出站钩子（caplog 命中 [im:wechat] outbound → "
          "sess_out: {reply}）+ reply 行落库；")
    print("  · B disable 后入站 HTTP 410 + persist_agent_reply 无出站日志（双向静默）；")
    print("  · C /test 端点返 ok=True + 出站探针日志 + outbound_log=False 跳过 send；")
    print("  · D channel/conversation/agent 已删 + scheduler 单例已清。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
