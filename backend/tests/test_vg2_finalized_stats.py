"""VG2 回归：定稿气泡 stats 必带 elapsed_ms>0（task A8）.

锁住「定稿气泡 data 必带 elapsed_ms>0」——防用户报告的「作文无模型消耗信息」(2a 症状)回归。
与 test_va2 互补：va2 锁 stats 透传链全貌（七元组/6 key/路由/落盘/JSON 列/c32de07/前端字段），
vg2 聚焦任务 A8 的三个具体点 + elapsed_ms>0 这个「定稿气泡必带」不变量。

三段契约（纯静态，读源码断言）：

  A. node_llm_decide 对 chat/ask/continue 盖 _stream_stats（含 5 必填 key）
    1. action in (chat, ask, continue) 时盖 decision["_stream_stats"]，含
       {reply_id, elapsed_ms, tokens, model, reasoning_tokens}（任务明列的 5 个）。
    2. ask/continue 也在白名单（不只 chat）——澄清/续接回复也带 stats。
    3. dispatch 不盖（announce 是模板非流式文本，stats 不匹配 content）。
    4. LLM 异常 except 路径把 7 变量置零（reply_id="" / tokens=0 / elapsed_ms=0 / model="" /
       reasoning_tokens=0 / reasoning_text=""）——异常兜底回复的 stats 是零值，前端 [C10]
       elapsed_ms<=0 → null 不渲染，不显示假状态行。

  B. node_chat 透传 _unified_reply(data=_stream_stats)
    5. node_chat → _unified_reply(..., data=state.get("_stream_stats"))。
    6. route_after_llm_decide: ask/continue 落到 "chat"（同一回复路径，stats 不丢）。

  C. 定稿气泡 data 必带 elapsed_ms>0（核心不变量）
    7. elapsed_ms 由 time.monotonic() 墙钟计算（int((time.monotonic()-start)*1000)），
       非硬编码 0 —— 保证真实 LLM 调用后 elapsed_ms > 0。
    8. start 在流式循环前赋值（elapsed 测的是循环时长，LLM 必跑 > 0）。
    9. 成功路径不把 elapsed_ms 置 0（except 才置 0，靠 [C10] 前端 <=0 → null 兜底）。
   10. 前端 extractCoordStats: elapsed_ms 非有限/<=0 → null 不渲染；finite & > 0 → 返状态行
       —— 后端保证 elapsed_ms>0 与前端渲染条件对齐（定稿气泡必渲染状态行）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COORD = REPO / "backend" / "engine" / "coordinator.py"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"

# 任务明列的 5 个必填 key（reasoning 是第 6 个，va2 已锁，vg2 聚焦任务明列的 5 个）。
REQUIRED_KEYS = {"reply_id", "elapsed_ms", "tokens", "model", "reasoning_tokens"}
# action 白名单：chat/ask/continue 三类都盖 stats。
STAT_ACTIONS = ("chat", "ask", "continue")


def _fn_body(src: str, fname: str, indent_opts=("    ", "")) -> str:
    """抽 fn 函数体到下一个同级 def（试多种缩进：类方法 4 空格 / 模块级 0 缩进）。"""
    for indent in indent_opts:
        m = re.search(
            rf"(?:async def|def) {fname}\([^)]*\)(.*?)(?=\n{indent}(?:async )?def )",
            src,
            re.S,
        )
        if m:
            return m.group(1)
    return ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    coord = COORD.read_text(encoding="utf-8")

    # ── A. node_llm_decide 盖 _stream_stats（5 必填 key）──
    # [1] action in (chat, ask, continue) 盖 _stream_stats 含 5 key
    m_stats = re.search(
        r'if\s+decision\["action"\]\s+in\s+\("chat",\s*"ask",\s*"continue"\)\s*:\s*'
        r'decision\["_stream_stats"\]\s*=\s*\{(.*?)\}',
        coord,
        re.S,
    )
    if not m_stats:
        errs.append("[A1] 未找到 decision['_stream_stats'] 赋值块（chat/ask/continue 条件）")
    else:
        block = m_stats.group(1)
        keys = set(re.findall(r'"(\w+)"\s*:', block))
        missing = REQUIRED_KEYS - keys
        if missing:
            errs.append(f"[A1] _stream_stats 缺任务明列的 5 key 中的 {missing}（应含 {REQUIRED_KEYS}）")
        else:
            print(f"[A1] OK  action in (chat,ask,continue) 盖 _stream_stats 含 5 必填 key：{sorted(keys & REQUIRED_KEYS)}")

    # [2] ask/continue 也在白名单（不只 chat）
    if m_stats:
        if '"ask"' not in m_stats.group(0) or '"continue"' not in m_stats.group(0):
            errs.append("[A2] _stream_stats 白名单缺 ask/continue（澄清/续接回复会丢 stats）")
        else:
            print("[A2] OK  ask/continue 也在白名单（澄清/续接回复带 stats）")

    # [3] dispatch 不盖 _stream_stats（白名单不含 dispatch）
    if m_stats and '"dispatch"' in m_stats.group(0):
        errs.append("[A3] _stream_stats 白名单误含 dispatch（announce 不该带 stats）")
    else:
        print("[A3] OK  dispatch 不在 _stream_stats 白名单（announce 非流式文本不带 stats）")

    # [4] LLM 异常 except 路径把 7 变量置零（reply_id="" / tokens=0 / elapsed_ms=0 /
    # model="" / reasoning_tokens=0 / reasoning_text=""）。stats 盖在主流程（try/except 之后），
    # 对成功 + 异常两路径都生效——异常路径 decision["action"]="chat" 也会盖 _stream_stats，
    # 但值是零（elapsed_ms=0）。安全网在 [C10]：前端 elapsed_ms<=0 → null 不渲染，
    # 故异常路径不显示假状态行。本项核 except 路径确实置零（非留旧值/非造假正数）。
    m_except = re.search(
        r'except Exception as e:\s*\n\s*logger\.warning\("\[coordinator\] LLM decision failed[^"]*"[^)]*\)'
        r'(.*?)(?=\n    # Stamp the streaming run-stats)',
        coord,
        re.S,
    )
    if not m_except:
        errs.append("[A4] node_llm_decide 的 except Exception 块未找到（无法核异常路径置零）")
    else:
        exc_block = m_except.group(1)
        # except 块应含 7 变量置零赋值
        m_zero = re.search(
            r'reply_id,\s*tokens,\s*elapsed_ms,\s*model,\s*reasoning_tokens,\s*reasoning_text\s*=\s*"",\s*0,\s*0,\s*"",\s*0,\s*""',
            exc_block,
        )
        if not m_zero:
            errs.append("[A4] except 路径未把 7 变量置零（异常路径可能留旧值/造假正数 stats）")
        else:
            print("[A4] OK  LLM 异常 except 路径把 7 变量置零（elapsed_ms=0 → 前端不渲染假状态行）")

    # ── B. node_chat 透传 ──
    # [5] node_chat → _unified_reply(data=state.get("_stream_stats"))
    chat_body = _fn_body(coord, "node_chat", indent_opts=("",))
    if not chat_body:
        errs.append("[B5] coordinator node_chat 函数体未找到")
    elif 'data=state.get("_stream_stats")' not in chat_body:
        errs.append('[B5] node_chat 未透传 data=state.get("_stream_stats")')
    else:
        print('[B5] OK  node_chat → _unified_reply(data=state.get("_stream_stats")) 透传 stats')

    # [6] route_after_llm_decide: ask/continue 落到 "chat"
    m_route = re.search(
        r"def route_after_llm_decide\(state[^)]*\)\s*->\s*str:\s*(.*?)(?=\ndef )",
        coord,
        re.S,
    )
    if not m_route:
        errs.append("[B6] route_after_llm_decide 未找到")
    else:
        body = m_route.group(1)
        # chat→chat, dispatch→dispatch, 默认→chat（ask/continue 落到 chat）
        if 'action == "chat"' not in body or 'return "chat"' not in body:
            errs.append("[B6] route_after_llm_decide 缺 chat 分支或默认 return 'chat'")
        elif 'action == "dispatch"' not in body:
            errs.append("[B6] route_after_llm_decide 缺 dispatch 分支")
        else:
            # 不能 return "ask"/"continue"（图无此节点）
            bad = re.search(r'return\s+"(ask|continue)"', body)
            if bad:
                errs.append(f"[B6] route_after_llm_decide 有 return {bad.group(1)!r}（图无此节点）")
            else:
                print("[B6] OK  route_after_llm_decide: ask/continue 落到 chat（同回复路径 stats 不丢）")

    # ── C. 定稿气泡 data 必带 elapsed_ms>0（核心不变量）──
    # [7] elapsed_ms 由 time.monotonic() 墙钟计算（非硬编码 0）
    # _stream_coordinator_decision 里：elapsed_ms = int((time.monotonic() - start) * 1000)
    m_elapsed = re.search(
        r"elapsed_ms\s*=\s*int\(\s*\(time\.monotonic\(\)\s*-\s*start\)\s*\*\s*1000\s*\)",
        coord,
    )
    if not m_elapsed:
        errs.append("[C7] elapsed_ms 未由 time.monotonic() 墙钟计算（可能硬编码/缺计算 → 不保证 >0）")
    else:
        print("[C7] OK  elapsed_ms = int((time.monotonic()-start)*1000)（墙钟计算，非硬编码 0）")

    # [8] start 在流式循环前赋值（elapsed 测循环时长，LLM 必跑 > 0）
    # 抽 _stream_coordinator_decision 函数体，确认 start 在 `async for ... in chat_completion_stream` 前
    m_fn = re.search(
        r"async def _stream_coordinator_decision\([^)]*\)(.*?)(?=\nasync def |\ndef )",
        coord,
        re.S,
    )
    if not m_fn:
        errs.append("[C8] _stream_coordinator_decision 函数体未找到")
    else:
        fn_body = m_fn.group(1)
        idx_start = fn_body.find("start = time.monotonic()")
        idx_loop = fn_body.find("async for content_delta, reasoning_delta, usage, reasoning_usage")
        if idx_start < 0 or idx_loop < 0:
            errs.append("[C8] _stream_coordinator_decision 未找到 start 赋值或流式循环（结构已变）")
        elif idx_start >= idx_loop:
            errs.append("[C8] start 赋值在流式循环之后（elapsed 不含循环时长，可能 =0）")
        else:
            print("[C8] OK  start 在流式循环前赋值（elapsed 测循环时长，LLM 调用必 >0）")

    # [9] 成功路径不把 elapsed_ms 置 0（except 才置 0）。两路径都经主流程盖 _stream_stats，
    # 但 except 路径 7 变量已置零（[A4]），故盖的 stats 是 elapsed_ms=0；前端 [C10] 不渲染。
    # 成功路径 elapsed_ms 来自 _stream_coordinator_decision 的墙钟值（[C7]/[C8] 保证 >0）。
    if m_except:
        # 核 except 路径 elapsed_ms 确实置 0（与成功路径墙钟值 >0 互斥）
        has_zero_assign = re.search(
            r'reply_id,\s*tokens,\s*elapsed_ms,\s*model,\s*reasoning_tokens,\s*reasoning_text\s*=\s*"",\s*0,\s*0,\s*"",\s*0,\s*""',
            coord,
        )
        if not has_zero_assign:
            errs.append("[C9] except 路径未把 elapsed_ms 置 0（异常兜底 stats 口径不一致）")
        else:
            print("[C9] OK  except 路径置 0（异常不显示假状态行，靠 [C10] elapsed_ms<=0 → null）+ 成功路径墙钟 >0")
    else:
        errs.append("[C9] 依赖 [A4] except 块，[A4] 已 FAIL")

    # [10] 前端 extractCoordStats: elapsed_ms finite & > 0 → 渲染状态行（与后端保证对齐）
    panel = PANEL.read_text(encoding="utf-8")
    m_extract = re.search(r"function extractCoordStats\([^)]*\)[^{]*\{(.*?)\n\}", panel, re.S)
    if not m_extract:
        errs.append("[C10] extractCoordStats 未找到")
    else:
        body = m_extract.group(1)
        # elapsed 非有限 → null；elapsed <= 0 → null（不渲染假状态行）
        if "Number.isFinite(elapsed)" not in body:
            errs.append("[C10] extractCoordStats 缺 Number.isFinite(elapsed) 守卫")
        elif "elapsed <= 0" not in body:
            errs.append("[C10] extractCoordStats 缺 elapsed <= 0 → null 守卫（会渲染 0 耗时假状态行）")
        else:
            # 确认 finite & > 0 时返状态行（return { elapsed_ms: elapsed, ... }）
            has_return_stats = re.search(r"return\s*\{\s*elapsed_ms:\s*elapsed", body) is not None
            if not has_return_stats:
                errs.append("[C10] extractCoordStats finite&>0 时未返 {elapsed_ms:elapsed,...}（不渲染状态行）")
            else:
                print("[C10] OK  extractCoordStats: elapsed_ms finite&>0 → 返状态行（与后端 elapsed_ms>0 保证对齐）")

    return errs


def main() -> int:
    print("=== VG2 回归：定稿气泡 stats 必带 elapsed_ms>0 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "VG2 回归契约锁定（定稿气泡 data 必带 elapsed_ms>0）：\n"
        "  · A node_llm_decide 对 chat/ask/continue 盖 _stream_stats 含 5 必填 key"
        "（reply_id/elapsed_ms/tokens/model/reasoning_tokens），dispatch 不盖，"
        "LLM 异常 except 路径置零（靠前端 elapsed_ms<=0 → null 兜底，不显示假状态行）；\n"
        "  · B node_chat → _unified_reply(data=_stream_stats) 透传 + ask/continue 落到 chat（stats 不丢）；\n"
        "  · C elapsed_ms 由 time.monotonic() 墙钟计算 + start 在循环前赋值（成功路径必 >0）"
        "+ except 路径置 0 靠前端 elapsed_ms<=0 → null 兜底 + 前端 finite&>0 才渲染——"
        "后端保证 elapsed_ms>0 与前端渲染条件对齐，定稿气泡必带模型消耗状态行。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
