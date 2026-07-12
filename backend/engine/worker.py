"""Worker StateGraph — 4 nodes + conditional edge (Rust handle_notify worker branch).

Nodes: brain, chat, execute, ask. The brain node calls the worker LLM for a
three-state decision (chat/execute/ask). ``execute`` replies with a preview
and ``push_task`` to itself (the engine's _handle_task then runs the CLI via
``_run_worker_task``). The graph is compiled once with a MemorySaver
checkpointer and invoked per incoming notify by ``AgentEngine._handle_notify``.
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from engine.coordinator import _ContentExtractor
from engine.inbox import push_task
from engine.state import WorkerState
from events import emit_coordinator_reasoning, emit_message_added, emit_task_token
from llm.client import chat_completion_stream, get_llm_config
from llm.extract_json import extract_json
from llm.prompts import build_brain_prompt
from store import crud

logger = logging.getLogger("multi-agent.worker")

# reply callback installed by the engine for the duration of one invoke.
# 用 contextvars 而非模块级全局变量：每个 agent engine 是独立 asyncio task（registry
# 用 asyncio.create_task(self._run_loop())），task 创建时 copy context，各自 set 的 cb
# 互不覆盖。原全局单例在并发场景（前端后端同时 ainvoke）会被后 set 的覆盖先 set 的 →
# 后端 _unified_reply 时 _REPLY_CB 已被前端的 set_reply_callback(None) 清空 → @peer
# 不路由 → 接龙只跑一轮就断（话筒落地）。contextvars 让每个 task 有自己独立的 cb。
_REPLY_CB: contextvars.ContextVar = contextvars.ContextVar(
    "worker_reply_cb", default=None
)


def set_reply_callback(cb: Any) -> None:
    """Install the engine's unified reply callable for the duration of one invoke.

    Sets the callback in the *current task's* context (contextvars), so
    concurrent engine invokes (front/back brain running in parallel) each see
    their own cb — not a shared global that the last writer wins.
    """
    _REPLY_CB.set(cb)


async def _unified_reply(
    group_id: str, agent_id: str, content: str, data: dict[str, Any] | None = None
) -> None:
    """Persist an agent_reply + emit + mention route (Rust engine.reply).

    ``data`` 写到持久化 message 上（存活重载/重连）。worker chat/ask 路径把
    brain 流式采集的 run-stats（``{elapsed_ms, tokens, model, reasoning_tokens,
    reasoning?}``）传进来，定稿气泡据此渲染「model · Ns · ↓ N tokens（含 N 推理）」
    状态行——与协调者 node_chat 落盘 ``_stream_stats`` 同形（前端 extractCoordStats
    不区分来源，按 data.elapsed_ms 是否存在判定渲染）。execute 路径回复是模板化
    announce（``收到，我来 {preview}...``），不匹配 brain token，不带 stats（传 None）。
    """
    msg = await crud.create_message(
        {
            "group_id": group_id,
            "task_id": None,
            "sender_id": agent_id,
            "receiver_id": "broadcast",
            "type": "agent_reply",
            "content": content,
            "data": data,
        }
    )
    await emit_message_added(msg.model_dump())
    cb = _REPLY_CB.get()
    if cb is not None:
        await cb(content)


def _build_context(memory: list[dict], agent_name: str) -> str:
    """Render the recent memory into a context string (Rust build_context).

    仅保留给旧路径/单聊兜底用——群聊成员的 brain 上下文现由
    ``_build_context_from_db`` 直接从 messages 表真源拉取（见 ``node_brain_decide``），
    不再依赖这套 self._memory + append 时序 + role 渲染。
    """
    recent = (memory or [])[-5:]
    if not recent:
        return "（无历史对话）"
    lines = []
    for m in recent:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            lines.append(f"用户: {content}")
        else:
            lines.append(f"{agent_name}: {content}")
    return "\n".join(lines)


async def _build_context_from_db(group_id: str, coordinator_id: str) -> str:
    """从 messages 表真源拉最近若干条，拼成带发送者身份的上下文。

    取代旧的 ``_build_context(self._memory)``——不再维护引擎内 ``self._memory`` 这套
    list + append 时序 + role 渲染。直接查 DB：每条消息的 ``sender_id`` 天然带身份，
    最近的当前 incoming 也在结果里（发送方落库在唤醒接收方之前），无时序坑、无身份
    抹平、换话题自然就是新消息流（跨场景不污染）。

    sender_id → 显示名：``user``→「用户」，``coordinator_id``→「协调者」，群成员→其
    agent name（按 id 匹配成员表；非成员的 agent_id 原样显示）。一次 DB 查 messages +
    一次查成员，相比下游 LLM 调用可忽略。

    取最近 8 条（含当前 incoming）：worker 来回对话通常短轮次，8 条够覆盖接龙上下文
    又不灌太多历史噪声。task_log/slash_card 等非对话型消息按 type 过滤掉，只留
    agent_reply / user_input / coordinator 的对话气泡。
    """
    members = await crud.list_group_members_with_agent(group_id)
    id_to_name: dict[str, str] = {m.agent_id: m.agent_name for m in members}
    if coordinator_id:
        id_to_name.setdefault(coordinator_id, "协调者")

    msgs = await crud.list_messages(group_id, limit=8)
    # 只留对话型消息（task_log 是执行过程产物，不该进 brain 上下文）
    msgs = [m for m in msgs if (m.type or "") in ("agent_reply", "user_input", "coordinator_reply", "coordinator_task")]
    if not msgs:
        return "（无历史对话）"
    lines = []
    for m in msgs:
        sender = m.sender_id or ""
        who = id_to_name.get(sender, "用户" if sender == "user" else sender)
        lines.append(f"[{who}] {m.content or ''}")
    return "\n".join(lines)


def _format_display_msg(sender: str, content: str) -> str:
    """Prefix non-user messages with the sender identity (Rust handle_notify)."""
    if sender != "user" and sender != "coordinator":
        return f"[来自智能体 {sender}] {content}"
    return content


async def _stream_brain_decision(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    group_id: str,
    agent_id: str,
) -> tuple[str, str, int, int, str, int, str]:
    """Stream the worker brain LLM, emitting per-token + reasoning events.

    镜像 ``coordinator._stream_coordinator_decision``：消费
    ``chat_completion_stream`` 的四元组 (content_delta/reasoning_delta/usage/
    reasoning_usage)，把可见 ``content`` 字段的解码增量经 ``emit_task_token`` 推
    WS（跳过 JSON 骨架，只推可见回复），reasoning_content 增量经
    ``emit_coordinator_reasoning`` 推 WS（复用协调者同款通道，思考与正文同
    ``reply_id`` 归并到同一流式气泡）。两路 emit 均 best-effort（try/except），
    WS 推送失败不阻断 brain 决策。采集 provider 真实 usage + 耗时 + model 交回
    调用方盖 ``_stream_stats`` 落盘。

    Returns ``(reply_id, raw_full, tokens, elapsed_ms, model, reasoning_tokens,
    reasoning_text)``：

    - ``reply_id`` — 每轮一个 uuid4 hex，单聊回复流式 token 的归并键（与协调者
      ``_stream_coordinator_decision`` 同构）。前端 ``coordStreaming[reply_id]`` 按
      reply_id 拼接逐字增量；落盘到 ``agent_reply.data.reply_id`` 后退场流式气泡。
    - ``raw_full`` — 组装的原始 LLM ``content``（strict JSON ``{"action","content",
      "reasoning"}``），交 ``_parse_brain_decision`` 解析（reasoning_content 不属
      raw_full——它不是回复正文）。
    - ``tokens`` — 最终 token 数（provider 回 usage 用真实 completion_tokens，否则
      退化为粗估 ``len(raw_full)//3``，与协调者 ``live_tokens`` 启发式一致，保证状态行
      总有数）。
    - ``elapsed_ms`` — 流式起止墙钟。
    - ``model`` — 产出本回复的 LLM model id（``config["model"]``），经 stats 落盘让
      气泡显示哪个模型答的（用户可经 provider 目录热切换）。
    - ``reasoning_tokens`` — reasoning 链 token 数（非推理模型为 0；无 fallback 估值，
      与协调者 ``live_reasoning_tokens`` 兜底不一致——B5 待统一）。
    - ``reasoning_text`` — 组装完整的 ``reasoning_content``（落盘到
      ``agent_reply.data["reasoning"]``，定稿气泡折叠区据此展开——流式期靠
      ``coordinator_reasoning`` 事件，定稿后 phase=done 清 coordReasoning，靠持久化
      文本才可再展开历史气泡推理）。
    """
    # reply_id（task 23）：每轮一个 uuid4 hex，作为单聊回复流式 token 的归并键。
    # 与协调者 _stream_coordinator_decision 的 reply_id 同构——前端 coordStreaming[reply_id]
    # 按 reply_id 拼接逐字增量。落盘到 agent_reply.data.reply_id 后，前端用「该 agent 在
    # 收尾时间戳之后的消息」判定持久化回复已落地、退场流式气泡——与协调者 phase=done
    # 清 coordStreaming 同模式。
    reply_id = uuid.uuid4().hex
    # worker brain 的 LLM 输出是 strict JSON（``{"action","content","reasoning"}``），
    # 可见回复是 ``content`` 字段值——若把 raw_parts（含 JSON 骨架/action/reasoning）逐字
    # 推给前端，流式气泡会渲染出 ``{"action":"chat","content":"...`` 这种骨架噪声。故复用
    # 协调者的 ``_ContentExtractor``：feed 每个 content_delta，take() 出「content 字段的
    # 解码增量」（跳过 JSON 骨架/转义解码/前导散文），只把真正的可见回复文本逐字推。
    extractor = _ContentExtractor()
    start = time.monotonic()
    raw_parts: list[str] = []
    # reasoning_content 全文累积——落盘到 agent_reply.data.reasoning，定稿气泡的折叠区据此
    # 展开（流式期靠 coordinator_reasoning 事件，定稿后靠持久化文本）。
    reasoning_parts: list[str] = []
    final_tokens = 0
    final_reasoning_tokens = 0
    async for content_delta, reasoning_delta, usage, reasoning_usage in chat_completion_stream(
        config, messages
    ):
        if content_delta:
            raw_parts.append(content_delta)
            # 推可见回复的解码增量（跳过 JSON 骨架），按 reply_id 归并。emit 失败
            # 不阻断 brain 决策（WS 推送是 best-effort，前端断连等不应让 worker 回复挂）。
            extractor.feed(content_delta)
            piece = extractor.take()
            if piece:
                try:
                    await emit_task_token(
                        group_id, reply_id, agent_id, "streaming", piece
                    )
                except Exception:
                    logger.exception(
                        "[worker %s] failed to emit task_token delta", agent_id
                    )
        if reasoning_delta:
            reasoning_parts.append(reasoning_delta)
            # 推推理链逐字增量（task-思考流式）：与协调者 _stream_coordinator_decision
            # 同款 emit_coordinator_reasoning，按 reply_id 归并。前端 coordReasoning[reply_id]
            # 实时累加 → 流式气泡的「思考过程」折叠区逐字流式 + 自动展开（思考结束自动收起，
            # 然后流式输出正文）。复用 coordinator_reasoning 通道而非新造 task_reasoning——单聊
            # worker 的可见正文已用同一 reply_id 归并进 coordStreaming[reply_id]（task_token 分支），
            # 思考与正文同 reply_id，前端同一流式气泡天然同时接收两者，零新增归并逻辑。
            # best-effort：emit 失败不阻断 brain 决策（WS 推送是 fire-and-forget）。
            try:
                await emit_coordinator_reasoning(
                    group_id, agent_id, reply_id, reasoning_delta
                )
            except Exception:
                logger.exception(
                    "[worker %s] failed to emit reasoning delta", agent_id
                )
        if usage is not None:
            final_tokens = usage
        if reasoning_usage is not None:
            final_reasoning_tokens = reasoning_usage
    raw_full = "".join(raw_parts)
    reasoning_text = "".join(reasoning_parts)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    model = str(config.get("model") or "")
    # usage 未到（provider 不回 usage 或中断）→ 退化为粗估 len//3（与协调者
    # _stream_coordinator_decision 的 live_tokens 启发式一致），保证状态行总有数。
    tokens = final_tokens if final_tokens else max(1, len(raw_full) // 3)
    reasoning_tokens = final_reasoning_tokens  # 0 for non-reasoning models
    return reply_id, raw_full, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text


async def node_brain_decide(state: WorkerState) -> dict:
    """Worker LLM three-state decision: chat/execute/ask (Rust handle_notify 389-422).

    流式采集（reply_id/usage/耗时/model/reasoning）+ 逐字推 task_token/reasoning 抽到
    ``_stream_brain_decision``（镜像 ``coordinator._stream_coordinator_decision``，单一
    职责）；本函数只管 prompt 构建 + 决策解析 + stats 装配 + 异常兜底。
    """
    # 上下文直接从 messages 表真源拉（带 sender 身份），不再用 self._memory——
    # 修两个 bug：(1) 时序（旧 append 在 ainvoke 之后，当前 incoming 没进 context）；
    # (2) 身份（旧 _build_context 把 peer 消息全渲染成「用户:」）。coordinator_id
    # 用来把协调者消息标成「协调者」而非裸 agent_id。
    context = await _build_context_from_db(
        state.get("group_id", ""), state.get("coordinator_id", "") or ""
    )
    display_msg = _format_display_msg(
        state.get("incoming_sender", ""), state.get("incoming_message", "")
    )
    prompt = build_brain_prompt(
        state.get("agent_role", ""),
        state.get("agent_name", ""),
        context,
        display_msg,
        state.get("system_prompt", ""),
    )
    config = get_llm_config()
    # system_prompt 作为独立 system 消息注入（空串时 LLM 以 brain prompt 内
    # 的 {role} 兜底人设作答）。单聊 agent 用自己的 system_prompt 主导行为，
    # 不再回「我可以调度团队成员来协助你」（那是 COORDINATOR_SYSTEM 的人设）。
    messages = [
        {"role": "system", "content": state.get("system_prompt", "") or ""},
        {"role": "user", "content": prompt},
    ]
    try:
        # 流式采集真实 usage + 耗时 + model + reasoning，逐字推 task_token/reasoning（best-effort）。
        # _stream_brain_decision 返七元组（与协调者 _stream_coordinator_decision 同形），
        # 本函数据此装配 _stream_stats，经 node_chat/ask 落盘到 agent_reply.data —— 定稿气泡
        # 据此渲染「model · Ns · ↓ N tokens（含 N 推理）」状态行（与协调者 node_chat 同形，
        # 前端 extractCoordStats 不区分来源）。原非流式 chat_completion 丢弃 usage，worker 回复
        # 无状态行（只有协调者有）。
        reply_id, raw, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text = (
            await _stream_brain_decision(
                config,
                messages,
                state.get("group_id", ""),
                state.get("agent_id", ""),
            )
        )
        decision = _parse_brain_decision(raw)
        stats: dict[str, Any] = {
            "reply_id": reply_id,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "model": model,
            "reasoning_tokens": reasoning_tokens,
        }
        if reasoning_text:
            stats["reasoning"] = reasoning_text
    except Exception as e:
        logger.warning("[worker %s] brain decision failed: %s", state.get("agent_name"), e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "llm_error",
        }
        stats = None
    return {"decision": decision, "_stream_stats": stats}


async def node_chat(state: WorkerState) -> dict:
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        state["decision"]["content"],
        data=state.get("_stream_stats"),
    )
    return {}


async def node_execute(state: WorkerState) -> dict:
    """Reply with a preview and push a task to self (Rust handle_notify 432-440).

    The pushed task wakes the engine's _handle_task -> _run_worker_task, which
    in M3 calls the mock CLI executor.

    回复是模板化 announce（``收到，我来 {preview}...``），非 brain 流式文本，
    token 数不匹配 → 不带 stats（与协调者 dispatch announce 排除同理）。真正的
    execute 执行统计在 _run_worker_task / create_react_agent 那条线（task_log）。
    """
    content = state["decision"]["content"]
    preview = content[:30]
    await _unified_reply(
        state["group_id"], state["agent_id"], f"收到，我来 {preview}..."
    )
    await push_task(
        state["group_id"],
        state["agent_id"],
        state["agent_id"],
        content,
        None,
    )
    return {}


async def node_ask(state: WorkerState) -> dict:
    await _unified_reply(
        state["group_id"],
        state["agent_id"],
        state["decision"]["content"],
        data=state.get("_stream_stats"),
    )
    return {}


def route_brain(state: WorkerState) -> str:
    return state.get("decision", {}).get("action", "chat")


def build_worker_graph():
    """Compile the worker StateGraph with a MemorySaver checkpointer."""
    g: StateGraph = StateGraph(WorkerState)
    g.add_node("brain", node_brain_decide)
    g.add_node("chat", node_chat)
    g.add_node("execute", node_execute)
    g.add_node("ask", node_ask)

    g.add_edge(START, "brain")
    g.add_conditional_edges(
        "brain",
        route_brain,
        {"chat": "chat", "execute": "execute", "ask": "ask"},
    )
    g.add_edge("chat", END)
    g.add_edge("execute", END)
    g.add_edge("ask", END)

    return g.compile(checkpointer=MemorySaver())


def _parse_brain_decision(raw: str) -> dict:
    """Parse the worker LLM JSON response into action/content/reasoning (Rust parse_brain_decision)."""
    v = extract_json(raw)
    if v is None:
        return {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "parse_failed",
        }
    action = str(v.get("action", "chat"))
    if action not in ("chat", "execute", "ask"):
        action = "chat"
    content = str(v.get("content", ""))
    reasoning = str(v.get("reasoning", ""))
    return {"action": action, "content": content, "reasoning": reasoning}
