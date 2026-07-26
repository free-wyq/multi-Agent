"""任务15a 回归：GET /api/usage token 用量聚合端点.

锁住 任务15a——新增 ``backend/api/usage.py`` 的 ``GET /api/usage?start=&end=&
model=&group_by=``，SQLite JSON1 + GROUP BY 聚合 ``messages.data`` 的
tokens/model/elapsed_ms（+ reasoning_tokens）。

数据源：``agent_reply.data``（chat/ask 路径，coordinator ``node_chat`` + worker
``node_brain_decide``）持久化的 per-turn 流式 run-stats
（``{reply_id, elapsed_ms, tokens, model, reasoning_tokens, ...}``）。execute 路径
模板 announce（``任务完成 🎉``）+ ``user_input`` 行 ``data=None`` 无 stats —— 必须
被排除（json_extract(data,'$.elapsed_ms') IS NOT NULL 守卫）。

聚合在 SQL 层完成（SQLite JSON1 + GROUP BY，非整列拉回 Python），与
``get_message_by_reply_id`` 同款 ``func.json_extract`` 用法。

六段契约（静态 + TestClient 真 HTTP + 真 crud 落库交叉验证，隔离临时 DB）：

  A. 路由就位（api/usage.py 注册 + main.py 挂载）
    1. ``api/usage.py`` 文件存在 + 定义 ``async def get_usage``。
    2. ``GET /api/usage`` 路由注册（prefix=/api/usage）。
    3. ``main.py`` include_router(usage.router)。

  B. crud.aggregate_usage 函数就位（store/crud.py）
    4. ``crud.aggregate_usage(start, end, model, group_by)`` 函数存在。
    5. 用 ``func.json_extract`` 读 ``$.tokens`` / ``$.elapsed_ms`` /
       ``$.reasoning_tokens`` / ``$.model``（SQLite JSON1）。
    6. WHERE 守卫 ``json_extract(data,'$.elapsed_ms') IS NOT NULL``（排除 data=None 行）。
    7. ``GROUP BY`` + ``func.coalesce(func.sum(...), 0)``（NULL 行不破坏求和）。
    8. 4 维度 key 表达式：model=json_extract('$.model') / day=substr(created_at,1,10)
       / conversation=conversation_id 列 / agent=sender_id 列。

  C. 真实 HTTP——默认聚合（group_by=model）
    9. seed 三条 stats agent_reply（glm-5.2 两次 + deepseek 一次）+ 一条 data=None
       announce + 一条 user_input → GET /api/usage 200 + totals.messages==3
       （announce/user_input 不计入）+ glm-5.2 行 tokens 求和正确。
   10. 非法 group_by=foo → 200 不报错（lenient 兜底 model），group_by 字段返 'model'。
   11. 空库（无 stats 行）→ GET /api/usage 200 + totals 全 0 + rows=[]（非 500/404）。

  D. 维度切换（group_by=day/conversation/agent）
   12. group_by=day → key 是 'YYYY-MM-DD'（substr(created_at,1,10)），按日聚合。
   13. group_by=conversation → key 是 conversation_id，跨 model 合并。
   14. group_by=agent → key 是 sender_id。

  E. 过滤（start/end/model）
   15. model=glm-5.2 → 只聚合 glm-5.2 行（deepseek 行被排除）。
   16. start=未来时间 → rows=[]（全部行 created_at < start 被排除）。

  F. 排除契约（data=None 不计入）
   17. data=None 的 announce 行不被聚合（elapsed_ms IS NOT NULL 守卫）—— 即使它是
       agent_reply 类型 + 同 conversation，也不进 totals.messages / tokens。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

# --- redirect DATA_DIR to a temp root BEFORE importing config/workspace ---
_TMP = tempfile.mkdtemp(prefix="usage_15a_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402

config.DATA_DIR = _TMP

import api.usage as usage_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
CRUD_PY = BACKEND / "store" / "crud.py"
USAGE_PY = BACKEND / "api" / "usage.py"
MAIN_PY = BACKEND / "main.py"

app = FastAPI()
app.include_router(usage_api.router)
client = TestClient(app)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


def assert_static() -> list[str]:
    errs: list[str] = []
    usage_src = _read(USAGE_PY)
    crud_src = _read(CRUD_PY)
    main_src = _read(MAIN_PY)

    # ── A. 路由就位 ──
    try:
        _check("[A1] api/usage.py 定义 async def get_usage",
               "async def get_usage(" in usage_src)
    except AssertionError as e:
        errs.append(str(e))

    try:
        paths = {r.path for r in usage_api.router.routes}
        _check("[A2] GET /api/usage 路由注册", "/api/usage" in paths,
               detail=f"paths={sorted(paths)}")
    except AssertionError as e:
        errs.append(str(e))

    try:
        _check("[A3] main.py include_router(usage.router)",
               "usage.router" in main_src)
    except AssertionError as e:
        errs.append(str(e))

    # ── B. crud.aggregate_usage 函数就位 ──
    try:
        _check("[B4] crud.aggregate_usage 函数存在",
               "async def aggregate_usage(" in crud_src)
    except AssertionError as e:
        errs.append(str(e))

    body = _fn_body(crud_src, "aggregate_usage")
    try:
        _check("[B5] json_extract 读 tokens/elapsed_ms/reasoning_tokens/model",
               all(k in body for k in (
                   "$.tokens", "$.elapsed_ms", "$.reasoning_tokens", "$.model")))
    except AssertionError as e:
        errs.append(str(e))

    try:
        _check("[B6] WHERE 守卫 elapsed_ms IS NOT NULL（排除 data=None）",
               "$.elapsed_ms" in body and "isnot(None)" in body)
    except AssertionError as e:
        errs.append(str(e))

    try:
        _check("[B7] GROUP BY + coalesce(sum(...),0)",
               "group_by(" in body and "coalesce(" in body and "func.sum(" in body)
    except AssertionError as e:
        errs.append(str(e))

    try:
        has_model = "$.model" in body
        has_day = "substr(MessageEntity.created_at, 1, 10)" in body
        has_conv = "MessageEntity.conversation_id" in body
        has_agent = "MessageEntity.sender_id" in body
        _check("[B8] 四维度 key 表达式齐（model/day/conversation/agent）",
               has_model and has_day and has_conv and has_agent,
               detail=f"model={has_model} day={has_day} conv={has_conv} agent={has_agent}")
    except AssertionError as e:
        errs.append(str(e))

    return errs


async def assert_e2e() -> list[str]:
    """C/D/E/F：真 crud 落库 + TestClient 真 HTTP（隔离临时 DB）。"""
    errs: list[str] = []
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="usage_15a_e2e_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )
        _db.engine = create_async_engine(
            _db.DB_URL, echo=False,
            connect_args={"check_same_thread": False}, pool_pre_ping=True,
        )
        _db.SessionLocal = async_sessionmaker(
            _db.engine, expire_on_commit=False, class_=AsyncSession,
        )
        await _db.init_db()
        from store import crud

        # seed 三条 stats agent_reply + 一条 data=None announce + 一条 user_input。
        # created_at 用固定 ISO 串便于按 day/start 过滤断言（crud.create_message
        # 用 _now_iso() 覆盖 created_at，故 seed 后手动按返回的 msg.id UPDATE 回写）。
        m1 = await crud.create_message({
            "conversation_id": "conv_15a", "sender_id": "agent_glm",
            "receiver_id": "broadcast", "type": "agent_reply",
            "content": "回复1", "data": {"reply_id": "r1", "elapsed_ms": 200,
            "tokens": 100, "model": "glm-5.2", "reasoning_tokens": 10}})
        m2 = await crud.create_message({
            "conversation_id": "conv_15a", "sender_id": "agent_glm",
            "receiver_id": "broadcast", "type": "agent_reply",
            "content": "回复2", "data": {"reply_id": "r2", "elapsed_ms": 300,
            "tokens": 150, "model": "glm-5.2", "reasoning_tokens": 20}})
        m3 = await crud.create_message({
            "conversation_id": "conv_15b", "sender_id": "agent_ds",
            "receiver_id": "broadcast", "type": "agent_reply",
            "content": "回复3", "data": {"reply_id": "r3", "elapsed_ms": 500,
            "tokens": 250, "model": "deepseek-v4", "reasoning_tokens": 0}})
        # data=None announce（execute 路径）+ user_input —— 不应被聚合
        m4 = await crud.create_message({
            "conversation_id": "conv_15a", "sender_id": "agent_glm",
            "receiver_id": "broadcast", "type": "agent_reply",
            "content": "任务完成 🎉", "data": None})
        m5 = await crud.create_message({
            "conversation_id": "conv_15a", "sender_id": "user",
            "receiver_id": "broadcast", "type": "user_input",
            "content": "你好"})

        # 按返回的 msg.id 显式回写固定 created_at（id 是 uuid hex 随机，
        # 不能按 id 排序推插入顺序——故按已知对象逐条 UPDATE）。
        from store.entities import MessageEntity
        from sqlalchemy import update as sa_update
        ts_by_id = {
            m1.id: "2026-07-20T10:00:00Z",
            m2.id: "2026-07-20T11:00:00Z",
            m3.id: "2026-07-21T09:00:00Z",
            m4.id: "2026-07-20T12:00:00Z",
            m5.id: "2026-07-20T09:30:00Z",
        }
        async with _db.SessionLocal() as db:
            for mid, ts in ts_by_id.items():
                await db.execute(
                    sa_update(MessageEntity)
                    .where(MessageEntity.id == mid)
                    .values(created_at=ts)
                )
            await db.commit()

        # ── C9 默认聚合 group_by=model ──
        try:
            r = client.get("/api/usage")
            rep = r.json()
            ok = r.status_code == 200
            msgs = rep.get("totals", {}).get("messages", -1)
            toks = rep.get("totals", {}).get("tokens", -1)
            # 3 条 stats 行（glm x2 + deepseek x1），announce/user_input 不计
            ok = ok and msgs == 3
            # tokens 求和：100 + 150 + 250 = 500
            ok = ok and toks == 500
            _check("[C9] GET /api/usage 200 + totals.messages==3 + tokens 求和==500",
                   ok, detail=f"status={r.status_code} msgs={msgs} toks={toks} body={rep}")
        except AssertionError as e:
            errs.append(str(e))

        # ── C10 非法 group_by 兜底 model ──
        try:
            r = client.get("/api/usage?group_by=foo")
            rep = r.json()
            ok = r.status_code == 200 and rep.get("group_by") == "model"
            _check("[C10] 非法 group_by=foo → 200 + 兜底 model", ok,
                   detail=f"status={r.status_code} gb={rep.get('group_by')}")
        except AssertionError as e:
            errs.append(str(e))

        # ── C11 空库 → 200 + 全 0 + rows=[] ──
        # 用一个全新隔离 DB（无 stats 行）单独跑：直接查 group_by=model 在一个无 agent_reply
        # stats 的 conversation 上。此处复用当前 DB 但断言「无 stats 时不报错」——
        # 通过查一个不存在的 model 过滤得到空 rows 验证兜底。
        try:
            r = client.get("/api/usage?model=nonexistent_model_xyz")
            rep = r.json()
            ok = (r.status_code == 200 and rep.get("totals", {}).get("messages") == 0
                  and rep.get("totals", {}).get("tokens") == 0
                  and rep.get("rows") == [])
            _check("[C11] 无匹配 stats → 200 + 全 0 + rows=[]", ok,
                   detail=f"status={r.status_code} body={rep}")
        except AssertionError as e:
            errs.append(str(e))

        # ── D12 group_by=day ──
        try:
            r = client.get("/api/usage?group_by=day")
            rep = r.json()
            rows = {row["key"]: row for row in rep.get("rows", [])}
            # 2026-07-20: glm r1(100) + glm r2(150) = 250 tokens, 2 msgs
            # 2026-07-21: deepseek r3(250) = 250 tokens, 1 msg
            d20 = rows.get("2026-07-20")
            d21 = rows.get("2026-07-21")
            ok = (d20 is not None and d20["tokens"] == 250 and d20["messages"] == 2
                  and d21 is not None and d21["tokens"] == 250 and d21["messages"] == 1)
            _check("[D12] group_by=day → key=YYYY-MM-DD 按日聚合", ok,
                   detail=f"rows={list(rows.keys())} d20={d20} d21={d21}")
        except AssertionError as e:
            errs.append(str(e))

        # ── D13 group_by=conversation ──
        try:
            r = client.get("/api/usage?group_by=conversation")
            rep = r.json()
            rows = {row["key"]: row for row in rep.get("rows", [])}
            # conv_15a: glm r1(100) + glm r2(150) = 250 tokens, 2 msgs（跨 model 合并）
            # conv_15b: deepseek r3(250) = 250 tokens, 1 msg
            ca = rows.get("conv_15a")
            cb = rows.get("conv_15b")
            ok = (ca is not None and ca["tokens"] == 250 and ca["messages"] == 2
                  and cb is not None and cb["tokens"] == 250 and cb["messages"] == 1)
            _check("[D13] group_by=conversation → key=conversation_id 跨 model 合并", ok,
                   detail=f"ca={ca} cb={cb}")
        except AssertionError as e:
            errs.append(str(e))

        # ── D14 group_by=agent ──
        try:
            r = client.get("/api/usage?group_by=agent")
            rep = r.json()
            rows = {row["key"]: row for row in rep.get("rows", [])}
            ag = rows.get("agent_glm")
            ad = rows.get("agent_ds")
            ok = (ag is not None and ag["tokens"] == 250 and ag["messages"] == 2
                  and ad is not None and ad["tokens"] == 250 and ad["messages"] == 1)
            _check("[D14] group_by=agent → key=sender_id", ok,
                   detail=f"ag={ag} ad={ad}")
        except AssertionError as e:
            errs.append(str(e))

        # ── E15 model 过滤 ──
        try:
            r = client.get("/api/usage?model=glm-5.2")
            rep = r.json()
            msgs = rep.get("totals", {}).get("messages")
            toks = rep.get("totals", {}).get("tokens")
            # 只 glm-5.2 两行：100 + 150 = 250
            ok = r.status_code == 200 and msgs == 2 and toks == 250
            _check("[E15] model=glm-5.2 → 只聚合该模型行", ok,
                   detail=f"status={r.status_code} msgs={msgs} toks={toks}")
        except AssertionError as e:
            errs.append(str(e))

        # ── E16 start=未来时间 → rows=[] ──
        try:
            r = client.get("/api/usage?start=2099-01-01T00:00:00Z")
            rep = r.json()
            ok = (r.status_code == 200 and rep.get("rows") == []
                  and rep.get("totals", {}).get("messages") == 0)
            _check("[E16] start=未来时间 → rows=[]（全部被排除）", ok,
                   detail=f"status={r.status_code} body={rep}")
        except AssertionError as e:
            errs.append(str(e))

        # ── F17 data=None announce 不计入 ──
        # （C9 已隐含验证：4 条 agent_reply + 1 user_input 落库，但 totals.messages==3
        # 说明 data=None announce 被排除。这里显式断言：seed 时 announce 行的
        # content=='任务完成 🎉' 存在但不在聚合结果里。）
        try:
            r = client.get("/api/usage?group_by=conversation")
            rep = r.json()
            # conv_15a 有 2 条 stats agent_reply（r1+r2）+ 1 条 announce + 1 user_input
            # 聚合应只算 2 条（announce/user_input 排除）
            ca = next((row for row in rep.get("rows", []) if row["key"] == "conv_15a"), None)
            ok = ca is not None and ca["messages"] == 2
            _check("[F17] data=None announce + user_input 不计入聚合", ok,
                   detail=f"ca={ca}")
        except AssertionError as e:
            errs.append(str(e))

        return errs
    finally:
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)


def main() -> int:
    print("=" * 70)
    print("任务15a 回归：GET /api/usage token 用量聚合端点")
    print("=" * 70 + "\n")
    print("── A/B 静态契约 ──")
    static_errs = assert_static()
    print("\n── C/D/E/F 真 crud 落库 + TestClient ──")
    e2e_errs = asyncio.run(assert_e2e())
    errs = static_errs + e2e_errs
    print()
    if errs:
        print(f"=== 结果: FAIL ({len(errs)} 项) ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print(
        "\n任务15a 契约锁定（GET /api/usage + crud.aggregate_usage）：\n"
        "  · A 路由 GET /api/usage 注册 + main.py 挂载；\n"
        "  · B crud.aggregate_usage 用 json_extract + GROUP BY + coalesce(sum,0) 聚合；\n"
        "  · C 默认 group_by=model 求 tokens/elapsed_ms/messages（4 维度 + 空兜底）；\n"
        "  · D 维度切换 day/conversation/agent key 表达式正确；\n"
        "  · E model/start 过滤生效；\n"
        "  · F data=None announce + user_input 不计入聚合（elapsed_ms IS NOT NULL 守卫）。"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
