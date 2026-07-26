"""任务19c — IM 网关核心 + API 契约测试.

锁住任务19c 交付物（``docs/im-gateway-design.md`` §5/§6/§7）：

  A. API CRUD + 脱敏 + 启停 + test 端点（``api/im.py``）—— 建渠道（config 含
     app_secret）→ GET 返回脱敏 ``***`` → PUT 带 ``***`` merge 保留原值 →
     enable/disable → test 端点 mock 出站日志 + ``ImChannelTestResult(ok=True)``。
  B. gateway.deliver_inbound 入站路由（``engine/im/gateway.py``）—— disabled
     channel → ``InboundDeliveryError(410)``；unknown id → 404；unknown platform
     → 400；enabled + wechat body → verify_inbound(mock True) → parse_inbound →
     按 target_kind=single 路由到 ``route_direct_message``（patch 记录调用，不建
     engine）；target_kind=group → ``route_user_message``。
  C. 出站钩子（``engine/im/outbound.py`` + ``reply.py`` 挂点）——
     ``maybe_deliver_outbound`` 有 channel → caplog 命中
     ``[im:wechat] outbound → {target}: {content}``；无 channel → no-op（无日志
     无异常）；``persist_agent_reply`` 落盘后自动触发出站钩子（真链路：reply→
     outbound）。
  D. 清理 —— delete channel；list_im_channels 为空；shutdown_scheduler（防
     AsyncIOScheduler 跨 asyncio.run 坑，见 [[scheduler-e2e-test-pattern-2026-07-27]]）。

确定性（不依赖 live server / 真实 LLM / 真实 IM 平台）。隔离 DB（与 18b/19b 同款
MULTI_AGENT_DATA_DIR 临时目录 + reload(_db) + create_async_engine + init_db）。
入站路由用 patch 替换 ``route_direct_message`` / ``route_user_message`` 记录调用——
测 gateway 路由分流，不建 engine 不调 LLM（与 18b「不跑真 agent LLM loop」思路一致，
本测聚焦 IM 链路）。caplog 拦 ``multi-agent.im`` logger——出站是真 ``logger.info``，
caplog 是真实链路断言点（不 monkeypatch adapter，保持任务19e 会复用的真链路）。

pytest 收集：``test_a``..``test_d`` + ``main()``。每段独立 ``asyncio.run`` + 独立
``_init_isolated_db``，每段末 ``assert not errs``（pytest 真门，与 18b/19b 同）。
``main()`` 顺序调（直接 ``python3 tests/test_im_19c_gateway_api.py`` 也门）。无
conftest / 无 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="im_19c_")
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
    direct-run modes — bridging to a module-level list, same pattern as 19b's
    test_d ``_Collector``.
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


# ── A. API CRUD + 脱敏 + 启停 + test 端点 ─────────────────────────────────
async def _async_a_crud_masking_test() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from api import im as im_api
    from models import ImChannelCreatePayload
    from store import crud

    # 建：config 含 app_secret（明文落库）
    payload = ImChannelCreatePayload(
        name="19c 探针渠道",
        platform="wechat",
        config={
            "app_id": "wx_probe",
            "app_secret": "secret_probe_value",
            "verify_token": "vt_probe",
            "webhook_url": "http://mock/hook",
        },
        target_conversation_id="conv_probe_19c",
        target_kind="single",
        target_agent_id="agent_probe",
    )
    ch = await im_api.create_im_channel_route(payload)
    ok = _check(
        "A1 POST /api/im-channels 建渠道（id imc_ 前缀 / enabled 默认 False）",
        ch.id.startswith("imc_") and ch.enabled is False,
        f"id={ch.id} enabled={ch.enabled}",
    )
    if not ok:
        errs.append("[A1] 建渠道失败")
        return errs

    # GET 单条：config 脱敏（app_secret / verify_token → ***，app_id / webhook_url 原值）
    got = await im_api.get_im_channel_route(ch.id)
    assert got is not None
    ok = _check(
        "A2 GET 单条 config 脱敏：app_secret → ***",
        got.config is not None and got.config.get("app_secret") == "***",
        f"config={got.config}",
    ) and ok
    ok = _check(
        "A3 GET 单条 config 脱敏：verify_token → ***",
        got.config is not None and got.config.get("verify_token") == "***",
    ) and ok
    ok = _check(
        "A4 GET 单条 config 非敏感字段保留：app_id / webhook_url 原值",
        got.config is not None
        and got.config.get("app_id") == "wx_probe"
        and got.config.get("webhook_url") == "http://mock/hook",
    ) and ok
    # raw entity 仍是明文（gateway / test 端点要用真凭证）
    raw = await crud.get_im_channel_entity(ch.id)
    ok = _check(
        "A5 get_im_channel_entity 返回明文 config（gateway/test 真源）",
        raw is not None and raw.config is not None
        and raw.config.get("app_secret") == "secret_probe_value",
    ) and ok

    # PUT：只改 name，config 带 ***（前端 GET 后原样回传）→ merge 保留原 secret
    update_payload = ImChannelCreatePayload(
        name="19c 探针渠道（改名）",
        platform="wechat",
        config={
            "app_id": "wx_probe",
            "app_secret": "***",  # 前端未改，回传掩码
            "verify_token": "***",
            "webhook_url": "http://mock/hook",
        },
        target_conversation_id="conv_probe_19c",
        target_kind="single",
        target_agent_id="agent_probe",
    )
    updated = await im_api.update_im_channel_route(ch.id, update_payload)
    ok = _check(
        "A6 PUT 改名生效",
        updated is not None and updated.name == "19c 探针渠道（改名）",
    ) and ok
    raw_after = await crud.get_im_channel_entity(ch.id)
    ok = _check(
        "A7 PUT 带 *** merge 保留原 app_secret（不落 *** 明文）",
        raw_after is not None and raw_after.config is not None
        and raw_after.config.get("app_secret") == "secret_probe_value",
        f"config={raw_after.config if raw_after else None}",
    ) and ok

    # enable：enabled → True
    enabled = await im_api.enable_im_channel_route(ch.id)
    ok = _check(
        "A8 enable 后 enabled=True",
        enabled is not None and enabled.enabled is True,
    ) and ok

    # test 端点：mock 出站日志 + ImChannelTestResult(ok=True)
    with _LogCollector() as cap:
        result = await im_api.test_im_channel_route(ch.id)
    ok = _check(
        "A9 test 端点返回 ImChannelTestResult(ok=True, platform=wechat)",
        result.ok is True and result.platform == "wechat",
        f"result={result}",
    ) and ok
    ok = _check(
        "A10 test 端点触发 mock 出站日志行 [im:wechat] outbound →",
        any("[im:wechat] outbound →" in m for m in cap.messages),
        f"msgs={cap.messages}",
    ) and ok

    # test 端点：不存在的 channel → ok=False（不 500）
    missing = await im_api.test_im_channel_route("imc_does_not_exist")
    ok = _check(
        "A11 test 不存在 channel → ImChannelTestResult(ok=False)（不 500）",
        missing.ok is False,
        f"result={missing}",
    ) and ok

    # 留 channel_id 给 D 段收尾
    global _PROBE
    _PROBE = {"channel_id": ch.id}
    return errs if ok else errs + ["[A] API CRUD/脱敏/test 断言失败"]


# ── B. gateway.deliver_inbound 入站路由 ───────────────────────────────────
async def _async_b_inbound_routing() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from engine.im.gateway import InboundDeliveryError, deliver_inbound
    from models import ImChannelCreatePayload
    from store import crud
    from api import im as im_api

    # 建一个 enabled wechat channel（target_kind=single）
    ch = await im_api.create_im_channel_route(ImChannelCreatePayload(
        name="19c 入站探针 single",
        platform="wechat",
        config={"app_id": "wx_in", "app_secret": "s", "default_session": "sess_in"},
        target_conversation_id="conv_inbound_single",
        target_kind="single",
        target_agent_id="agent_in",
        enabled=True,
    ))
    # 建一个 enabled wechat channel（target_kind=group）
    ch_g = await im_api.create_im_channel_route(ImChannelCreatePayload(
        name="19c 入站探针 group",
        platform="wechat",
        config={"app_id": "wx_g"},
        target_conversation_id="grp_inbound",
        target_kind="group",
        enabled=True,
    ))
    # 建一个 disabled channel
    ch_dis = await im_api.create_im_channel_route(ImChannelCreatePayload(
        name="19c 入站探针 disabled",
        platform="wechat",
        config={"app_id": "wx_d"},
        target_conversation_id="conv_disabled",
        target_kind="single",
        enabled=False,
    ))
    # 建一个 unknown platform channel（直接落库绕过校验，模拟档四未落 adapter）
    ch_unk = await crud.create_im_channel(ImChannelCreatePayload(
        name="19c 入站探针 unknown-platform",
        platform="slack",  # 不在 ADAPTERS
        target_conversation_id="conv_unk",
        target_kind="single",
        enabled=True,
    ))

    # B1 disabled → 410
    try:
        await deliver_inbound(ch_dis.id, {"X": "y"}, b'{"FromUserName":"u","Content":"hi"}')
        ok = _check("B1 disabled channel 入站 → InboundDeliveryError(410)", False, "未 raise")
        errs.append("[B1] disabled 未拒入站")
    except InboundDeliveryError as e:
        ok = _check(
            "B1 disabled channel 入站 → InboundDeliveryError(410)",
            e.status_code == 410,
            f"status={e.status_code}",
        )
    if not ok:
        errs.append("[B1] disabled 入站断言失败")

    # B2 unknown id → 404
    try:
        await deliver_inbound("imc_nope", {}, b"{}")
        ok = _check("B2 unknown channel id → 404", False, "未 raise")
    except InboundDeliveryError as e:
        ok = _check("B2 unknown channel id → 404", e.status_code == 404, f"status={e.status_code}")
    if not ok:
        errs.append("[B2] unknown id 断言失败")

    # B3 unknown platform → 400
    try:
        await deliver_inbound(ch_unk.id, {}, b'{"FromUserName":"u","Content":"hi"}')
        ok = _check("B3 unknown platform → 400", False, "未 raise")
    except InboundDeliveryError as e:
        ok = _check(
            "B3 unknown platform → 400",
            e.status_code == 400 and "slack" in e.detail,
            f"status={e.status_code} detail={e.detail}",
        )
    if not ok:
        errs.append("[B3] unknown platform 断言失败")

    # B4 enabled + single → 路由到 route_direct_message（patch 记录调用）
    import engine.direct as _direct_mod
    import engine.mention as _mention_mod
    direct_calls: list[tuple[str, str]] = []
    user_calls: list[tuple[str, str]] = []

    async def _stub_direct(conversation_id: str, content: str) -> None:
        direct_calls.append((conversation_id, content))

    async def _stub_user(group_id: str, content: str, *, converge: bool = False) -> None:
        user_calls.append((group_id, content))

    orig_direct = _direct_mod.route_direct_message
    orig_user = _mention_mod.route_user_message
    _direct_mod.route_direct_message = _stub_direct
    _mention_mod.route_user_message = _stub_user
    try:
        body = json.dumps({"FromUserName": "u_wx", "Content": "你好 IM", "MsgType": "text"})
        result = await deliver_inbound(ch.id, {"X": "y"}, body)
        ok = _check(
            "B4 single 入站 → route_direct_message 被调（target=conv_inbound_single）",
            len(direct_calls) == 1 and direct_calls[0][0] == "conv_inbound_single",
            f"calls={direct_calls}",
        )
        if not ok:
            errs.append("[B4] single 路由未到 route_direct_message")
        ok = _check(
            "B5 single 入站 content 透传（你好 IM）",
            len(direct_calls) == 1 and direct_calls[0][1] == "你好 IM",
        ) and ok
        ok = _check(
            "B6 deliver_inbound 返回 conversation_id + platform_session_id",
            result.get("conversation_id") == "conv_inbound_single"
            and result.get("platform_session_id") == "u_wx",
            f"result={result}",
        ) and ok
        ok = _check(
            "B7 single 入站未误走 route_user_message（group 路径）",
            len(user_calls) == 0,
            f"user_calls={user_calls}",
        ) and ok

        # B8 enabled + group → 路由到 route_user_message
        user_calls.clear()
        result_g = await deliver_inbound(ch_g.id, {}, body)
        ok = _check(
            "B8 group 入站 → route_user_message 被调（target=grp_inbound）",
            len(user_calls) == 1 and user_calls[0][0] == "grp_inbound",
            f"user_calls={user_calls}",
        ) and ok
        if not ok:
            errs.append("[B8] group 路由未到 route_user_message")
    finally:
        _direct_mod.route_direct_message = orig_direct
        _mention_mod.route_user_message = orig_user

    # 留 id 给 D 段
    global _PROBE
    _PROBE = {
        "channel_id": ch.id,
        "channel_id_g": ch_g.id,
        "channel_id_dis": ch_dis.id,
        "channel_id_unk": ch_unk.id,
    }
    if not ok:
        errs.append("[B] 入站路由断言失败")
    return errs


# ── C. 出站钩子（maybe_deliver_outbound + reply.py 挂点） ───────────────────
async def _async_c_outbound_hook() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from engine.im.outbound import maybe_deliver_outbound
    from models import ImChannelCreatePayload
    from api import im as im_api

    # 建一个 enabled wechat channel 绑定 conv_outbound
    ch = await im_api.create_im_channel_route(ImChannelCreatePayload(
        name="19c 出站探针",
        platform="wechat",
        config={"app_id": "wx_out", "default_session": "sess_out"},
        target_conversation_id="conv_outbound",
        target_kind="single",
        enabled=True,
    ))

    # C1 maybe_deliver_outbound 有 channel → caplog 命中出站日志
    reply_msg = {
        "id": "msg_out_1",
        "conversation_id": "conv_outbound",
        "sender_id": "agent_out",
        "receiver_id": "broadcast",
        "type": "agent_reply",
        "content": "这是 IM 出站探针回复",
    }
    with _LogCollector() as cap:
        await maybe_deliver_outbound(reply_msg)
    expected = "[im:wechat] outbound → sess_out: 这是 IM 出站探针回复"
    ok = _check(
        f"C1 maybe_deliver_outbound 有 channel → caplog 命中: {expected}",
        expected in cap.messages,
        f"msgs={cap.messages}",
    )
    if not ok:
        errs.append("[C1] 出站钩子未发预期日志")

    # C2 无 channel → no-op（无日志无异常）
    with _LogCollector() as cap:
        await maybe_deliver_outbound({
            "id": "msg_no_channel",
            "conversation_id": "conv_no_channel_bound",
            "content": "不应触发任何出站",
        })
    ok = _check(
        "C2 无 channel → no-op（无出站日志）",
        len(cap.messages) == 0,
        f"msgs={cap.messages}",
    ) and ok

    # C3 disabled channel → 出站也跳过（§7.3 disable 双向静默）
    ch_dis = await im_api.create_im_channel_route(ImChannelCreatePayload(
        name="19c 出站探针 disabled",
        platform="wechat",
        config={"default_session": "sess_dis"},
        target_conversation_id="conv_out_dis",
        target_kind="single",
        enabled=False,
    ))
    with _LogCollector() as cap:
        await maybe_deliver_outbound({
            "id": "msg_dis",
            "conversation_id": "conv_out_dis",
            "content": "disabled 不应出站",
        })
    ok = _check(
        "C3 disabled channel → 出站跳过（disable 双向静默）",
        len(cap.messages) == 0,
        f"msgs={cap.messages}",
    ) and ok

    # C4 真链路：persist_agent_reply 落盘后自动触发出站钩子（reply.py 挂点）
    from engine.reply import persist_agent_reply
    with _LogCollector() as cap:
        await persist_agent_reply(
            group_id="conv_outbound",
            agent_id="agent_out",
            content="persist_reply 触发出站",
        )
    ok = _check(
        "C4 persist_agent_reply 落盘后自动触发出站钩子（[im:wechat] outbound → sess_out）",
        any(
            "[im:wechat] outbound → sess_out" in m and "persist_reply 触发出站" in m
            for m in cap.messages
        ),
        f"msgs={cap.messages}",
    ) and ok

    # C5 reply 落盘主流程不被出站钩子破坏（msg 行已落库）
    from store import crud
    msgs = await crud.list_messages("conv_outbound", limit=10)
    ok = _check(
        "C5 persist_agent_reply 行已落库（出站钩子不破坏落盘主流程）",
        any(m.content == "persist_reply 触发出站" and m.type == "agent_reply" for m in msgs),
        f"msgs={[(m.type, m.content) for m in msgs]}",
    ) and ok

    global _PROBE
    _PROBE["out_channel_id"] = ch.id
    _PROBE["out_channel_id_dis"] = ch_dis.id
    if not ok:
        errs.append("[C] 出站钩子断言失败")
    return errs


# ── D. 清理 ───────────────────────────────────────────────────────────────
async def _async_d_cleanup() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from api import im as im_api
    from store import crud

    # Self-contained cleanup: list ALL remaining channels and delete each.
    # Not relying on _PROBE — each segment runs in its own asyncio.run (pytest
    # one test per function; main() resets _PROBE between segments), so _PROBE
    # would only carry the last segment's ids. Listing guarantees every channel
    # created across A/B/C is removed regardless of which ids were tracked.
    remaining = await crud.list_im_channels()
    for c in remaining:
        deleted = await im_api.delete_im_channel_route(c.id)
        _check(f"D1 delete channel {c.id} ({c.name})", deleted is True)

    leftover = await crud.list_im_channels()
    ok = _check(
        "D2 清理后 list_im_channels 为空",
        len(leftover) == 0,
        f"leftover={[c.id for c in leftover]}",
    )
    if not ok:
        errs.append("[D] 清理不干净")

    # shutdown_scheduler（AsyncIOScheduler 跨 asyncio.run 坑，习惯性收）
    from engine import scheduler as sch
    await sch.shutdown_scheduler()
    _check("D3 shutdown_scheduler OK（无残留 scheduler 单例）", True)
    return errs


# ── pytest 入口（test_a..test_d + main） ────────────────────────────────────


def test_a_crud_masking_test() -> None:
    errs = asyncio.run(_async_a_crud_masking_test())
    assert not errs, f"段 A 失败: {errs}"


def test_b_inbound_routing() -> None:
    errs = asyncio.run(_async_b_inbound_routing())
    assert not errs, f"段 B 失败: {errs}"


def test_c_outbound_hook() -> None:
    errs = asyncio.run(_async_c_outbound_hook())
    assert not errs, f"段 C 失败: {errs}"


def test_d_cleanup() -> None:
    errs = asyncio.run(_async_d_cleanup())
    assert not errs, f"段 D 失败: {errs}"


_PROBE: dict[str, Any] = {}


def main() -> int:
    """直接 ``python3 tests/test_im_19c_gateway_api.py`` 跑（非 pytest）."""
    global _PROBE
    print("=" * 60)
    print("任务19c — IM 网关核心 + API 契约测试")
    print("=" * 60)

    all_errs: list[str] = []

    print("\n[A] API CRUD + 脱敏 + 启停 + test 端点")
    _PROBE = {}
    all_errs += asyncio.run(_async_a_crud_masking_test())

    print("\n[B] gateway.deliver_inbound 入站路由")
    _PROBE = {}
    all_errs += asyncio.run(_async_b_inbound_routing())

    print("\n[C] 出站钩子（maybe_deliver_outbound + reply.py 挂点）")
    _PROBE = {}
    all_errs += asyncio.run(_async_c_outbound_hook())

    print("\n[D] 清理")
    all_errs += asyncio.run(_async_d_cleanup())

    print("\n" + "=" * 60)
    if all_errs:
        print(f"FAIL — {len(all_errs)} 段断言失败:")
        for e in all_errs:
            print(f"  - {e}")
        return 1
    print("PASS — API CRUD/脱敏/test + 入站路由分流 + 出站钩子真链路全契约通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
