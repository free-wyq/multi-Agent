"""Mock OpenAI-compatible LLM HTTP server for Playwright e2e (任务20a).

启动方式：被 e2e/global-setup.ts 作为子进程 spawn（``python3 -m
playwright_mock_llm_server``，经 ``backend/tests`` 入 sys.path）。

为什么是「真起一个 OpenAI 兼容 HTTP server」而非 monkeypatch：
- Playwright 的 webServer 把 FastAPI 当独立子进程起，后端进程内无法
  monkeypatch。要让后端真正不打真 LLM，必须给它一个 *能连的 HTTP 端点*，
  让它按 OpenAI Chat Completions 协议回确定性假回复。后端零改动（只把
  active provider 的 base_url 指向本 server），所有 worker brain / coordinator
  / ReAct 三条 LLM 调用路径都走 httpx 打到本 server，行为与真实上游一致，
  仅响应内容脚本化。
- monkeypatch 方案（任务16b mc e2e / 19e im e2e 用）只能锁单进程内 in-process
  链路；端到端跨进程的 Playwright 必须 HTTP 级 mock，这是它与契约测试的根本区别。

协议覆盖：
1. ``POST /v1/chat/completions``（stream=false，coordinator chat_completion +
   chat_completion_stream 的非流式分支；worker brain 的 chat_completion_stream
   也走 SSE，但本 server 的流式实现与它们共用同一份「响应脚本」）。
2. ``POST /v1/chat/completions``（stream=true，SSE）—— ``chat_completion_stream``
   的真链路：worker ``_stream_brain_decision`` + coordinator
   ``_stream_coordinator_decision`` 都消费它。
3. ``POST /v1/chat/completions`` 的 ReAct 路径——``run_agent_loop`` 经
   ``langchain_openai.ChatOpenAI`` 调（非流式 ``chat.completions.create``）。

确定性策略（让 e2e 稳定可复现）：
- 默认对任何消息回一条 ``chat`` action 的回复（worker brain 期望 strict JSON
  ``{"action":"chat","content":"..."}``；coordinator 期望
  ``{"action":"chat","content":"..."}``）——把可见正文塞进 content 字段，
  让 ``ContentExtractor`` 增量解码出真实文本，流式气泡 / 定稿回复 / 持久化
  content 三处同源。
- 识别关键词触发不同脚本：含 ``[卡片]`` → 回带 ``card`` 围栏的 table 卡片
  内容（结构化卡片 e2e）；含 ``execute`` / 「调用」/「执行」→ 回
  ``{"action":"execute",...}`` 让 worker 走 execute 路径（push_task +
  ReAct）；含 ``handoff`` /「转交」→ coordinator 回 dispatch action。
- ReAct 循环（``create_react_agent``）目前 e2e 只覆盖「brain=chat」即止的
  路径（不发工具调用），故默认回复不含 tool_calls；execute 路径的 ReAct 工具
  调用由真实 MCP echo fixture（任务16a 已有）或后续 20b/20c 接入。

SSE chunk 形状严格遵守 OpenAI 协议（``backend/llm/client.py:chat_completion_stream``
的解析契约）：
  data: {"choices":[{"delta":{"content":"<chunk>"}}]}
  ...
  data: {"choices":[],"usage":{"prompt_tokens":N,"completion_tokens":M,"total_tokens":N+M,"completion_tokens_details":{"reasoning_tokens":0}}}
  data: [DONE]

逐字 yield content（让流式 token 经 ``ContentExtractor.feed/take`` 增量解码，
与真实流式期同源），末尾一个 usage chunk（``include_usage: true`` 契约），
最后 ``[DONE]``。
"""
from __future__ import annotations

import json
import os

from aiohttp import web

# 可见正文里的稳定探针串——e2e 断言「回复里有它」即可，无需精确匹配全文
# （LLM 回复长度/措辞会变，但探针串固定，断言稳）。
DEFAULT_REPLY_TEXT = "[e2e-mock] 你好，这是 mock LLM 的确定性回复。"
CARD_REPLY_TEXT = "[e2e-mock] 结构化卡片演示。"
EXECUTE_REPLY_TEXT = "[e2e-mock] 走 execute 路径。"
HANDOFF_REPLY_TEXT = "[e2e-mock] coordinator 派工。"

