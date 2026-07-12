"""A2 静态核对：coordinator chat/ask/continue 路径 stats 落盘契约（task A2）.

核对 c32de07 之后的 stats 透传链不回归——纯静态契约（读源码断言，不依赖后端在线）。

契约链（node_llm_decide → node_chat → _unified_reply → crud.create_message → emit）：

  node_llm_decide (coordinator.py:824-895)
    1. _stream_coordinator_decision 返回七元组 (reply_id, raw, tokens, elapsed_ms,
       model, reasoning_tokens, reasoning_text) —— 全部 7 个槽位都从流式 usage +
       计时填实。
    2. action in (chat, ask, continue) 时把 _stream_stats = {reply_id, elapsed_ms,
       tokens, model, reasoning_tokens, reasoning} 盖到 decision；dispatch 不盖。
    3. result["_stream_stats"] 透传到图状态（dispatch 时为 None，非 dispatch 时为 dict）。

  route_after_llm_decide (coordinator.py:1106)
    4. chat→"chat"；dispatch→"dispatch"；ask/continue 落到 "chat"（同一回复路径，
       node_chat 复用）。故 ask/continue 也经 node_chat 落盘，stats 不丢。

  node_chat (coordinator.py:898-912)
    5. _unified_reply(data=state.get("_stream_stats")) —— 把 stats 盖到 agent_reply.data。

  _unified_reply (coordinator.py:130-160)
    6. crud.create_message({"data": data, ...}) 持久化 data 到 MessageEntity.data（JSON 列）。
    7. emit_message_added(msg.model_dump()) —— emit 的事件也带 data（前端 WS 抓到）。

  持久化 (store/crud.py + store/entities.py)
    8. create_message 透传 data 到 MessageEntity.data；MessageEntity.data 是 JSON 列
       （mapped_column(JSON, nullable=True)），stats dict 原样落盘。

  c32de07 不回归（src/hooks/useBusEvent.ts:283-290）
    9. logs 排除仍含 coordinator_token / task_token / coordinator_reasoning /
       coordinator_stats 四类逐字 delta（c32de07 加的后两个不回归）。

  前端消费 (src/components/ChatPanel.tsx:extractCoordStats)
   10. extractCoordStats 从 data 取 {elapsed_ms, tokens, model, reasoning_tokens}；
       elapsed_ms 非有限/<=0 返 null 不渲染状态行。前端契约与后端落盘字段一一对齐。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD = REPO / "backend" / "engine" / "coordinator.py"
REPLY = REPO / "backend" / "engine" / "reply.py"
BUS = REPO / "backend" / "events" / "bus.py"
CRUD = REPO / "backend" / "store" / "crud.py"
ENTITIES = REPO / "backend" / "store" / "entities.py"
HOOK = REPO / "src" / "hooks" / "useBusEvent.ts"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"

# node_llm_decide 盖到 _stream_stats 的 6 个字段（七元组里 reasoning_text 落到
# data["reasoning"]，其余 5 个槽位 + reasoning 共 6 个 key）。
STAT_KEYS = {"reply_id", "elapsed_ms", "tokens", "model", "reasoning_tokens", "reasoning"}
# action 白名单：chat/ask/continue 三类都走 node_chat 落盘，dispatch 不盖 stats。
STAT_ACTIONS = ("chat", "ask", "continue")


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD.read_text(encoding="utf-8")

    # ── [1] _stream_coordinator_decision 返回七元组 ──
    # 函数签名返回 reply_id, raw_full, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text
    m_sig = re.search(
        r"async def _stream_coordinator_decision\([^)]*\)\s*->\s*([^\n:]+)",
        coord,
    )
    if not m_sig:
        errs.append("[1] 未找到 _stream_coordinator_decision 签名")
    else:
        ret = m_sig.group(1)
        # tuple[str, str, int, int, str, int] 是旧六元组（缺 reasoning_text）；七元组应是
        # tuple[str, str, int, int, str, int, str]。但返回类型注解可能省略或写成 tuple。
        # 改查 return 语句实际返回的标识符数。
        m_ret = re.search(r"return\s+reply_id,\s*raw_full,\s*real_tokens,\s*elapsed_ms,\s*model,\s*real_reasoning_tokens,\s*reasoning_text", coord)
        if not m_ret:
            errs.append(
                "[1] _stream_coordinator_decision return 未返回七元组 "
                "(reply_id, raw_full, real_tokens, elapsed_ms, model, real_reasoning_tokens, reasoning_text)"
            )
        else:
            print("[1] OK  _stream_coordinator_decision 返回七元组（含 reasoning_text）")

    # ── [2] node_llm_decide 解包七元组 ──
    # coordinator.py:824 reply_id, raw, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text
    m_unpack = re.search(
        r"reply_id,\s*raw,\s*tokens,\s*elapsed_ms,\s*model,\s*reasoning_tokens,\s*reasoning_text\s*=\s*await\s+_stream_coordinator_decision",
        coord,
    )
    if not m_unpack:
        errs.append("[2] node_llm_decide 未解包七元组（缺 reasoning_text 槽位）")
    else:
        print("[2] OK  node_llm_decide 解包七元组（含 reasoning_text）")

    # ── [3] action in (chat, ask, continue) 时盖 _stream_stats，含 6 个 key ──
    # 抽 _stream_stats 赋值块
    m_stats = re.search(
        r'if\s+decision\["action"\]\s+in\s+\("chat",\s*"ask",\s*"continue"\)\s*:\s*'
        r'decision\["_stream_stats"\]\s*=\s*\{(.*?)\}',
        coord,
        re.S,
    )
    if not m_stats:
        errs.append("[3] 未找到 decision['_stream_stats'] 赋值块（chat/ask/continue 条件）")
    else:
        block = m_stats.group(1)
        # 抽所有 key（"key": value 形式）
        keys = set(re.findall(r'"(\w+)"\s*:', block))
        missing = STAT_KEYS - keys
        if missing:
            errs.append(f"[3] _stream_stats 缺字段 {missing}（应含 {STAT_KEYS}）")
        else:
            print(f"[3] OK  action in (chat,ask,continue) 盖 _stream_stats 含全部 6 key：{sorted(keys)}")
        # 确认 ask/continue 也在白名单（不只 chat）
        if '"ask"' not in m_stats.group(0) or '"continue"' not in m_stats.group(0):
            errs.append("[3] _stream_stats 白名单缺 ask/continue（只盖 chat 会让 ask/continue 丢 stats）")

    # ── [4] result["_stream_stats"] 透传到图状态（dispatch 时为 None）──
    if '"_stream_stats": decision.get("_stream_stats")' not in coord:
        errs.append('[4] result 未透传 _stream_stats（decision.get("_stream_stats")）')
    else:
        print('[4] OK  result["_stream_stats"] = decision.get("_stream_stats") 透传到图状态')

    # ── [5] route_after_llm_decide: ask/continue 落到 "chat" ──
    m_route = re.search(
        r"def route_after_llm_decide\(state[^)]*\)\s*->\s*str:\s*(.*?)(?=\ndef )",
        coord,
        re.S,
    )
    if not m_route:
        errs.append("[5] 未找到 route_after_llm_decide")
    else:
        body = m_route.group(1)
        # chat → "chat"; dispatch → "dispatch"; 否则 return "chat"（ask/continue 落到 chat）
        if 'action == "chat"' not in body or 'return "chat"' not in body:
            errs.append("[5] route_after_llm_decide 缺 chat 分支或默认 return 'chat'")
        elif 'action == "dispatch"' not in body:
            errs.append("[5] route_after_llm_decide 缺 dispatch 分支")
        else:
            # 确认 ask/continue 不是显式分支（它们落到默认 return "chat"）
            has_ask_branch = bool(re.search(r'action\s*==\s*"ask"', body))
            has_continue_branch = bool(re.search(r'action\s*==\s*"continue"', body))
            if has_ask_branch or has_continue_branch:
                # 有显式分支也行，只要它们 return "chat"。检查是否有 return "ask"/"continue"
                # 的节点名（图里没 ask/continue 节点，会 KeyError）。
                bad = re.search(r'return\s+"(ask|continue)"', body)
                if bad:
                    errs.append(f"[5] route_after_llm_decide 有 return {bad.group(1)!r}（图无此节点，会 KeyError）")
                else:
                    print("[5] OK  route_after_llm_decide: chat→chat, dispatch→dispatch, ask/continue→chat（同回复路径）")
            else:
                print("[5] OK  route_after_llm_decide: chat→chat, dispatch→dispatch, 默认→chat（ask/continue 落到 chat）")

    # ── [6] node_chat 用 _unified_reply(data=state.get("_stream_stats")) ──
    m_chat = re.search(
        r"async def node_chat\(state[^)]*\)[^}]*?await _unified_reply\(\s*state\[\"group_id\"\],\s*state\[\"agent_id\"\],\s*state\.get\(\"reply_content\",\s*\"\"\),\s*data=state\.get\(\"_stream_stats\"\),\s*\)",
        coord,
        re.S,
    )
    if not m_chat:
        errs.append('[6] node_chat 未用 _unified_reply(data=state.get("_stream_stats"))')
    else:
        print('[6] OK  node_chat → _unified_reply(data=state.get("_stream_stats"))')

    # ── [7] _unified_reply 持久化 data + emit 带 data ──
    # B10 抽 persist_agent_reply 到 engine/reply.py 后，_unified_reply 改调它（单一真源），
    # 不再内联 crud.create_message + emit_message_added。改为锁「_unified_reply 调
    # persist_agent_reply 透传 data」+「persist_agent_reply 内部透传 data + emit」（行为等价）。
    m_reply_fn = re.search(
        r"async def _unified_reply\([^)]*\)(.*?)(?=\nasync def |\ndef )",
        coord,
        re.S,
    )
    reply_body = m_reply_fn.group(1) if m_reply_fn else ""
    if not reply_body:
        errs.append("[7] _unified_reply 函数体未找到")
    else:
        # _unified_reply 调 persist_agent_reply 透传 data
        if "persist_agent_reply" not in reply_body:
            errs.append("[7] _unified_reply 未调 persist_agent_reply（B10 单一真源未接线）")
        else:
            print("[7] OK  _unified_reply → persist_agent_reply（B10 单一真源）")
        # persist_agent_reply 内部透传 data + emit（engine/reply.py 锁）
        reply_mod = REPLY.read_text(encoding="utf-8")
        if '"data": data' not in reply_mod:
            errs.append('[7] persist_agent_reply 未透传 "data": data 到 crud.create_message')
        else:
            print('[7] OK  persist_agent_reply → crud.create_message({"data": data}) 持久化 data')
        if "emit_message_added(msg.model_dump())" not in reply_mod:
            errs.append("[7] persist_agent_reply 未 emit_message_added(msg.model_dump())")
        else:
            print("[7] OK  persist_agent_reply → emit_message_added(msg.model_dump())（emit 事件带 data）")

    # ── [8] create_message 透传 data 到 MessageEntity.data（JSON 列）──
    crud = CRUD.read_text(encoding="utf-8")
    m_cm_fn = re.search(
        r"async def create_message\(payload[^)]*\)(.*?)(?=\nasync def |\ndef )",
        crud,
        re.S,
    )
    cm_body = m_cm_fn.group(1) if m_cm_fn else ""
    if not cm_body:
        errs.append("[8] create_message 函数体未找到")
    elif 'data=data.get("data")' not in cm_body:
        errs.append('[8] MessageEntity 未透传 data=data.get("data")')
    else:
        print('[8] OK  create_message → MessageEntity(data=data.get("data")) 透传 data')

    # MessageEntity.data 是 JSON 列
    ent = ENTITIES.read_text(encoding="utf-8")
    m_col = re.search(
        r'class MessageEntity\(Base\).*?data:\s*Mapped\[.*?\]\s*=\s*mapped_column\(([^)]*)\)',
        ent,
        re.S,
    )
    if not m_col:
        errs.append("[8] MessageEntity.data 列定义未找到")
    elif "JSON" not in m_col.group(1):
        errs.append(f"[8] MessageEntity.data 非 JSON 列（{m_col.group(1).strip()!r}），stats dict 无法落盘")
    else:
        print("[8] OK  MessageEntity.data = mapped_column(JSON) —— stats dict 原样落盘")

    # ── [9] c32de07 不回归：logs 排除仍含四类逐字 delta ──
    hook = HOOK.read_text(encoding="utf-8")
    # 抽 logs 过滤 if 块
    m_logs = re.search(
        r"if\s*\(\s*d\.content\s*&&(.*?)\)\s*\{",
        hook,
        re.S,
    )
    if not m_logs:
        errs.append("[9] useBusEvent logs 过滤 if 块未找到（c32de07 结构可能已变）")
    else:
        cond = m_logs.group(1)
        required_exclude = {
            "coordinator_token": "coordinator_token" in cond,
            "task_token": "task_token" in cond,
            "coordinator_reasoning": "coordinator_reasoning" in cond,
            "coordinator_stats": "coordinator_stats" in cond,
        }
        missing = [k for k, v in required_exclude.items() if not v]
        if missing:
            errs.append(f"[9] logs 排除缺 {missing}（c32de07 的 reasoning/stats 排除回归）")
        else:
            print(f"[9] OK  logs 排除四类逐字 delta（{sorted(required_exclude)}）—— c32de07 不回归")

    # ── [10] 前端 extractCoordStats 与后端落盘字段对齐 ──
    panel = PANEL.read_text(encoding="utf-8")
    m_extract = re.search(
        r"function extractCoordStats\([^)]*\)[^{]*\{(.*?)\n\}",
        panel,
        re.S,
    )
    if not m_extract:
        errs.append("[10] extractCoordStats 未找到")
    else:
        body = m_extract.group(1)
        # 前端读的字段必须是后端落盘的子集
        fe_reads = set(re.findall(r'data\.(\w+)', body))
        # 后端落盘 6 key，前端应读 elapsed_ms/tokens/model/reasoning_tokens（reply_id/reasoning
        # 由 finalizedBubbles 退场判定/extractCoordReasoning 另读，不在 extractCoordStats）
        expected_fe = {"elapsed_ms", "tokens", "model", "reasoning_tokens"}
        missing_fe = expected_fe - fe_reads
        if missing_fe:
            errs.append(f"[10] extractCoordStats 未读 {missing_fe}（与后端落盘字段不对齐）")
        else:
            print(f"[10] OK  extractCoordStats 读 {sorted(fe_reads & expected_fe)}（与后端 _stream_stats 字段对齐）")
        # elapsed_ms 非有限/<=0 返 null（不渲染假状态行）
        if "Number.isFinite(elapsed)" not in body or "elapsed <= 0" not in body:
            errs.append("[10] extractCoordStats 缺 elapsed_ms 非有限/<=0 → null 守卫")

    return errs


def main() -> int:
    print("=== A2 静态核对：coordinator chat/ask/continue 路径 stats 落盘契约 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "coordinator chat/ask/continue 路径 stats 落盘链完整：\n"
        "  · _stream_coordinator_decision 返回七元组（含 reasoning_text）；\n"
        "  · node_llm_decide 对 chat/ask/continue 盖 _stream_stats（6 key 全）+ result 透传；\n"
        "  · route_after_llm_decide: ask/continue 落到 chat（同一回复路径，stats 不丢）；\n"
        "  · node_chat → _unified_reply(data=_stream_stats) → crud.create_message 落盘 + emit 带 data；\n"
        "  · MessageEntity.data 是 JSON 列，stats dict 原样落盘；\n"
        "  · c32de07 logs 排除四类逐字 delta 不回归；\n"
        "  · 前端 extractCoordStats 字段与后端落盘对齐 + elapsed_ms 守卫。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
