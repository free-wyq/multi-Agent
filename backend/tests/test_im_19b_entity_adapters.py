"""任务19b — IM 实体 + 三 mock adapter 契约测试.

锁住任务19b 交付物（``docs/im-gateway-design.md`` §3/§4）：

  A. ImChannelEntity 建表 + 字段默认值（enabled=0 / outbound_log=1 /
     target_kind=single / config JSON 持久化）—— ``im_channels`` 表由
     ``create_all`` 建（新库）/ ``_migrate_schema`` 兜底（老库），本段建表后真插
     真读，锁字段默认 + JSON 读写。
  B. ADAPTERS 注册表 = {wechat, dingtalk, feishu}，每 adapter 实现
     ``ImChannelAdapter`` 协议（runtime_checkable isinstance True）+ ``platform``
     类属性对齐 key。
  C. 三 adapter ``parse_inbound`` 按平台字段抽取（wechat: FromUserName/Content；
     dingtalk: text.content/senderId/conversationId；feishu:
     event.message.chat_id + content JSON 字符串解析）—— 平台原始回调 →
     InboundMessage 标准化的真值。
  D. 三 adapter ``send_outbound`` 走 ``logger.info`` mock（不真发 HTTP）——
     caplog 拦截 ``[im:{platform}] outbound → {target}: {content}`` 行（任务19e
     e2e 的断言点）；``verify_inbound`` mock 恒 True。

确定性（不依赖 live server / 真实 LLM / 真实 IM 平台）。隔离 DB（与 18b/18c 同款
MULTI_AGENT_DATA_DIR 临时目录 + reload(_db) + create_async_engine + init_db）。
caplog 拦 ``multi-agent.im`` logger——send_outbound 是真 logger.info 调用，caplog
是真实链路断言点（不 monkeypatch adapter，保持任务19e 会复用的真链路）。

pytest 收集：``test_a``..``test_d`` + ``main()``。每段独立 ``asyncio.run`` + 独立
``_init_isolated_db``，每段末 ``assert not errs``（pytest 真门，与 18b/18c 同）。
``main()`` 顺序调（直接 ``python3 tests/test_im_19b_entity_adapters.py`` 也门）。
无 conftest / 无 pytest-asyncio。
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

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="im_19b_")
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


# ── A. ImChannelEntity 建表 + 字段默认值 ──────────────────────────────────
async def _async_a_entity_defaults() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store.entities import ImChannelEntity
    from store.database import SessionLocal

    cfg = {"app_id": "wx_app", "app_secret": "secret_x", "verify_token": "vt_y"}
    e = ImChannelEntity(
        id="imc_probe_a",
        name="探针渠道 A",
        platform="wechat",
        config=cfg,
        target_conversation_id="conv_probe_a",
    )
    async with SessionLocal() as db:
        db.add(e)
        await db.commit()
    async with SessionLocal() as db:
        row = await db.get(ImChannelEntity, "imc_probe_a")
        ok = _check(
            "A1 ImChannelEntity 行落库可读回",
            row is not None,
        )
        if not ok:
            errs.append("[A1] ImChannelEntity 行未落库")
            return errs
        assert row is not None  # narrow for mypy
        ok = _check(
            "A2 enabled 默认 0（channel 显式 enable 前不入站）",
            row.enabled == 0, f"enabled={row.enabled}",
        ) and ok
        ok = _check(
            "A3 outbound_log 默认 1（mock 出站日志开）",
            row.outbound_log == 1, f"outbound_log={row.outbound_log}",
        ) and ok
        ok = _check(
            "A4 target_kind 默认 single",
            row.target_kind == "single", f"target_kind={row.target_kind}",
        ) and ok
        ok = _check(
            "A5 target_agent_id 默认空串",
            row.target_agent_id == "", f"target_agent_id={row.target_agent_id!r}",
        ) and ok
        ok = _check(
            "A6 config JSON 持久化（含 app_secret 明文落库）",
            row.config == cfg, f"config={row.config}",
        ) and ok
        ok = _check(
            "A7 platform 落库",
            row.platform == "wechat", f"platform={row.platform}",
        ) and ok
        ok = _check(
            "A8 target_conversation_id 落库",
            row.target_conversation_id == "conv_probe_a",
        ) and ok
        ok = _check(
            "A9 created_at/updated_at 自动填充",
            bool(row.created_at) and bool(row.updated_at),
            f"created={row.created_at} updated={row.updated_at}",
        ) and ok
        ok = _check(
            "A10 session_bindings/metadata_ 默认 None（可空）",
            row.session_bindings is None and row.metadata_ is None,
        ) and ok

    if not ok:
        errs.append("[A] ImChannelEntity 默认值断言失败")
    return errs


# ── B. ADAPTERS 注册表 + 协议符合 ──────────────────────────────────────────
async def _async_b_adapters_registry() -> list[str]:
    errs: list[str] = []
    from engine.im import (
        ADAPTERS,
        DingtalkAdapter,
        FeishuAdapter,
        ImChannelAdapter,
        InboundMessage,
        OutboundPayload,
        WechatAdapter,
    )

    ok = _check(
        "B1 ADAPTERS 含三平台 wechat/dingtalk/feishu",
        set(ADAPTERS) == {"wechat", "dingtalk", "feishu"},
        f"keys={sorted(ADAPTERS)}",
    )
    expected_classes = {
        "wechat": WechatAdapter,
        "dingtalk": DingtalkAdapter,
        "feishu": FeishuAdapter,
    }
    for plat, cls in expected_classes.items():
        ok = _check(
            f"B2 ADAPTERS[{plat}] == {cls.__name__}",
            ADAPTERS[plat] is cls,
        ) and ok
        inst = cls()
        ok = _check(
            f"B3 {cls.__name__}.platform == {plat!r}",
            inst.platform == plat,
            f"platform={inst.platform!r}",
        ) and ok
        ok = _check(
            f"B4 {cls.__name__} 符合 ImChannelAdapter 协议 (runtime_checkable)",
            isinstance(inst, ImChannelAdapter),
        ) and ok

    # 协议 DTO 是 dataclass（有字段）
    im = InboundMessage(platform_user_id="u", platform_session_id="s", content="c")
    op = OutboundPayload(target="t", content="c")
    ok = _check(
        "B5 InboundMessage/OutboundPayload 是 dataclass（有声明字段）",
        hasattr(im, "platform_user_id") and hasattr(op, "target") and im.msg_type == "text",
    ) and ok

    # 未知 platform 不在注册表（gateway 19c 会据此拒/兜底）
    ok = _check(
        "B6 未知 platform 不在 ADAPTERS（无假 adapter）",
        "slack" not in ADAPTERS,
    ) and ok

    if not ok:
        errs.append("[B] ADAPTERS 注册表 / 协议符合断言失败")
    return errs


# ── C. parse_inbound 平台字段抽取真值 ──────────────────────────────────────
async def _async_c_parse_inbound() -> list[str]:
    errs: list[str] = []
    from engine.im import ADAPTERS

    # wechat: FromUserName / Content / MsgType
    w = ADAPTERS["wechat"]()
    m = w.parse_inbound(
        json.dumps({"FromUserName": "u_wx", "Content": "你好微信", "MsgType": "text"}),
        {},
    )
    ok = _check(
        "C1 wechat parse: platform_user_id=FromUserName",
        m.platform_user_id == "u_wx", f"got={m.platform_user_id!r}",
    )
    ok = _check(
        "C2 wechat parse: content=Content",
        m.content == "你好微信", f"got={m.content!r}",
    ) and ok
    ok = _check(
        "C3 wechat parse: msg_type=MsgType",
        m.msg_type == "text", f"got={m.msg_type!r}",
    ) and ok
    ok = _check(
        "C4 wechat parse: platform_session_id 默认回落到 user（单聊）",
        m.platform_session_id == "u_wx", f"got={m.platform_session_id!r}",
    ) and ok
    ok = _check(
        "C5 wechat parse: raw 保留原始 body",
        m.raw is not None and m.raw.get("FromUserName") == "u_wx",
    ) and ok

    # dingtalk: 嵌套 text.content / senderId / conversationId
    d = ADAPTERS["dingtalk"]()
    m = d.parse_inbound(
        json.dumps({
            "msgtype": "text",
            "text": {"content": "钉钉你好"},
            "senderId": "s_dt",
            "senderNick": "张三",
            "conversationId": "c_dt",
        }),
        {},
    )
    ok = _check(
        "C6 dingtalk parse: content=text.content",
        m.content == "钉钉你好", f"got={m.content!r}",
    ) and ok
    ok = _check(
        "C7 dingtalk parse: platform_user_id=senderId",
        m.platform_user_id == "s_dt", f"got={m.platform_user_id!r}",
    ) and ok
    ok = _check(
        "C8 dingtalk parse: platform_session_id=conversationId",
        m.platform_session_id == "c_dt", f"got={m.platform_session_id!r}",
    ) and ok
    ok = _check(
        "C9 dingtalk parse: senderId 缺时回落 senderNick",
        d.parse_inbound(json.dumps({"senderNick": "李四"}), {}).platform_user_id == "李四",
    ) and ok

    # feishu: event.message.chat_id + content 是 JSON 字符串 {"text":"..."}
    f = ADAPTERS["feishu"]()
    body = {
        "event": {
            "message": {
                "chat_id": "oc_feishu",
                "message_type": "text",
                "content": json.dumps({"text": "飞书你好"}),
            },
            "sender": {"sender_id": {"open_id": "ou_feishu"}},
        }
    }
    m = f.parse_inbound(json.dumps(body), {})
    ok = _check(
        "C10 feishu parse: content 解析 content JSON 字符串的 text",
        m.content == "飞书你好", f"got={m.content!r}",
    ) and ok
    ok = _check(
        "C11 feishu parse: platform_session_id=event.message.chat_id",
        m.platform_session_id == "oc_feishu", f"got={m.platform_session_id!r}",
    ) and ok
    ok = _check(
        "C12 feishu parse: platform_user_id=sender.sender_id.open_id",
        m.platform_user_id == "ou_feishu", f"got={m.platform_user_id!r}",
    ) and ok

    # bytes body 容忍（ASGI 层送 bytes）
    m = w.parse_inbound(b'{"FromUserName":"ub","Content":"hb"}', {})
    ok = _check(
        "C13 bytes body 解码后正确解析",
        m.platform_user_id == "ub" and m.content == "hb",
        f"got user={m.platform_user_id!r} content={m.content!r}",
    ) and ok

    # 空 body 容忍（不崩，回落空字段）
    m = w.parse_inbound(b"", {})
    ok = _check(
        "C14 空 body 不崩（回落空 InboundMessage）",
        m.content == "" and m.platform_user_id == "",
    ) and ok

    # 非 JSON body 容忍（档四 WeChat XML 在 mock 阶段也走 _decode_body 回落 {}）
    m = w.parse_inbound("<xml>not json</xml>", {})
    ok = _check(
        "C15 非 JSON body 不崩（回落 {}）",
        m.content == "" and m.platform_user_id == "",
    ) and ok

    if not ok:
        errs.append("[C] parse_inbound 平台字段抽取断言失败")
    return errs


# ── D. verify_inbound mock True + send_outbound logger.info mock（caplog） ─
async def _async_d_outbound_mock(caplog_records: list[logging.LogRecord]) -> list[str]:
    errs: list[str] = []
    from engine.im import ADAPTERS, OutboundPayload

    # verify_inbound mock 恒 True（echo server 不签名）
    for plat, cls in ADAPTERS.items():
        inst = cls()
        ok = _check(
            f"D1 {plat} verify_inbound mock 恒 True（任意 headers/body）",
            inst.verify_inbound({"X": "y"}, b"whatever") is True,
        )
        if not ok:
            errs.append(f"[D1] {plat} verify_inbound 非 True")
            return errs

    # send_outbound 走 logger.info（caplog 拦截）—— 三平台各发一条
    log = logging.getLogger("multi-agent.im")
    # 接一个 handler 把记录收集进 caplog_records（caplog fixture 在 pytest 函数签名里，
    # 但本测走 main() 直跑模式——用模块级 list + Handler 桥接，pytest 下也 work）
    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caplog_records.append(record)

    collector = _Collector()
    collector.setLevel(logging.INFO)
    log.addHandler(collector)
    prev_level = log.level
    log.setLevel(logging.INFO)
    try:
        sent = []
        for plat, cls in ADAPTERS.items():
            inst = cls()
            payload = OutboundPayload(target=f"sess_{plat}", content=f"reply_{plat}")
            await inst.send_outbound(payload, {"webhook_url": "http://mock/ignored"})
            sent.append(plat)
    finally:
        log.removeHandler(collector)
        log.setLevel(prev_level)

    msgs = [r.getMessage() for r in caplog_records if r.name == "multi-agent.im"]
    for plat in ("wechat", "dingtalk", "feishu"):
        expected = f"[im:{plat}] outbound → sess_{plat}: reply_{plat}"
        ok = _check(
            f"D2 {plat} send_outbound 发出 mock 日志行: {expected}",
            any(m == expected for m in msgs),
            f"got={msgs}",
        )
        if not ok:
            errs.append(f"[D2] {plat} send_outbound 未发出预期日志行")

    # 日志行格式严格（19e e2e 会 grep 此格式）
    ok = _check(
        "D3 日志行格式严格匹配 '[im:{platform}] outbound → {target}: {content}'",
        all(
            f"[im:{p}] outbound → sess_{p}: reply_{p}" in msgs
            for p in ("wechat", "dingtalk", "feishu")
        ),
        f"msgs={msgs}",
    )
    if not ok:
        errs.append("[D3] 日志行格式不匹配 19e 断言点")

    # channel_config 透传但 mock 不真用（不发 HTTP）—— 无异常即证明
    ok = _check(
        "D4 send_outbound 对 channel_config 不报错（mock 不真发 HTTP）",
        True,
    )
    if not ok:
        errs.append("[D4] send_outbound 异常")

    return errs


# ── pytest 入口（test_a..test_d + main） ────────────────────────────────────


def test_a_entity_defaults() -> None:
    errs = asyncio.run(_async_a_entity_defaults())
    assert not errs, f"段 A 失败: {errs}"


def test_b_adapters_registry() -> None:
    errs = asyncio.run(_async_b_adapters_registry())
    assert not errs, f"段 B 失败: {errs}"


def test_c_parse_inbound() -> None:
    errs = asyncio.run(_async_c_parse_inbound())
    assert not errs, f"段 C 失败: {errs}"


def test_d_outbound_mock() -> None:
    records: list[logging.LogRecord] = []
    errs = asyncio.run(_async_d_outbound_mock(records))
    assert not errs, f"段 D 失败: {errs}"


def main() -> int:
    """直接 ``python3 tests/test_im_19b_entity_adapters.py`` 跑（非 pytest）."""
    print("=" * 60)
    print("任务19b — IM 实体 + 三 mock adapter 契约测试")
    print("=" * 60)

    all_errs: list[str] = []

    print("\n[A] ImChannelEntity 建表 + 字段默认值")
    all_errs += asyncio.run(_async_a_entity_defaults())

    print("\n[B] ADAPTERS 注册表 + 协议符合")
    all_errs += asyncio.run(_async_b_adapters_registry())

    print("\n[C] parse_inbound 平台字段抽取")
    all_errs += asyncio.run(_async_c_parse_inbound())

    print("\n[D] verify_inbound mock + send_outbound logger.info mock")
    records: list[logging.LogRecord] = []
    all_errs += asyncio.run(_async_d_outbound_mock(records))

    print("\n" + "=" * 60)
    if all_errs:
        print(f"FAIL — {len(all_errs)} 段断言失败:")
        for e in all_errs:
            print(f"  - {e}")
        return 1
    print("PASS — IM 实体建表默认值 + ADAPTERS 注册表 + 三 adapter")
    print("       parse_inbound 真值 + send_outbound logger.info mock 全契约通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