# 把可见正文塞进 strict JSON 的 content 字段——worker brain 期望
# {"action","content","reasoning"}，coordinator 期望 {"action","content","plan"}。
# 两者的 _parse_*_decision 都用 extract_json 取 content，故同一份 JSON 两边都能解析。
def _brain_chat_payload(text: str) -> str:
    return json.dumps(
        {"action": "chat", "content": text, "reasoning": "e2e mock"},
        ensure_ascii=False,
    )


def _brain_execute_payload(text: str) -> str:
    return json.dumps(
        {"action": "execute", "content": text, "reasoning": "e2e mock execute"},
        ensure_ascii=False,
    )


def _coordinator_dispatch_payload(text: str) -> str:
    """coordinator 派工脚本——action=dispatch + 真实可派工 plan。

    20b 群聊协作 e2e 用：发关键词「派工」→ coordinator 走 dispatch 路径
    （node_dispatch → announce + emit_coordinator_plan → interrupt 等确认），
    前端 PlanConfirmCard 渲染，用户点「确认继续」→ resume → dispatch_next fan-out
    到 agent 节点 → worker brain 回复。覆盖计划确认闭环（PL-02）。

    plan 引用 seed agent_frontend_1（前端工程师）——20b 群聊 e2e 建群必含 seed
    agents（协调者/前端工程师/后端工程师），故 agent_frontend_1 必在群图注册为
    ``agent_<id>`` 节点，``build_dispatch_sends`` 的 ``Send`` 能命中。instruction
    不含 card/execute/handoff/派工 关键词，避免 worker brain 收到派发消息时被
    mock 路由到其他脚本（worker 会按默认 chat 回 DEFAULT_REPLY_TEXT）。
    """
    plan = [
        {
            "step": 1,
            "agent_id": "agent_frontend_1",
            "agent_name": "前端工程师",
            "instruction": "实现登录页面表单与校验",
            "depends_on": [],
        }
    ]
    return json.dumps(
        {"action": "dispatch", "content": text, "plan": plan},
        ensure_ascii=False,
    )


def _card_table_payload(text: str) -> str:
    """带结构化卡片围栏的回复——``card`` fenced block + table JSON。

    锁 ``docs/structured-result-card-schema.md`` 契约：前端 ``CARD_RE`` 解析、
    ``count_card_fragments`` 统计、AntD Table 渲染。e2e 断言「页面里出现表格」。
    """
    card_json = json.dumps(
        {
            "icon": "🔥",
            "title": "e2e 演示榜单",
            "kind": "table",
            "columns": ["排名", "标题", "热度"],
            "rows": [
                ["1", "e2e 第一条", "100"],
                ["2", "e2e 第二条", "80"],
            ],
        },
        ensure_ascii=False,
    )
    # 围栏内是合法 JSON（已转义）；围栏本身三个反引号。content 字段里夹围栏块
    # 不会破坏外层 JSON（围栏内的 JSON 字符串是 content 的值，已用 json.dumps
    # 转义了内部双引号）。
    content = f"{text}\n\n```card\n{card_json}\n```\n"
    return json.dumps(
        {"action": "chat", "content": content, "reasoning": "e2e mock card"},
        ensure_ascii=False,
    )


def _pick_response(messages: list[dict]) -> str:
    """按最后一条 user 消息内容选脚本化响应（worker brain / coordinator 共用）。

    后端 ``build_brain_prompt`` / ``build_coordinator_prompt`` 把 incoming_message
    拼进 user 消息，故最后一条 user content 即用户输入。关键词命中即分流，
    否则默认 chat 回复。

    匹配优先级（先特后普，避免「派工完成登录」里的「登录」误命中 execute 分支）：
    1. ``dispatch``/``派工``/``转交`` → coordinator dispatch 脚本（action=dispatch + plan）。
       仅 coordinator 走这条（worker 不会收到含「派工」的原句——@mention 转交场景
       worker 收到的是 ``[来自智能体 X]`` 前缀消息，不含原「派工」关键词）。
    2. ``[卡片]``/``卡片``/``榜单`` → card table 脚本。
    3. ``execute``/``执行``/``调用工具`` → worker execute 脚本。
    4. 默认 → chat 脚本（DEFAULT_REPLY_TEXT 探针串）。
    """
    last_user = ""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = str(m.get("content") or "")
            break
    text = last_user
    # 「派工」必须先于「执行」匹配——用户消息「请派工完成登录功能」里虽不含「执行」
    # 二字，但若 dispatch 关键词后置，某些组合消息可能含「执行派工」之类双重命中。
    # dispatch 优先：一旦命中派工走 coordinator dispatch，不再走 execute。
    if "dispatch" in text.lower() or "派工" in text or "转交" in text:
        return _coordinator_dispatch_payload(HANDOFF_REPLY_TEXT)
    if "[卡片]" in text or "卡片" in text or "榜单" in text:
        return _card_table_payload(CARD_REPLY_TEXT)
    if "execute" in text.lower() or "执行" in text or "调用工具" in text:
        return _brain_execute_payload(EXECUTE_REPLY_TEXT)
    return _brain_chat_payload(DEFAULT_REPLY_TEXT)


