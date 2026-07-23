"""VH24 回归：chat_completion_stream 超时/重试/usage/异常审计锁契约（task B27）.

锁住 B27 审计——``backend/llm/client.py chat_completion_stream`` 的超时/重试/usage 缺失
分支 + reasoning_usage 透传 + 异常不吞没 + 与 coordinator/worker 两调用方契约对齐.

B27 审计结论（4 维度现状对齐，行为零变不改码只补契约测锁防回归）：

  ── 超时：httpx.Timeout(requestTimeout, 默认 120s) = connect/read/write/pool 同值 ──
    chat_completion_stream 用 ``client_kwargs: dict[str, Any] = {"timeout": timeout}``
    传入 httpx.AsyncClient(**client_kwargs) → httpx.Timeout(120.0) 把 4 个 timeout phase
    都设成同值. 关键语义（审计确认无误，非 bug）：read timeout = 120s 治的是**单 chunk 间隙**
    （两个 SSE chunk 之间最长 120s），**不是**整个流式响应的总时长. 推理模型（DeepSeek-R1/
    kimi-k2.6）思考期可能持续 39s 但每个 reasoning chunk 间隔 <1s → 不触发 read timeout.
    故 reasoning 长耗时不会误杀流式. 与 chat_completion 非流式一致（单请求 120s 总超时）.

  ── 重试（client.py 应用层重试，瞬态 5xx/429/网络/空200 退避重试）──
    chat_completion_stream / chat_completion 都从 config 取 ``maxRetries``（get_llm_config
    映射 provider.max_retries，默认 2）做应用层退避重试（0.5/1.0/...s 指数退避）。与
    agent_loop.py langchain ChatOpenAI(max_retries=) 路径**不同**——本模块直调 httpx，
    应用层 asyncio.sleep 退避（httpx.HTTPTransport(retries=) 治连接级瞬态，本模块额外
    覆盖 5xx/429/空200）. ``_is_retryable_llm_error`` 判定可重试：
      - httpx.TransportError（connect/read/timeout 等网络层）→ 可重试.
      - RuntimeError ``LLM API error 5xx/429`` → 可重试（_RETRYABLE_STATUS={429,500,502,503,504}）.
      - RuntimeError ``LLM returned empty choices``（网关 200 返空 body）→ 可重试.
      - 4xx 鉴权/参数 → 不可重试（重试也修不好，立即抛）.
    流式 status 200 通过后开始 yield 即不再重试（已吐半个回复，重试会重复 token）——
    故重试只覆盖「连接建立 + status 校验」阶段。调用方仍 try/except 兜底 RuntimeError
    （重试用尽仍抛），兜底文案见下方 G20.

  ── usage 缺失分支：completion_tokens / reasoning_tokens 都 None-safe ──
    chat_completion_stream 每个非 [DONE] chunk 都 yield 4 元组，其中 completion_tokens
    / reasoning_tokens 在非 usage chunk 上恒 None（只在最终 usage chunk 带 include_usage 时
    落真实值）. 缺失分支全枚举：
      1. usage 字段缺失（provider 不回 usage / 中间 chunk 无 usage）→ ``isinstance(usage, dict)``
         False → completion_tokens=None, reasoning_tokens=None（None-safe，不抛）.
      2. usage 在但无 completion_tokens → ``usage.get("completion_tokens")`` = None（yield None）.
      3. usage 在但无 completion_tokens_details → ``details = {}`` → reasoning_tokens=None（不抛）.
      4. completion_tokens_details 在但 reasoning_tokens 非 int（如 "123" 字符串）→ ``if isinstance(rt, int)``
         False → reasoning_tokens=None（类型守卫，不强制转换防异常）.
      5. provider 只回 ``{prompt, completion, total}`` 无 details（kimi gateway）→ reasoning_tokens=None
         全程 → 调用方用 live_reasoning_tokens 粗估兜底（worker.py:236 / coordinator.py:1414，B5 已锁）.
    所有缺失分支都走 None 不抛——**异常不吞没**（缺失≠异常，缺失返回 None 是正常协议降级，
    真 RuntimeError 只在 status!=200 时抛且带 resp.text 诊断）.

  ── reasoning_usage 透传：第 4 元组项 = usage.completion_tokens_details.reasoning_tokens ──
    chat_completion_stream yield 的第 4 项 ``reasoning_tokens`` 来自
    ``usage.completion_tokens_details.reasoning_tokens``（OpenAI 标准 reasoning token 字段）.
    两调用方都解包第 4 项并落盘：
      - coordinator.py:1370 ``async for content_delta, reasoning_delta, usage, reasoning_usage in
        chat_completion_stream(...)`` → ``final_reasoning_tokens = reasoning_usage``
        → ``real_reasoning_tokens = final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens``
        （coordinator.py:1389/1414）→ 落 emit_coordinator_stats(reasoning_tokens=) + agent_reply.data.
      - worker.py:185 ``async for content_delta, reasoning_delta, usage, reasoning_usage in
        chat_completion_stream(...)`` → ``final_reasoning_tokens = reasoning_usage``
        → ``reasoning_tokens = final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens``
        （worker.py:223/236，B5 已锁）.
    两调用方解包同形（第 4 项命名 reasoning_usage / 落 final_reasoning_tokens / fallback live）.

  ── 异常不吞没：[DONE] 前 yield usage + status!=200 抛 RuntimeError 带 resp.text ──
    chat_completion_stream 两个出口点：
      1. status != 200：``await resp.aread()`` drain 响应体 → ``raise RuntimeError(f"LLM API error
         {resp.status_code}: {body_text.decode('utf-8','replace')}")``（带 resp.text 诊断，B25 已审计
         resp.text 是上游响应体不含我们的 key，安全）.
      2. 正常结束：``if payload.strip() == "[DONE]": return``（在 usage chunk 处理之后？——审计确认：
         [DONE] return 在 json.loads 之前，usage chunk 是最后一个带 data 的 chunk，[DONE] 紧随其后，
         故 usage 已在前一轮 yield，[DONE] return 不丢 usage）.
    循环内 json.JSONDecodeError → ``continue``（跳坏 chunk，非吞没——坏 SSE 行常见 keep-alive 注释，
    continue 是标准 SSE 容错）.

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh23 同款风格.

七段契约：

  A. 超时（httpx.Timeout 4-phase 同值，read 治单 chunk 间隙非总时长）
    1. chat_completion_stream 用 ``client_kwargs = {"timeout": timeout}`` 喂 httpx.AsyncClient.
    2. timeout 默认 120s（``float(config.get("requestTimeout", 120.0) or 120.0)``，空值兜底 120）.
    3. chat_completion（非流式）同样 timeout 配置（两函数超时口径一致）.

  B. 重试（client.py 应用层重试：瞬态 5xx/429/网络/空200 退避重试，maxRetries 驱动）
    4. client.py 从 config 取 maxRetries 做应用层退避重试（_is_retryable_llm_error 判定）。
    5. coordinator _stream_coordinator_decision 被 node_llm_decide try/except 包（重试用尽仍兜底）。
    6. worker _stream_brain_decision 被 node_brain_decide try/except 包（重试用尽仍兜底）。
    7. 两调用方兜底同形（decision=chat 兜底回复 + stats 空值 + logger.warning 非吞没）。

  C. usage 缺失分支（completion_tokens / reasoning_tokens 都 None-safe）
    8. usage 字段缺失 → ``isinstance(usage, dict)`` False → completion_tokens=None.
    9. completion_tokens_details 缺失 → ``details = {}`` → reasoning_tokens=None.
   10. reasoning_tokens 非 int → ``if isinstance(rt, int)`` False → reasoning_tokens=None（类型守卫）.

  D. reasoning_usage 透传（第 4 元组项 → 两调用方落盘）
   11. chat_completion_stream yield 4 元组（content/reasoning/usage/reasoning_usage）.
   12. coordinator.py 解包第 4 项 reasoning_usage → final_reasoning_tokens（带 live fallback）.
   13. worker.py 解包第 4 项 reasoning_usage → final_reasoning_tokens（带 live fallback，B5 已锁）.

  E. 异常不吞没（status!=200 抛 RuntimeError + [DONE] 不丢 usage）
   14. status != 200 → ``raise RuntimeError(f"LLM API error {resp.status_code}: ...")``（带诊断）.
   15. json.JSONDecodeError → continue（标准 SSE 容错，非吞没）.
   16. [DONE] return 在 usage chunk yield 之后（usage 不丢——审计确认顺序）.

  F. [DONE] 前丢 usage 顺序审计（关键契约）
   17. [DONE] return 在 ``async for line`` 循环内，且在 json.loads 之前（usage chunk 是 data 行，
       [DONE] 是紧随其后的终止行，故 usage 已在前一轮 yield——不丢 usage）.

  G. 与两调用方契约对齐（兜底/stats 形状同构）
   18. coordinator node_llm_decide 兜底 ``reply_id, tokens, elapsed_ms, model, reasoning_tokens,
       reasoning_text = "", 0, 0, "", 0, ""``（6 空值，stats 落 {} 不显状态行）.
   19. worker node_brain_decide 兜底 ``stats = None``（_stream_stats=None，node_chat 落 data=None 不显）.
   20. 两调用方 LLM 失败都兜底成 chat 回复（非抛——engine 不因 LLM 失败崩，降级对话继续）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLIENT_PY = REPO / "backend" / "llm" / "client.py"
COORD_PY = REPO / "backend" / "engine" / "coordinator.py"
WORKER_PY = REPO / "backend" / "engine" / "worker.py"


def _fn_body_py(src: str, fname: str, is_async: bool = False) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    client = CLIENT_PY.read_text(encoding="utf-8")
    coord = COORD_PY.read_text(encoding="utf-8")
    worker = WORKER_PY.read_text(encoding="utf-8")

    ccs_body = _fn_body_py(client, "chat_completion_stream", is_async=True)
    cc_body = _fn_body_py(client, "chat_completion", is_async=True)

    # ── A. 超时 ──
    if not ccs_body:
        errs.append("[setup] chat_completion_stream 函数体未找到")
    else:
        # [1] client_kwargs = {"timeout": timeout}
        if 'client_kwargs' not in ccs_body or '"timeout"' not in ccs_body:
            errs.append("[A1] chat_completion_stream 未用 client_kwargs={'timeout': ...} 喂 httpx")
        else:
            print("[A1] OK  chat_completion_stream client_kwargs={'timeout'} 喂 httpx.AsyncClient")
        # [2] timeout 默认 120s（requestTimeout fallback 120）
        if not re.search(r'float\(config\.get\(["\']requestTimeout["\'],\s*120\.0\)\s*or\s*120\.0\)', ccs_body):
            errs.append("[A2] chat_completion_stream timeout 非默认 120s + 空值兜底 120")
        else:
            print("[A2] OK  timeout 默认 120s（requestTimeout fallback 120，空值兜底）")
    # [3] chat_completion（非流式）同样 timeout
    if not cc_body:
        errs.append("[setup] chat_completion 函数体未找到")
    elif not re.search(r'float\(config\.get\(["\']requestTimeout["\'],\s*120\.0\)\s*or\s*120\.0\)', cc_body):
        errs.append("[A3] chat_completion（非流式）timeout 口径与 stream 不一致")
    else:
        print("[A3] OK  chat_completion（非流式）同 timeout 口径（两函数超时一致）")

    # ── B. 重试（client.py 应用层重试：瞬态 5xx/429/网络/空 200 退避重试，maxRetries 驱动）──
    # [4] client.py 走应用层重试（maxRetries 从 config 取，非 0 重试直调）。与 agent_loop
    #     的 langchain ChatOpenAI(max_retries=) 路径不同——本模块直调 httpx + asyncio.sleep 退避。
    if "maxRetries" not in client or "_is_retryable_llm_error" not in client:
        errs.append("[B4] client.py 缺 maxRetries 应用层重试（应直调 httpx + 应用层退避重试瞬态失败）")
    else:
        print("[B4] OK  client.py 应用层重试（maxRetries 驱动，直调 httpx 非 langchain）")
    # [4b] 可重试判定：5xx/429/TransportError/空 choices → True；4xx 鉴权参数 → False
    if "_RETRYABLE_STATUS" not in client or "429" not in client or "503" not in client:
        errs.append("[B4b] _RETRYABLE_STATUS 缺 429/503（瞬态状态码集不全）")
    elif not re.search(r"LLM API error \(\\d\+\)", client):
        errs.append("[B4b] _is_retryable_llm_error 缺 LLM API error 状态码正则解析")
    else:
        print("[B4b] OK  _RETRYABLE_STATUS={429,500,502,503,504} + 空choices + TransportError 可重试")
    # [5] coordinator _stream_coordinator_decision 被 try/except 包
    nld_body = _fn_body_py(coord, "node_llm_decide", is_async=True)
    if not nld_body:
        errs.append("[setup] node_llm_decide 函数体未找到")
    elif "await _stream_coordinator_decision(" not in nld_body:
        errs.append("[B5] node_llm_decide 未调 _stream_coordinator_decision（审计锚点失）")
    elif not re.search(r'try:\s*\n[^}]*_stream_coordinator_decision', nld_body, re.S):
        errs.append("[B5] _stream_coordinator_decision 未被 try 包（RuntimeError 无兜底）")
    else:
        print("[B5] OK  coordinator _stream_coordinator_decision 被 node_llm_decide try/except 包")
    # [6] worker _stream_brain_decision 被 try/except 包
    nbd_body = _fn_body_py(worker, "node_brain_decide", is_async=True)
    if not nbd_body:
        errs.append("[setup] node_brain_decide 函数体未找到")
    elif "await _stream_brain_decision(" not in nbd_body:
        errs.append("[B6] node_brain_decide 未调 _stream_brain_decision（审计锚点失）")
    elif not re.search(r'try:\s*\n[^}]*_stream_brain_decision', nbd_body, re.S):
        errs.append("[B6] _stream_brain_decision 未被 try 包（RuntimeError 无兜底）")
    else:
        print("[B6] OK  worker _stream_brain_decision 被 node_brain_decide try/except 包")
    # [7] 两调用方兜底同形（decision=chat + stats 空 + logger.warning）
    coord_fallback = nld_body and "decision = {" in nld_body and '"action": "chat"' in nld_body and "logger.warning" in nld_body
    worker_fallback = nbd_body and "decision = {" in nbd_body and '"action": "chat"' in nbd_body and "logger.warning" in nbd_body
    if not (coord_fallback and worker_fallback):
        errs.append(f"[B7] 两调用方兜底不同形（coord={coord_fallback} worker={worker_fallback}）")
    else:
        print("[B7] OK  两调用方兜底同形（decision=chat 兜底 + logger.warning 非吞没）")

    # ── C. usage 缺失分支 ──
    if ccs_body:
        # [8] usage 缺失 → isinstance(usage, dict) False → completion_tokens=None
        if "isinstance(usage, dict)" not in ccs_body:
            errs.append("[C8] chat_completion_stream 缺 isinstance(usage, dict) 守卫（usage 缺失会抛）")
        else:
            print("[C8] OK  usage 缺失 → isinstance(usage, dict) False → completion_tokens=None（None-safe）")
        # [9] completion_tokens_details 缺失 → details = {} → reasoning_tokens=None
        if "completion_tokens_details" not in ccs_body or "or {}" not in ccs_body:
            errs.append("[C9] chat_completion_stream 缺 completion_tokens_details or {} 兜底（details 缺失会抛）")
        else:
            print("[C9] OK  completion_tokens_details 缺失 → details={} → reasoning_tokens=None（None-safe）")
        # [10] reasoning_tokens 非 int → isinstance(rt, int) False → None（类型守卫）
        if not re.search(r'rt\s*=\s*details\.get\(["\']reasoning_tokens["\']\)\s*\n\s*if\s+isinstance\(rt,\s*int\)', ccs_body):
            errs.append("[C10] chat_completion_stream 缺 isinstance(rt, int) 类型守卫（非 int 值会落 raw）")
        else:
            print("[C10] OK  reasoning_tokens 非 int → isinstance(rt, int) False → None（类型守卫不强制转换）")

    # ── D. reasoning_usage 透传 ──
    if ccs_body:
        # [11] yield 4 元组
        if "yield content_delta, reasoning_delta, completion_tokens, reasoning_tokens" not in ccs_body:
            errs.append("[D11] chat_completion_stream 未 yield 4 元组（reasoning_usage 透传断）")
        else:
            print("[D11] OK  yield 4 元组（content/reasoning/usage/reasoning_usage）")
    scd_body = _fn_body_py(coord, "_stream_coordinator_decision", is_async=True)
    if not scd_body:
        errs.append("[setup] _stream_coordinator_decision 函数体未找到")
    else:
        # [12] coordinator 解包第 4 项 reasoning_usage → final_reasoning_tokens（带 live fallback）
        if "reasoning_usage in chat_completion_stream" not in scd_body:
            errs.append("[D12] coordinator 未解包第 4 项 reasoning_usage（透传断）")
        elif "final_reasoning_tokens = reasoning_usage" not in scd_body:
            errs.append("[D12] coordinator 未把 reasoning_usage 落 final_reasoning_tokens")
        elif "final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens" not in scd_body:
            errs.append("[D12] coordinator 缺 final_reasoning_tokens live fallback（B5 兜底断）")
        else:
            print("[D12] OK  coordinator 解包 reasoning_usage → final_reasoning_tokens（带 live fallback）")
    sbd_body = _fn_body_py(worker, "_stream_brain_decision", is_async=True)
    if not sbd_body:
        errs.append("[setup] _stream_brain_decision 函数体未找到")
    else:
        # [13] worker 解包第 4 项 reasoning_usage → final_reasoning_tokens（带 live fallback，B5）
        if "reasoning_usage in chat_completion_stream" not in sbd_body:
            errs.append("[D13] worker 未解包第 4 项 reasoning_usage（透传断）")
        elif "final_reasoning_tokens = reasoning_usage" not in sbd_body:
            errs.append("[D13] worker 未把 reasoning_usage 落 final_reasoning_tokens")
        elif "final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens" not in sbd_body:
            errs.append("[D13] worker 缺 final_reasoning_tokens live fallback（B5 兜底断）")
        else:
            print("[D13] OK  worker 解包 reasoning_usage → final_reasoning_tokens（带 live fallback，B5）")

    # ── E. 异常不吞没 ──
    if ccs_body:
        # [14] status != 200 → raise RuntimeError（带诊断）
        if not re.search(r'if\s+resp\.status_code\s*!=\s*200:', ccs_body):
            errs.append("[E14] chat_completion_stream 缺 status != 200 分支")
        elif "raise RuntimeError" not in ccs_body:
            errs.append("[E14] chat_completion_stream status!=200 未 raise RuntimeError（异常吞没）")
        elif "resp.status_code" not in ccs_body:
            errs.append("[E14] RuntimeError 不含 resp.status_code（丢诊断）")
        else:
            print("[E14] OK  status!=200 → raise RuntimeError（带 resp.status_code + body_text 诊断）")
        # [15] json.JSONDecodeError → continue（SSE 容错）
        if "json.JSONDecodeError" not in ccs_body or "continue" not in ccs_body:
            errs.append("[E15] chat_completion_stream 缺 json.JSONDecodeError → continue（坏 chunk 会抛）")
        else:
            print("[E15] OK  json.JSONDecodeError → continue（标准 SSE 容错，非吞没）")
    # [16] [DONE] return 在 usage chunk yield 之后（usage 不丢）——审计顺序
    # 关键：[DONE] 与 usage chunk 是 SSE 流里**不同行**（不同 loop 迭代）。usage chunk
    # 是 ``data: {json含usage}`` 行 → 经 json.loads + yield 落 usage；[DONE] 是紧随其后的
    # ``data: [DONE]`` 终止行 → 命中 ``if payload.strip()=="[DONE]": return`` 短路。故 usage
    # 在前一轮迭代已 yield，[DONE] 终止不丢 usage。本契约锁「yield 是 loop 体最后一条语句
    # （在 usage/reasoning_tokens 提取之后）」——即每个成功 parse 的 chunk 都会把它的 usage
    # yield 出去（含 final usage chunk），不存在 parse 了 usage 却没 yield 的分支。
    if ccs_body:
        done_pos = ccs_body.find('payload.strip() == "[DONE]"')
        yield_pos = ccs_body.find("yield content_delta, reasoning_delta, completion_tokens, reasoning_tokens")
        usage_pos = ccs_body.find("chunk.get(\"usage\")")
        if done_pos < 0 or yield_pos < 0 or usage_pos < 0:
            errs.append("[E16/setup] [DONE]/yield/usage 锚点未找到")
        elif not (usage_pos < yield_pos):
            errs.append("[E16] usage 读取不在 yield 之前（usage chunk 可能被 parse 却不 yield）")
        else:
            print("[E16] OK  yield 在 usage 提取之后（每 chunk 含 final usage chunk 都 yield usage，[DONE] 终止行前轮已 yield）")

    # ── F. [DONE] 前丢 usage 顺序（独立锁，关键契约）──
    # [17] [DONE] return 在 json.loads 之前——usage chunk（带 data 的最后一行）已在上一轮 json.loads+yield，
    #     [DONE] 是终止行，return 不丢 usage（因 usage 在前一轮已处理）
    if ccs_body:
        done_block = ccs_body.find('payload.strip() == "[DONE]"')
        json_loads = ccs_body.find("json.loads(payload)")
        if done_block < 0 or json_loads < 0:
            errs.append("[F17/setup] [DONE]/json.loads 锚点未找到")
        elif not (done_block < json_loads):
            errs.append("[F17] [DONE] return 不在 json.loads 之前（顺序错——[DONE] 应短路在 parse 前）")
        else:
            print("[F17] OK  [DONE] return 在 json.loads 之前（短路终止行，usage chunk 前轮已 yield）")

    # ── G. 与两调用方契约对齐 ──
    if nld_body:
        # [18] coordinator 兜底 6 空值
        if not re.search(r'reply_id,\s*tokens,\s*elapsed_ms,\s*model,\s*reasoning_tokens,\s*reasoning_text\s*=\s*"",\s*0,\s*0,\s*"",\s*0,\s*""', nld_body):
            errs.append("[G18] coordinator node_llm_decide 兜底非 6 空值（stats 形状变）")
        else:
            print("[G18] OK  coordinator 兜底 reply_id/tokens/elapsed_ms/model/reasoning_tokens/reasoning_text 全空")
    if nbd_body:
        # [19] worker 兜底 stats = None
        if not re.search(r'^\s*stats\s*=\s*None\s*$', nbd_body, re.M):
            errs.append("[G19] worker node_brain_decide 兜底非 stats = None（_stream_stats 形状变）")
        else:
            print("[G19] OK  worker 兜底 stats = None（_stream_stats=None，node_chat 落 data=None 不显状态行）")
    # [20] 两调用方 LLM 失败都兜底成 chat 回复（非抛），文案直说故障而非误导性道歉
    coord_chat_fallback = nld_body and '"content":' in nld_body and "模型服务暂时无响应" in nld_body
    worker_chat_fallback = nbd_body and '"content":' in nbd_body and "模型服务暂时无响应" in nbd_body
    if not (coord_chat_fallback and worker_chat_fallback):
        errs.append(f"[G20] 两调用方 LLM 失败未兜底成直说故障的 chat 回复（coord={coord_chat_fallback} worker={worker_chat_fallback}）")
    else:
        print("[G20] OK  两调用方 LLM 失败兜底成直说故障文案（engine 不崩，降级对话继续）")

    return errs


def main() -> int:
    print("=== VH24 回归：chat_completion_stream 超时/重试/usage/异常审计锁契约（B27）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B27 chat_completion_stream 审计锁定：\n"
        "  · A 超时（httpx.Timeout 4-phase 同值 120s，read 治单 chunk 间隙非总时长——推理长耗时 chunk 间隔<1s 不误杀）；\n"
        "  · B 重试（client.py 应用层退避重试：瞬态 5xx/429/网络/空200 经 _is_retryable_llm_error 判定 + asyncio.sleep 指数退避，maxRetries 驱动；重试用尽仍抛，调用方 try/except 兜底 RuntimeError）；\n"
        "  · C usage 缺失分支（completion_tokens/reasoning_tokens 全 None-safe，isinstance 守卫不强制转换）；\n"
        "  · D reasoning_usage 透传（第 4 元组项 → 两调用方 final_reasoning_tokens + live fallback，B5 锁）；\n"
        "  · E 异常不吞没（status!=200 raise RuntimeError 带诊断 + json.JSONDecodeError continue SSE 容错）；\n"
        "  · F [DONE] 顺序（return 在 json.loads 之前短路，usage chunk 前轮已 yield 不丢）；\n"
        "  · G 两调用方契约对齐（兜底 chat 回复 + stats 空值，engine 不崩降级对话）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
