"""A3 静态核对：worker 单聊 chat 路径 stats 落盘契约（task A3）.

确认单聊 worker brain chat 路径 stats 与协调者同形落盘——前端 extractCoordStats
不区分来源（worker agent_reply 与 coordinator agent_reply 都按 data.elapsed_ms 是否
存在判定渲染状态行），故 worker 必须盖同形 _stream_stats，否则单聊回复无状态行
（只有协调者群聊有，前后端不一致）。

契约链（node_brain_decide → node_chat → _unified_reply → crud.create_message → emit）：

  node_brain_decide (worker.py:143-281)
    1. reply_id = uuid4().hex 生成（单聊回复流式 token 归并键，与协调者 _stream_coordinator_decision 同构）。
    2. chat_completion_stream 流式采集 usage（completion_tokens / reasoning_tokens）+ 耗时 + model +
       reasoning_content 全文（与协调者 _stream_coordinator_decision 同款 async for 四元组循环）。
    3. stats dict 盖 {reply_id, elapsed_ms, tokens, model, reasoning_tokens} + 条件 reasoning（非空才塞）。
       —— 与协调者 _stream_stats 6 key 对齐（协调者必塞 reasoning，worker 非空才塞——口径差异但
       前端 extractCoordReasoning 对 data.reasoning 缺失返 undefined，兼容）。
    4. usage 未到退粗估 len//3（与协调者 live_tokens 启发式一致），保证状态行总有数。
    5. LLM 异常 → stats = None（chat 兜底回复无 stats，不渲染假状态行）。
    6. return {"decision": decision, "_stream_stats": stats} —— stats 透传到图状态。

  route_brain (worker.py:329-330)
    7. decision.action 默认 chat（chat/execute/ask 三态），路由到对应节点。
       chat/ask 都用 node_chat/node_ask 落盘 _stream_stats，execute 不带 stats（模板 announce）。

  node_chat (worker.py:284-291)
    8. _unified_reply(data=state.get("_stream_stats")) —— stats 盖到 agent_reply.data。

  node_ask (worker.py:319-326)
    9. 同 node_chat，ask 路径也带 stats（澄清提问也消耗了 token，状态行不丢）。

  node_execute (worker.py:294-316)
   10. _unified_reply 不传 data（模板「收到，我来...」announce 非流式文本，不带 stats）——
       与协调者 dispatch announce 排除同理。

  _unified_reply (worker.py:~333)
   11. crud.create_message({"data": data}) 持久化 data + emit_message_added(msg.model_dump())
       （emit 事件带 data）。与协调者 _unified_reply 同一落盘路径（store.crud.create_message +
       events.bus.emit_message_added 单一真源，前后端不区分来源）。

  与协调者一致性 (前端 ChatPanel.tsx extractCoordStats)
   12. extractCoordStats 不区分 sender_id 来源——只要 data.elapsed_ms 非有限/>0 返 null，
       否则返 {elapsed_ms, tokens, model?, reasoning_tokens?}。worker agent_reply 与
       coordinator agent_reply 经同一段代码渲染状态行（同形 stats → 同款状态行）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKER = REPO / "backend" / "engine" / "worker.py"
COORD = REPO / "backend" / "engine" / "coordinator.py"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"

# worker stats 必塞的 5 key（reasoning 条件塞，非必塞——与协调者口径差异）。
WORKER_STAT_KEYS = {"reply_id", "elapsed_ms", "tokens", "model", "reasoning_tokens"}
# 协调者 stats 6 key（reasoning 必塞）。
COORD_STAT_KEYS = {"reply_id", "elapsed_ms", "tokens", "model", "reasoning_tokens", "reasoning"}


def _fn_body(src: str, fname: str) -> str:
    """抽 async def fname(...) 到下一个顶层 def/async def 的函数体。"""
    m = re.search(
        rf"async def {fname}\([^)]*\)(.*?)(?=\nasync def |\ndef )",
        src,
        re.S,
    )
    return m.group(1) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    worker = WORKER.read_text(encoding="utf-8")

    # ── [1] reply_id = uuid4().hex 生成 ──
    if "reply_id = uuid.uuid4().hex" not in worker:
        errs.append("[1] worker.py 未生成 reply_id = uuid.uuid4().hex（与协调者同构键缺失）")
    else:
        print("[1] OK  reply_id = uuid.uuid4().hex 生成（与协调者 _stream_coordinator_decision 同构）")

    # ── [2] chat_completion_stream 流式采集四元组（usage + reasoning_usage）──
    # worker.py 实际写法：async for content_delta, reasoning_delta, usage, reasoning_usage in chat_completion_stream(...)
    m_stream = re.search(
        r"async for\s+content_delta,\s*reasoning_delta,\s*usage,\s*reasoning_usage\s+in\s+chat_completion_stream",
        worker,
    )
    if not m_stream:
        errs.append("[2] worker.py 未用 chat_completion_stream 流式四元组（与协调者同款）")
    else:
        print("[2] OK  chat_completion_stream 流式四元组（content/reasoning/usage/reasoning_usage）—— 与协调者同款")

    # ── [3] stats dict 盖 5 必塞 key + 条件 reasoning ──
    # 抽 stats = {...} 赋值块
    m_stats = re.search(
        r'stats:\s*dict\[str,\s*Any\]\s*=\s*\{(.*?)\}(?:\s*if\s+reasoning_text:)',
        worker,
        re.S,
    )
    if not m_stats:
        errs.append("[3] worker.py 未找到 stats: dict[str, Any] = {...} 赋值块")
    else:
        block = m_stats.group(1)
        keys = set(re.findall(r'"(\w+)"\s*:', block))
        missing = WORKER_STAT_KEYS - keys
        if missing:
            errs.append(f"[3] stats 缺必塞字段 {missing}（应含 {WORKER_STAT_KEYS}）")
        else:
            print(f"[3] OK  stats 盖 5 必塞 key：{sorted(keys)}")
        # 条件 reasoning：if reasoning_text: stats["reasoning"] = reasoning_text
        m_cond = re.search(r'if\s+reasoning_text:\s*\n\s*stats\["reasoning"\]\s*=\s*reasoning_text', worker)
        if not m_cond:
            errs.append('[3] 未找到 if reasoning_text: stats["reasoning"] = reasoning_text（条件塞 reasoning）')
        else:
            print("[3] OK  reasoning 条件塞（非空才塞，与协调者必塞口径差异——前端 extractCoordReasoning 兼容 undefined）")

    # ── [4] usage 未到退粗估 len//3（与协调者 live_tokens 一致）──
    m_fallback = re.search(r"tokens\s*=\s*final_tokens\s+if\s+final_tokens\s+else\s+max\(1,\s*len\(raw\)\s*//\s*3\)", worker)
    if not m_stats:
        errs.append("[3] stats 块未找到（[4] 依赖同位置）")
    elif not m_fallback:
        errs.append("[4] 未找到 tokens 粗估兜底 final_tokens if final_tokens else max(1, len(raw)//3)")
    else:
        print("[4] OK  usage 未到退粗估 len//3（与协调者 live_tokens 启发式一致）")

    # ── [5] LLM 异常 → stats = None（不渲染假状态行）──
    # 抽 except Exception 块（到下一个非缩进 return）。校验块内含 decision 兜底 + stats = None。
    m_exc = re.search(
        r"except Exception as e:.*?(?=\n    return |\n    [a-z])",
        worker,
        re.S,
    )
    if not m_exc:
        errs.append("[5] 未找到 node_brain_decide 的 except Exception 块")
    else:
        exc_block = m_exc.group(0)
        has_warn = "brain decision failed" in exc_block
        has_chat_fallback = re.search(r'"action":\s*"chat"', exc_block) is not None
        has_stats_none = re.search(r"stats\s*=\s*None", exc_block) is not None
        if not (has_warn and has_chat_fallback and has_stats_none):
            errs.append(
                f"[5] except Exception 块缺要素：warn={has_warn} chat兜底={has_chat_fallback} stats=None={has_stats_none}"
            )
        else:
            print("[5] OK  LLM 异常 → stats = None（chat 兜底回复无 stats，不渲染假状态行）")

    # ── [6] return {"decision": decision, "_stream_stats": stats} ──
    if 'return {"decision": decision, "_stream_stats": stats}' not in worker:
        errs.append('[6] node_brain_decide 未 return {"decision": decision, "_stream_stats": stats}')
    else:
        print('[6] OK  return {"decision": decision, "_stream_stats": stats} —— stats 透传图状态')

    # ── [7] route_brain: decision.action 默认 chat ──
    m_route = re.search(
        r"def route_brain\(state[^)]*\)\s*->\s*str:\s*return\s+state\.get\(\"decision\",\s*\{\}\)\.get\(\"action\",\s*\"chat\"\)",
        worker,
    )
    if not m_route:
        errs.append('[7] route_bride 未 return state.get("decision",{}).get("action","chat")')
    else:
        print('[7] OK  route_brain: decision.action 默认 chat（chat/execute/ask 三态路由）')

    # ── [8] node_chat → _unified_reply(data=state.get("_stream_stats")) ──
    chat_body = _fn_body(worker, "node_chat")
    if not chat_body:
        errs.append("[8] node_chat 函数体未找到")
    elif 'data=state.get("_stream_stats")' not in chat_body:
        errs.append('[8] node_chat 未传 data=state.get("_stream_stats")')
    else:
        print('[8] OK  node_chat → _unified_reply(data=state.get("_stream_stats"))')

    # ── [9] node_ask 也带 stats（澄清提问不丢状态行）──
    ask_body = _fn_body(worker, "node_ask")
    if not ask_body:
        errs.append("[9] node_ask 函数体未找到")
    elif 'data=state.get("_stream_stats")' not in ask_body:
        errs.append('[9] node_ask 未传 data=state.get("_stream_stats")（澄清提问会丢状态行）')
    else:
        print('[9] OK  node_ask → _unified_reply(data=state.get("_stream_stats"))（ask 也带 stats）')

    # ── [10] node_execute 不带 stats（模板 announce）──
    exe_body = _fn_body(worker, "node_execute")
    if not exe_body:
        errs.append("[10] node_execute 函数体未找到")
    elif 'data=state.get("_stream_stats")' in exe_body:
        errs.append("[10] node_execute 传了 stats（模板 announce 不该带——与协调者 dispatch 排除同理）")
    else:
        print("[10] OK  node_execute → _unified_reply 不传 data（模板「收到，我来...」announce 无 stats）")

    # ── [11] worker _unified_reply 同款落盘（crud.create_message + emit）──
    reply_body = _fn_body(worker, "_unified_reply")
    if not reply_body:
        errs.append("[11] worker _unified_reply 函数体未找到")
    else:
        if '"data": data' not in reply_body:
            errs.append('[11] worker _unified_reply 未透传 "data": data 到 crud.create_message')
        else:
            print('[11] OK  worker _unified_reply → crud.create_message({"data": data}) 持久化')
        if "emit_message_added(msg.model_dump())" not in reply_body:
            errs.append("[11] worker _unified_reply 未 emit_message_added(msg.model_dump())")
        else:
            print("[11] OK  worker _unified_reply → emit_message_added(msg.model_dump())（emit 带 data）")

    # ── [12] 前端 extractCoordStats 不区分来源（worker/coordinator 同段代码渲染）──
    panel = PANEL.read_text(encoding="utf-8")
    m_extract = re.search(r"function extractCoordStats\([^)]*\)[^{]*\{(.*?)\n\}", panel, re.S)
    if not m_extract:
        errs.append("[12] extractCoordStats 未找到")
    else:
        body = m_extract.group(1)
        # 不按 sender_id 区分协调者/worker：只看 data.elapsed_ms（无 sender_id 条件分支）
        has_sender_branch = bool(re.search(r"sender_id|isCoordinator|graph_kind|sender\s*===", body))
        if has_sender_branch:
            errs.append("[12] extractCoordStats 含 sender_id/来源分支（违反「不区分来源」契约）")
        else:
            print("[12] OK  extractCoordStats 不按 sender_id 区分来源（仅看 data.elapsed_ms）—— worker/coordinator 同段渲染")
        # 与 worker stats 字段对齐：elapsed_ms/tokens/model/reasoning_tokens
        fe_reads = set(re.findall(r"data\.(\w+)", body))
        expected = {"elapsed_ms", "tokens", "model", "reasoning_tokens"}
        missing = expected - fe_reads
        if missing:
            errs.append(f"[12] extractCoordStats 未读 {missing}（与 worker stats 字段不对齐）")
        else:
            print(f"[12] OK  extractCoordStats 读 {sorted(fe_reads & expected)}（与 worker/coordinator stats 字段同形对齐）")

    # ── [13] worker/coordinator stats key 同形对照（reasoning 口径差异说明）──
    coord = COORD.read_text(encoding="utf-8")
    m_coord_stats = re.search(
        r'decision\["_stream_stats"\]\s*=\s*\{(.*?)\}',
        coord,
        re.S,
    )
    if not m_coord_stats:
        errs.append("[13] 协调者 _stream_stats 赋值块未找到（无法对照）")
    else:
        coord_keys = set(re.findall(r'"(\w+)"\s*:', m_coord_stats.group(1)))
        # worker 5 必塞 + 条件 reasoning（最多 6 key），协调者 6 key 全塞。
        # 交集应是 worker 必塞 5 key + reasoning（worker 条件塞时齐全）。
        common = WORKER_STAT_KEYS & coord_keys
        if common != WORKER_STAT_KEYS:
            errs.append(f"[13] worker/coordinator stats 必塞 key 不一致：worker={WORKER_STAT_KEYS} coord={coord_keys} 交集={common}")
        else:
            reasoning_diff = "reasoning" in coord_keys and ("reasoning" not in (keys if 'keys' in dir() else set()))
            print(f"[13] OK  worker（5必塞+条件reasoning）与协调者（6全塞）stats key 同形：必塞交集={sorted(common)}")
            print(f"      口径差异：协调者 reasoning 必塞、worker 非空才塞——前端 extractCoordReasoning 对缺失返 undefined 兼容")

    return errs


def main() -> int:
    print("=== A3 静态核对：worker 单聊 chat 路径 stats 落盘契约（与协调者同形）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "worker 单聊 chat/ask 路径 stats 同形落盘，与协调者一致：\n"
        "  · node_brain_decide 生成 reply_id + chat_completion_stream 流式采集 usage/耗时/model/reasoning；\n"
        "  · stats 盖 5 必塞 key（reply_id/elapsed_ms/tokens/model/reasoning_tokens）+ 条件 reasoning；\n"
        "  · usage 未到退粗估 len//3（与协调者 live_tokens 一致）+ LLM 异常 stats=None（不渲染假状态行）；\n"
        "  · node_chat/node_ask → _unified_reply(data=_stream_stats) 落盘；node_execute 不带 stats（模板 announce）；\n"
        "  · worker _unified_reply 与协调者同款 crud.create_message + emit_message_added（单一真源，不区分来源）；\n"
        "  · 前端 extractCoordStats 不按 sender_id 区分来源—— worker/coordinator agent_reply 同段渲染状态行。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