def _sse_chunks(payload_str: str) -> bytes:
    """把 payload_str 当作 content 字段值，逐字符吐 OpenAI SSE 流。

    ``chat_completion_stream`` 按 ``data: <json>`` 行解析，每行 choices[0].delta.
    content 是一个增量片段。末尾一个 usage chunk + ``[DONE]``。
    """
    lines: list[str] = []
    for ch in payload_str:
        delta = json.dumps(
            {"choices": [{"index": 0, "delta": {"content": ch}}]},
            ensure_ascii=False,
        )
        lines.append(f"data: {delta}")
    # final usage chunk (include_usage 契约)：choices 空 + usage 填满。
    usage = json.dumps(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": max(1, len(payload_str) // 3),
                "total_tokens": 10 + max(1, len(payload_str) // 3),
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
        ensure_ascii=False,
    )
    lines.append(f"data: {usage}")
    lines.append("data: [DONE]")
    lines.append("")  # 末尾空行让 aiter_lines 收尾
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _chat_completions(request: web.Request) -> web.StreamResponse:
    """OpenAI 兼容 ``/v1/chat/completions``：stream / 非流式都支持。

    body.model / messages 解析后交给 ``_pick_response`` 选脚本。stream=true
    返 SSE（逐字），stream=false 返普通 JSON（content 一次给全）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    messages = body.get("messages") or []
    payload_str = _pick_response(messages)

    if body.get("stream"):
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        await resp.write(_sse_chunks(payload_str))
        await resp.write_eof()
        return resp

    # 非流式：ReAct ``ChatOpenAI`` 走这条（langchain chat.completions.create）。
    # 注意 ChatOpenAI 期望 choices[0].message.content 是最终文本——但 worker
    # brain 的 ``_parse_brain_decision`` 期望 content 是 strict JSON。
    # 两者消息内容同源（``_pick_response`` 都吐 JSON），区别仅在调用方：brain
    # 走 ``chat_completion_stream``（SSE，本 server 流式分支），ReAct 走
    # ``ChatOpenAI``（非流式，本分支）。ReAct 默认场景（brain=chat 即止）不触
    # 发工具调用，content 字段是 JSON 文本，``create_react_agent`` 当普通回复
    # 文本透传给最终 output——不破坏 ReAct 契约（无 tool_calls → END）。
    resp_body = {
        "id": "chatcmpl-e2e-mock",
        "object": "chat.completion",
        "model": body.get("model", "e2e-mock-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": payload_str},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": max(1, len(payload_str) // 3),
            "total_tokens": 10 + max(1, len(payload_str) // 3),
        },
    }
    return web.json_response(resp_body)


async def _health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "mock-llm"})


async def _models(_: web.Request) -> web.Response:
    """``/v1/models`` —— langchain ChatOpenAI 偶尔自省模型列表，给个最小回包。"""
    return web.json_response(
        {
            "object": "list",
            "data": [
                {"id": "e2e-mock-model", "object": "model", "owned_by": "e2e"}
            ],
        }
    )


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", _chat_completions)
    app.router.add_get("/v1/chat/completions", _chat_completions)  # 防御
    app.router.add_get("/v1/models", _models)
    app.router.add_get("/health", _health)
    app.router.add_get("/", _health)
    return app


def main() -> int:
    """``python3 -m playwright_mock_llm_server`` 入口。

    端口由 env ``E2E_MOCK_LLM_PORT`` 决定（global-setup.ts 分配后传入）；
    缺省 8765 兜底（不与 8000/5173 冲突）。
    """
    port = int(os.environ.get("E2E_MOCK_LLM_PORT", "8765"))
    web.run_app(build_app(), host="127.0.0.1", port=port, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
