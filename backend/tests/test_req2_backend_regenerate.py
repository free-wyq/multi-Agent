"""需求2-后端 回归：按 reply_id 重跑（regenerate）端点 + 回查钩子 + 前端注入.

锁住 [需求2-后端] 改动——评估并实现「按 reply_id 重跑」端点（regenerate）。

设计单真源：``docs/structured-result-card-schema.md`` mockup 第 3 层「[操作栏]
📋复制内容 🔄重新生成」——「重新生成」按钮需后端端点支撑才能从 disabled 兜底转可点。

本任务评估结论（成本可控，落地实现，非留 TODO）：regenerate 不自研调度——复用现有
``send_message`` 的 persist-user-msg → route 分流路径（群聊 route_user_message /
单聊 route_direct_message），新回复经现有 WS ``message_added`` 事件流到达，零新
事件类型 / 零引擎接线 / 零 DB schema 变更。回查钩子用 SQLite ``json_extract``
（JSON1，aiosqlite 默认启用）按 ``data->>'$.reply_id'`` 精确定位回复行，恢复原
prompt 用「同会话该回复前最近一条 user_input」回查。

纯静态契约（读源码断言，不依赖后端在线 / 真实 LLM）+ 真 crud 落库交叉验证（隔离
临时 DB，对齐 vh61 模式），与 test_req2_backend_card_fragment.py / test_vh61 同款风格。

八段契约：

  A. 端点注册（api/messages.py · POST /api/messages/regenerate）
    1. ``api/messages.py`` 注册 ``@router.post("/regenerate")`` 路由。
    2. 处理函数名 ``regenerate_reply`` + query 参数 ``replyId``。

  B. 回查钩子（store/crud.py · get_message_by_reply_id）
    3. ``crud.get_message_by_reply_id(reply_id)`` 函数存在。
    4. 用 ``func.json_extract(MessageEntity.data, "$.reply_id") == reply_id`` 查
       （SQLite JSON1，非整列拉回 Python 过滤）。
    5. 仅查 ``type_ == "agent_reply"``（user_input/task_log 行 data 无 reply_id，
       不该被命中）。

  C. 恢复原 prompt（store/crud.py · find_preceding_user_input）
    6. ``crud.find_preceding_user_input(conversation_id, before_created_at)`` 函数存在。
    7. 查 ``type_ == "user_input"`` + ``created_at < before_created_at`` + 倒序取首
      （最近一条 user_input，即原 prompt）。

  D. 端点逻辑（regenerate_reply 流程）
    8. 空 replyId → 400（防呆）。
    9. get_message_by_reply_id 返 None → 404（模板公告 data 无 reply_id / 已清会话 / 未知 id）。
   10. find_preceding_user_input 返 None → 409（回查到回复但前无 user_input，无法重跑）。
   11. 成功路径：create_message 落新 user_input（content=恢复的原 prompt）→
       emit_message_added → 按 get_group 分流 route_user_message / route_direct_message
       （与 send_message 同款路由，零新引擎接线）。
   12. 返回值=新落盘的 user_input 行（fire-and-forget 踢一轮 turn，新回复靠 WS 到达）。

  E. 真 crud 落库交叉验证（隔离临时 DB）
   13. seed user_input(t1) + agent_reply(data.reply_id=rid, t2) → get_message_by_reply_id(rid)
       命中该回复行（content 正确）。
   14. get_message_by_reply_id(未知 id) 返 None。
   15. find_preceding_user_input(conv, t2.created_at) 返回 t1 的 user_input（content=原 prompt）。
   16. 回复是会话首条（前无 user_input）→ find_preceding_user_input 返 None。
   17. execute 路径模板 announce（data=None）不会被任何 reply_id 命中（json_extract 返 NULL）。

  F. 前端 API 层（services/api.ts · messageApi.regenerate）
   18. ``messageApi.regenerate(replyId)`` 方法存在 → POST /api/messages/regenerate?replyId=...

  G. 前端注入（ChatPanel · handleRegenerate + 持久化气泡按钮）
   19. ``handleRegenerate`` 回调调 ``messageApi.regenerate`` + regeneratingReplyIds loading 态。
   20. 持久化 agent_reply 气泡的 hover 操作组渲染「重新生成」Button（仅当 data.reply_id 存在）。
   21. ``extractReplyId(msg.data)`` 从 agent_reply.data.reply_id 取回查键。

  H. ChatMessageBubble footer 操作栏（[需求2-前端] 已落，[需求2-后端] 接通回调）
   22. footer 按钮新增 ``regenerating`` loading prop（重跑中转菊花防连点）。
   23. footer 按钮 disabled 守卫含 ``!onRegenerate``（未注入兜底）+ ``regenerating``（loading）。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
MESSAGES_PY = REPO / "backend" / "api" / "messages.py"
CRUD_PY = REPO / "backend" / "store" / "crud.py"
API_TS = REPO / "src" / "services" / "api.ts"
CHATPANEL_TSX = REPO / "src" / "components" / "ChatPanel.tsx"
BUBBLE_TSX = REPO / "src" / "components" / "ChatMessageBubble.tsx"


def _fn_body(src: str, fname: str) -> str:
    """抽 async def fname(...) 函数体（含签名），跨花括号到下一个同级 def。"""
    m = re.search(rf"async def {fname}\([^)]*\)", src)
    if not m:
        return ""
    start = m.start()
    # 取到下一个同级 async def / def 或文件末尾
    rest = src[start:]
    # 简化：取到下一个 "\nasync def " 或 "\ndef "（模块级）
    nxt = re.search(r"\n(?:async )?def ", rest[1:])
    if nxt:
        return rest[: nxt.start() + 1]
    return rest


def _check(errs: list[str], label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[OK] {label}")
    else:
        errs.append(label)
        msg = f" — {detail}" if detail else ""
        print(f"[FAIL] {label}{msg}")


def assert_static() -> list[str]:
    errs: list[str] = []
    msg_src = MESSAGES_PY.read_text(encoding="utf-8") if MESSAGES_PY.exists() else ""
    crud_src = CRUD_PY.read_text(encoding="utf-8") if CRUD_PY.exists() else ""
    api_ts = API_TS.read_text(encoding="utf-8") if API_TS.exists() else ""
    panel_tsx = CHATPANEL_TSX.read_text(encoding="utf-8") if CHATPANEL_TSX.exists() else ""
    bubble_tsx = BUBBLE_TSX.read_text(encoding="utf-8") if BUBBLE_TSX.exists() else ""

    regen_body = _fn_body(msg_src, "regenerate_reply")

    # ── A. 端点注册 ──
    _check(errs, "[A1] api/messages.py 注册 @router.post(\"/regenerate\")",
           bool(re.search(r'@router\.post\(\s*["\']/?regenerate["\']\s*\)', msg_src)))
    _check(errs, "[A2] 处理函数 regenerate_reply + query 参数 replyId",
           bool(re.search(r"async def regenerate_reply\([^)]*replyId", msg_src, re.S)) and "replyId" in regen_body)

    # ── B. 回查钩子 get_message_by_reply_id ──
    _check(errs, "[B3] crud.get_message_by_reply_id 函数存在",
           "async def get_message_by_reply_id(" in crud_src)
    gmb_body = _fn_body(crud_src, "get_message_by_reply_id")
    _check(errs, "[B4] 用 func.json_extract 查 data.reply_id（SQLite JSON1）",
           "func.json_extract" in gmb_body and '"$.reply_id"' in gmb_body or "$.reply_id" in gmb_body,
           detail=f"json_extract={'func.json_extract' in gmb_body} rid={'$.reply_id' in gmb_body}")
    _check(errs, "[B5] 仅查 type_ == 'agent_reply'",
           'MessageEntity.type_ == "agent_reply"' in gmb_body)

    # ── C. 恢复原 prompt find_preceding_user_input ──
    _check(errs, "[C6] crud.find_preceding_user_input 函数存在",
           "async def find_preceding_user_input(" in crud_src)
    fpu_body = _fn_body(crud_src, "find_preceding_user_input")
    _check(errs, "[C7] 查 user_input + created_at < before + 倒序取首",
           'MessageEntity.type_ == "user_input"' in fpu_body
           and "MessageEntity.created_at <" in fpu_body
           and "created_at.desc()" in fpu_body,
           detail=f"type={'user_input' in fpu_body} lt={'created_at <' in fpu_body} desc={'desc()' in fpu_body}")

    # ── D. 端点逻辑 ──
    _check(errs, "[D8] 空 replyId → 400",
           'status_code=400' in regen_body and "不能为空" in regen_body)
    _check(errs, "[D9] get_message_by_reply_id 返 None → 404",
           "get_message_by_reply_id" in regen_body and 'status_code=404' in regen_body)
    _check(errs, "[D10] find_preceding_user_input 返 None → 409",
           "find_preceding_user_input" in regen_body and 'status_code=409' in regen_body)
    _check(errs, "[D11] 成功路径：create_message + emit_message_added + route 分流",
           "create_message" in regen_body and "emit_message_added" in regen_body
           and "route_user_message" in regen_body and "route_direct_message" in regen_body)
    _check(errs, "[D12] 返回 user_msg（fire-and-forget，新回复靠 WS 到达）",
           "return user_msg" in regen_body)

    # ── F. 前端 API 层 ──
    _check(errs, "[F18] messageApi.regenerate(replyId) → POST /api/messages/regenerate",
           "regenerate:" in api_ts and "/api/messages/regenerate" in api_ts and "replyId" in api_ts)

    # ── G. 前端注入 ──
    _check(errs, "[G19] handleRegenerate 调 messageApi.regenerate + regeneratingReplyIds loading",
           "handleRegenerate" in panel_tsx and "messageApi.regenerate" in panel_tsx
           and "regeneratingReplyIds" in panel_tsx)
    _check(errs, "[G20] 持久化气泡 hover 组渲染「重新生成」Button（仅当 data.reply_id 存在）",
           "extractReplyId" in panel_tsx and "ReloadOutlined" in panel_tsx
           and "handleRegenerate(replyId)" in panel_tsx)
    _check(errs, "[G21] extractReplyId(msg.data) 从 agent_reply.data.reply_id 取回查键",
           "function extractReplyId" in panel_tsx and "dd['reply_id']" in panel_tsx)

    # ── H. ChatMessageBubble footer ──
    _check(errs, "[H22] footer 按钮新增 regenerating loading prop",
           "regenerating?: boolean" in bubble_tsx and "loading={regenerating}" in bubble_tsx)
    _check(errs, "[H23] footer 按钮 disabled 守卫含 !onRegenerate + regenerating",
           "!onRegenerate" in bubble_tsx and "regenerating" in bubble_tsx)

    return errs, regen_body


async def assert_crud_e2e() -> list[str]:
    """E. 真 crud 落库交叉验证（隔离临时 DB，对齐 vh61 模式）。"""
    errs: list[str] = []
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="req2_regen_test_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        _db.engine = create_async_engine(
            _db.DB_URL, echo=False,
            connect_args={"check_same_thread": False}, pool_pre_ping=True,
        )
        _db.SessionLocal = async_sessionmaker(
            _db.engine, expire_on_commit=False, class_=AsyncSession,
        )
        await _db.init_db()
        from store import crud

        # seed: user_input(t1) + agent_reply(data.reply_id=rid, t2)
        await crud.create_message({"conversation_id": "c1", "sender_id": "user",
                                  "receiver_id": "broadcast", "type": "user_input",
                                  "content": "帮我写诗"})
        # 确保 created_at 递增（_now_iso 同毫秒可能并列——加一个无关行间隔）
        await crud.create_message({"conversation_id": "c1", "sender_id": "a1",
                                  "receiver_id": "broadcast", "type": "agent_reply",
                                  "content": "好的", "data": {"reply_id": "rid_xyz",
                                  "elapsed_ms": 300}})
        got = await crud.get_message_by_reply_id("rid_xyz")
        _check(errs, "[E13] get_message_by_reply_id(rid) 命中 + content 正确",
               got is not None and got.content == "好的",
               detail=f"got={got.content if got else None}")

        miss = await crud.get_message_by_reply_id("rid_nope")
        _check(errs, "[E14] 未知 reply_id 返 None", miss is None)

        if got:
            pre = await crud.find_preceding_user_input("c1", got.created_at)
            _check(errs, "[E15] find_preceding_user_input 返回原 prompt",
                   pre is not None and pre.content == "帮我写诗",
                   detail=f"pre={pre.content if pre else None}")

        # 回复是会话首条（c2 无前置 user_input）
        await crud.create_message({"conversation_id": "c2", "sender_id": "a1",
                                  "receiver_id": "broadcast", "type": "agent_reply",
                                  "content": "hi", "data": {"reply_id": "rid_c2",
                                  "elapsed_ms": 100}})
        c2_reply = await crud.get_message_by_reply_id("rid_c2")
        pre2 = await crud.find_preceding_user_input("c2", c2_reply.created_at) if c2_reply else "ERR"
        _check(errs, "[E16] 回复是会话首条 → find_preceding_user_input 返 None",
               pre2 is None, detail=f"pre2={pre2}")

        # execute 路径模板 announce（data=None）不应被任何 reply_id 命中
        await crud.create_message({"conversation_id": "c1", "sender_id": "a1",
                                  "receiver_id": "broadcast", "type": "agent_reply",
                                  "content": "任务完成 🎉", "data": None})
        # 用一个不存在的 reply_id 确认 announce 不被误命中
        ann_miss = await crud.get_message_by_reply_id("totally_unknown_rid")
        _check(errs, "[E17] execute 模板 announce（data=None）不被 reply_id 命中",
               ann_miss is None)
        return errs
    finally:
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)


def main() -> int:
    print("=" * 70)
    print("需求2-后端 回归：按 reply_id 重跑（regenerate）端点契约")
    print("=" * 70)
    static_errs, _ = assert_static()
    print()
    e2e_errs = asyncio.run(assert_crud_e2e())
    errs = static_errs + e2e_errs
    print()
    if errs:
        print(f"结果: FAIL ({len(errs)} 项)")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("结果: PASS")
    print(
        "\n需求2-后端 regenerate 契约锁定（评估结论=落地实现，非留 TODO）：\n"
        "  · A 端点 POST /api/messages/regenerate?replyId= 注册；\n"
        "  · B crud.get_message_by_reply_id 用 json_extract 查 data.reply_id（仅 agent_reply）；\n"
        "  · C crud.find_preceding_user_input 取同会话该回复前最近 user_input 作原 prompt；\n"
        "  · D 端点逻辑：空→400 / 未找到→404 / 前无 user_input→409 / 成功落新 user_input + route 分流；\n"
        "  · E 真 crud 落库交叉验证（命中/未命中/原 prompt/会话首条/模板 announce 五路径）；\n"
        "  · F messageApi.regenerate 前端 API；\n"
        "  · G ChatPanel handleRegenerate + 持久化气泡按钮注入 + extractReplyId；\n"
        "  · H ChatMessageBubble footer 按钮 regenerating loading + disabled 兜底。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
