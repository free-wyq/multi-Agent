"""Worker StateGraph — 4 nodes + conditional edge (Rust handle_notify worker branch).

Nodes: brain, chat, execute, ask. The brain node calls the worker LLM for a
three-state decision (chat/execute/ask). ``execute`` replies with a preview
and ``push_task`` to itself (the engine's _handle_task then runs the CLI via
``_run_worker_task``). The graph is compiled once with a MemorySaver
checkpointer and invoked per incoming notify by ``AgentEngine._handle_notify``.

Agent-as-node factory (``make_agent_node`` / ``build_agent_node``): a second,
parallel entry point that packages the same brain→chat/execute/ask decision
into ONE LangGraph node returning ``Command(goto=...)`` or ``Command(goto=END)``
— the building block for the per-group swarm graph (engine/group_graph.py, later
tasks). The resident ``build_worker_graph`` (4-node + conditional edge) stays
unchanged and remains the engine's live worker graph until the group-graph
migration swaps consumers over.
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from engine.agent_executor import _compose_system_prompt as _compose_skill_prompt
from engine.inbox import push_task
from engine.mention import find_mentions, resolve_mention
from engine.reply import persist_agent_reply
from engine.state import GroupState, WorkerState
from events import emit_coordinator_reasoning, emit_task_token
from llm.client import chat_completion_stream, get_llm_config
from llm.extract_json import extract_json
from llm.json_stream import ContentExtractor
from llm.prompts import TEAM_INTERACTION_SUFFIX, build_brain_prompt
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

# The per-group ``GroupRuntime`` whose cooperative stop event this node should
# consult at entry (task-17: route_entry + each agent node先查 ``is_stopped()``
# 命中即不发言直接返回 END，协作式停止). A contextvar (not a module global) for
# the same reason as ``_REPLY_CB``: the group graph runs ONE ainvoke per turn per
# group, but multiple groups' turns run concurrently as separate asyncio tasks
# (each a ``GroupRuntime.invoke_turn``), so a module global would let group A's
# ``request_stop`` bleed into group B's agent nodes. Each ``invoke_turn`` task
# copies the context at creation, so the runtime it set is the one its own agent
# nodes see. Mirrors the coordinator's ``_GRAPH_INSTANCE`` / ``_PENDING_PLAN_VIEW``
# contextvars (per-task copy, concurrent engines don't cross-talk). Default
# ``None`` (the resident worker graph path / a group graph invoked outside a
# ``GroupRuntime`` turn) → ``get_group_runtime()`` returns ``None`` + the
# stop-check guard is skipped (no runtime → no cooperative stop, the node runs as
# before — backward compatible with the resident engine).
_GROUP_RUNTIME: contextvars.ContextVar = contextvars.ContextVar(
    "group_runtime", default=None
)


def set_reply_callback(cb: Any) -> None:
    """Install the engine's unified reply callable for the duration of one invoke.

    Sets the callback in the *current task's* context (contextvars), so
    concurrent engine invokes (front/back brain running in parallel) each see
    their own cb — not a shared global that the last writer wins.
    """
    _REPLY_CB.set(cb)


def set_group_runtime(rt: Any) -> None:
    """Install the per-group ``GroupRuntime`` for the duration of one turn.

    Task-17: ``GroupRuntime.invoke_turn`` / ``resume_plan`` call this right
    before ``ainvoke`` so the group graph's ``route_entry`` + every agent node
    (``make_agent_node``) can consult ``rt.is_stopped()`` at entry (cooperative
    soft stop — on hit return ``Command(goto=END)`` instead of speaking).
    Sets it in the *current task's* context (contextvars), so concurrent group
    turns (each its own ``invoke_turn`` task) each see their own runtime.
    ``GroupRuntime.invoke_turn``'s ``finally`` clears it (``set_group_runtime
    (None)``) so the slot doesn't leak into the next turn (paired with the
    ``_REPLY_CB`` clear, mirroring the resident engine's per-invoke lifecycle).
    """
    _GROUP_RUNTIME.set(rt)


def get_group_runtime() -> Any:
    """Return the current turn's ``GroupRuntime`` (or ``None``).

    Called by ``make_agent_node`` / ``route_entry`` entry guards to decide
    whether to yield (``is_stopped()`` True → ``Command(goto=END)``). ``None``
    (no runtime installed — the resident worker graph path, or a group graph
    invoked outside a ``GroupRuntime`` turn) → the guard is skipped (no
    cooperative stop; the node runs as before). The resident engine path never
    installs a runtime, so the stop-check is a pure no-op there (backward
    compatible).
    """
    return _GROUP_RUNTIME.get()


async def _unified_reply(
    group_id: str, agent_id: str, content: str, data: dict[str, Any] | None = None
) -> None:
    """Persist an agent_reply + emit + mention route (Rust engine.reply).

    Persistence + emit delegated to ``persist_agent_reply`` (engine.reply, B10)
    so the agent_reply shape is a single source shared with the coordinator
    graph's reply and the registry's execute-path announce. Mention routing
    stays here, via the engine's reply callback (set per-invoke).

    ``data`` 写到持久化 message 上（存活重载/重连）。worker chat/ask 路径把
    brain 流式采集的 run-stats（``{elapsed_ms, tokens, model, reasoning_tokens,
    reasoning?}``）传进来，定稿气泡据此渲染「model · Ns · ↓ N tokens（含 N 推理）」
    状态行——与协调者 node_chat 落盘 ``_stream_stats`` 同形（前端 extractCoordStats
    不区分来源，按 data.elapsed_ms 是否存在判定渲染）。execute 路径回复是模板化
    announce（``收到，我来 {preview}...``），不匹配 brain token，不带 stats（传 None）。
    """
    await persist_agent_reply(group_id, agent_id, content, data)
    cb = _REPLY_CB.get()
    if cb is not None:
        await cb(content)


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
    # 命名口径（见 docs/naming-conventions.md §2.2）：reply_id 是裸 uuid hex（无 `task_`
    # 前缀），与 task_id（恒 `task_`+hex）靠前缀判别。worker 单聊回复走 task_token 通道，
    # 后端把 reply_id 塞进事件 task_id 槽，前端 useBusEvent.ts:430 按 `task_` 前缀分流到
    # coordStreaming[reply_id]——非碰撞，是有意的跨命名空间复用 + 前缀判别。
    reply_id = uuid.uuid4().hex
    # worker brain 的 LLM 输出是 strict JSON（``{"action","content","reasoning"}``），
    # 可见回复是 ``content`` 字段值——若把 raw_parts（含 JSON 骨架/action/reasoning）逐字
    # 推给前端，流式气泡会渲染出 ``{"action":"chat","content":"...`` 这种骨架噪声。故复用
    # ``llm.json_stream.ContentExtractor``（与协调者 ``_stream_coordinator_decision`` 同款）：
    # feed 每个 content_delta，take() 出「content 字段的解码增量」（跳过 JSON 骨架/转义解码/
    # 前导散文），只把真正的可见回复文本逐字推。
    extractor = ContentExtractor()
    start = time.monotonic()
    raw_parts: list[str] = []
    # reasoning_content 全文累积——落盘到 agent_reply.data.reasoning，定稿气泡的折叠区据此
    # 展开（流式期靠 coordinator_reasoning 事件，定稿后靠持久化文本）。
    reasoning_parts: list[str] = []
    final_tokens = 0
    final_reasoning_tokens = 0
    # running estimate of emitted reasoning chars → a coarse token estimate for
    # the live reasoning counter (reasoning_tokens only lands on the final chunk)
    # ——与协调者 live_reasoning_tokens 同款，作 final_reasoning_tokens 的 fallback 估值
    # （provider 不回 reasoning_usage 时退化，避免 reasoning 模型 stats 显示 0 推理）。
    live_reasoning_tokens = 0
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
            live_reasoning_tokens += max(1, len(reasoning_delta) // 3)
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
    # reasoning_tokens fallback：provider 不回 reasoning_usage 时退化为 live_reasoning_tokens
    # 粗估（与协调者 _stream_coordinator_decision 的 live_reasoning_tokens 兜底一致），避免
    # reasoning 模型只回 content usage 不回 reasoning_usage 时 stats 显示 0 推理（与实际有推理链
    # 不符）。非推理模型 reasoning_parts 空 → live_reasoning_tokens=0 → 仍如实显示 0。
    reasoning_tokens = (
        final_reasoning_tokens if final_reasoning_tokens else live_reasoning_tokens
    )
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
    await _unified_reply(state["group_id"], state["agent_id"], state["decision"]["content"], data=state.get("_stream_stats"))
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
    await _unified_reply(state["group_id"], state["agent_id"], f"收到，我来 {preview}...")
    await push_task(state["group_id"], state["agent_id"], state["agent_id"], content, None)
    return {}


async def node_ask(state: WorkerState) -> dict:
    await _unified_reply(state["group_id"], state["agent_id"], state["decision"]["content"], data=state.get("_stream_stats"))
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
    """Parse the worker LLM JSON response into action/content/reasoning (Rust parse_brain_decision).

    先走严格 ``extract_json``（balanced-brace + ``json.loads``）。LLM 偶发吐出
    非严格 JSON——``content`` 字段值里夹了未转义的双引号（如 ``"users"``）、
    输出被截断没闭合、或在 ``}`` 后拖了散文——``extract_json`` 此时返 ``None``，
    旧实现一律兜底成「抱歉，我这边有点卡壳」。但同一段 raw 在流式期已由
    ``ContentExtractor`` 增量解码出完整可见正文并逐字推给前端气泡（``ContentExtractor``
    只盯 ``content`` 字符串值、到尾引号即收，对骨架/截断/拖尾散文天然容忍）。结果是
    **流式气泡渲染了真实回复、定稿回复却是兜底道歉**——前后端不同源（vb3「流式拼接
    == 定稿回复」等式偶发 FAIL 的根因）。

    修复：``extract_json`` 失败时，用 ``ContentExtractor.extract_final(raw)`` 兜底
    恢复 ``content`` 字段值（与流式增量同源同机，逐字相等），只在连 ``content`` 都
    提不到（真无 JSON / 空响应）时才退回道歉文案。``action``/``reasoning`` 在恢复
    路径上取默认 ``chat``/``"parse_failed_recovered"``（标记走的是容错恢复而非严格解析，
    便于审计；content 已是真值可正常发言 + handoff 解析，不阻断对话）。
    """
    v = extract_json(raw)
    if v is None:
        # 容错恢复：用流式同款 ContentExtractor 取 content 字段值（chunk-invariant，
        # 与流式气泡逐字同源）。提取不到（真无 JSON / 空响应）才退回道歉。
        recovered = ContentExtractor().extract_final(raw)
        if not recovered:
            return {
                "action": "chat",
                "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
                "reasoning": "parse_failed",
            }
        return {
            "action": "chat",
            "content": recovered,
            "reasoning": "parse_failed_recovered",
        }
    action = str(v.get("action", "chat"))
    if action not in ("chat", "execute", "ask"):
        action = "chat"
    content = str(v.get("content", ""))
    reasoning = str(v.get("reasoning", ""))
    return {"action": action, "content": content, "reasoning": reasoning}


# Per-turn cap on how many handoffs a single user turn may chain (bounds the
# handoff chain length as an in-graph anti-loop backstop, complementing
# LangGraph's own ``recursion_limit``). Same default spirit as mention.py's
# ``_A2A_CAP`` but per-turn (reset each ``invoke_turn`` via GroupState.
# turn_count), not per-group cumulative — ``recent_speakers`` + ``turn_count``
# are the per-turn guard.
#
# Agent-as-node factory (group-graph migration · building block): packages the
# same brain→chat/execute/ask decision the resident ``build_worker_graph``
# runs across 4 nodes into ONE LangGraph node returning ``Command(goto=...)``
# or ``Command(goto=END)`` — the form the per-group swarm graph
# (engine/group_graph.py, later task) wires with handoff edges. Worker is
# still an LLM+LangGraph agent talking to an OpenAI-compatible endpoint
# directly (``chat_completion_stream``); it does NOT shell out to the Claude
# Code CLI (see memory ``agent-no-cli-decouple``).
AGENT_NODE_MAX_HANDOFFS = 8


async def _resolve_handoff_target(
    group_id: str,
    coordinator_id: str,
    sender_id: str,
    content: str,
) -> str | None:
    """Resolve the @mention in ``content`` to the next speaker's agent_id.

    Reuses ``mention.find_mentions`` + ``mention.resolve_mention`` (single
    source for @-token scanning + three-tier agent match), then applies the
    agent-as-node turn-local guards the resident graph enforced via
    ``route_mentions``'s 30s anti-loop + A2A cap:

      - skip self-mention (``@自己`` is a no-op handoff);
      - skip the coordinator as a handoff target — in the decentralized swarm
        path the coordinator is reached via ``route_entry`` for
        engineering/plan-confirm turns, NOT via a worker's @mention (returning
        the coordinator here would re-introduce the「协调者每轮插话」defect
        the group graph is built to eliminate; a worker that wants the Leader
        ends its turn with no @mention and the next user turn routes to the
        Leader).
      - first resolved mention wins (single next speaker — handoff is serial,
        one node runs at a time; ``route_user_message``'s first-mention-wins
        semantics preserved).

    Returns ``None`` when there is no resolvable next speaker → the node ends
    its turn (``Command(goto=END)``). Member/agent rows are read from the DB
    (``crud`` single source) so the resolution never goes stale vs the
    resident ``route_mentions`` path.
    """
    mentions = find_mentions(content)
    if not mentions:
        return None
    members = await crud.list_group_members_with_agent(group_id)
    agents = await crud.list_agents()
    for mention in mentions:
        if mention == sender_id:
            continue  # self-mention: no-op
        target_id = resolve_mention(members, mention, agents)
        if not target_id or target_id == sender_id:
            continue
        if coordinator_id and target_id == coordinator_id:
            # decentralized path: workers do not hand off back to the Leader
            # via @mention (route_entry owns Leader entry). Treat as no handoff.
            continue
        return target_id
    return None


def _build_agent_invoke_messages(
    system_prompt: str,
    agent_role: str,
    agent_name: str,
    context: str,
    display_msg: str,
) -> list[dict[str, str]]:
    """Build the LLM message list for an agent-node brain call.

    Mirrors ``node_brain_decide``'s message construction (system_prompt as an
    independent ``system`` message so the agent's own persona overrides the
    brain prompt's generic {role} fallback; the decision prompt as ``user``).
    Single source of this shape so the agent node and the resident worker
    graph can't drift.
    """
    return [
        {"role": "system", "content": system_prompt or ""},
        {"role": "user", "content": build_brain_prompt(
            agent_role, agent_name, context, display_msg, system_prompt,
        )},
    ]


async def make_agent_node(
    state: GroupState,
    *,
    agent_id: str,
    agent_name: str,
    agent_role: str,
    system_prompt: str,
    coordinator_id: str,
    mounted_skills: list[str] | None = None,
) -> Command:
    """Agent-as-node: one worker's brain→speak→handoff, returning a ``Command``.

    Collapses the resident worker graph's brain/chat/execute/ask four-node
    flow into a single node. The node:

      1. Builds context from the messages table (``_build_context_from_db``,
         same DB-true-source pull as ``node_brain_decide``) + the incoming
         message.
      2. Streams the brain LLM decision (``_stream_brain_decision`` — unchanged,
         still ``chat_completion_stream`` + ``emit_task_token`` /
         ``emit_coordinator_reasoning`` per-token WS, best-effort) and parses
         it (``_parse_brain_decision``).
      3. Speaks the reply (``_unified_reply`` — persist + emit + the engine's
         reply callback for @mention routing, single source shared with the
         resident graph). For ``execute`` it replies with the templated
         ``收到，我来 …`` announce + ``push_task`` to self (the engine's
         ``_handle_task`` then runs the agentic loop), exactly as
         ``node_execute`` does.
      4. Resolves the next speaker from the reply's @mention
         (``_resolve_handoff_target``). If a peer is mentioned →
         ``Command(goto="agent_<peer_id>", update=...)`` hands control to
         that agent node (LangGraph handoff: serial, one node at a time →
         kills the「顺序乱/抢序」defect). No @mention →
         ``Command(goto=END, update=...)`` ends the turn (话筒落地).

    **图内防连发守卫**（节点入口，先于 brain）：若 ``agent_id`` 已在 ``state["recent_speakers"]``
    里（本回合已发过言），直接 ``Command(goto=END)`` 不重复发言。handoff 串行只消除「两个
    节点同时跑」的抢序，但 LLM 仍可把话筒 @回已发言者形成 A→B→A→A 连发；本守卫把「同一
    agent 一回合不被驱动两次」做成图内硬约束（顺序乱根因①/②的图内兜底）。
    ``recent_speakers`` 由 ``append_list`` reducer 累加，跨 handoff 在 GroupState 单一真源。

    The ``update`` payload always carries the agent's appended ``AIMessage``
    (so ``GroupState.messages`` accumulates the turn's dialogue across
    handoffs via the ``add_messages`` reducer), bumps ``turn_count`` (in-graph
    handoff-chain-length backstop, capped by ``AGENT_NODE_MAX_HANDOFFS``),
    and appends ``agent_id`` to ``recent_speakers`` (per-turn「same agent not
    driven twice」guard, complementing handoff's natural serial-only-one-node).

    Identity (``agent_id``/``agent_name``/``agent_role``/``system_prompt``) is
    closed-over by ``build_agent_node`` so the compiled node knows who it is
    speaking as without re-deriving from ``current_speaker``.

    The worker does NOT call the Claude Code CLI — it is a framework-internal
    LLM+LangGraph agent hitting the OpenAI-compatible endpoint directly via
    ``chat_completion_stream`` (memory ``agent-no-cli-decouple`` /
    ``engines-use-frameworks-not-handrolled``). ``execute`` still pushes a task
    to itself; the agentic loop (``_run_worker_task``) is the only CLI-adjacent
    path and is owned by the engine, not this node.
    """
    group_id = state.get("group_id", "")

    # ── 图内防连发守卫（顺序乱根因①/②）────────────────────────
    # handoff 天然串行只一节点在跑，但「同一 agent 一回合被驱动两次」仍可能发生：
    # ① LLM 把话筒 @回刚发过言的人（"@前端 接着说" 而前端刚说完）；
    # ② route_entry/agent 节点解析到已在本回合发言的 agent 作为 next_speaker。
    # handoff 串行消除了「两个节点同时跑」的抢序，但单线程内 LLM 仍可把控制权交回
    # 已发言者形成 A→B→A→A 的连发。本守卫在节点入口查 recent_speakers：若本 agent
    # 已在本回合发过言，不再发言（不再调 brain / 不再 _unified_reply），直接 END
    # 结束回合（话筒落地），把「同一 agent 一回合不被驱动两次」做成图内硬约束。
    #
    # 守卫位置（节点入口，先于 brain 调用）：避免一次无谓 LLM 调用 + 持久化重复发言。
    # recent_speakers 由 append_list reducer 累加，跨 handoff 在 GroupState 单一真源，
    # checkpointer 跨 invoke_turn 不会串台（invoke_turn 注入 recent_speakers=[] 重置）。
    already_spoke = agent_id in (state.get("recent_speakers") or [])
    if already_spoke:
        logger.debug(
            "[worker %s] agent-node 防连发守卫命中：本回合已发言（recent_speakers=%s），"
            "不重复发言，回合 END",
            agent_name, state.get("recent_speakers"),
        )
        return Command(goto=END, update={"current_speaker": agent_id})

    # ── 协作式停止守卫（StopSignal·task-17）────────────────
    # ``GroupRuntime.request_stop()``（用户喊「停/stop/中断」）只 set 一个
    # ``asyncio.Event``（软停·不强切）。本节点入口查 ``is_stopped()``：命中即不发言
    # （不调 brain / 不 _unified_reply），直接 ``Command(goto=END)`` 结束回合——当前
    # 发言者已把当前 step 跑完（流式话说完），本节点是「下一个」发言者，命中 stop
    # 即话筒落地，不 mid-stream 强切、不留半截消息。这是双层停止的软停层；硬停层
    # ``cancel_turn``（先 set 再 task.cancel）由 UI 停止按钮走，与本守卫正交。
    #
    # runtime 经 ``get_group_runtime()`` 从 contextvar 取（``GroupRuntime.invoke_turn``
    # 在 ainvoke 前 set，finally 清）。``None``（驻留 worker 图 / 群图在 GroupRuntime
    # 之外被 invoke）→ 无协作停止信号，守卫跳过（向后兼容驻留引擎，其停止走
    # ``AgentEngine.request_cancel`` 强切路径，不经此 contextvar）。
    rt = get_group_runtime()
    if rt is not None and rt.is_stopped():
        logger.debug(
            "[worker %s] agent-node 协作式停止守卫命中：stop_event 已 set（"
            "用户喊停），不发言，回合 END（当前发言者已说完，不 mid-stream 强切）",
            agent_name,
        )
        return Command(goto=END, update={"current_speaker": agent_id})

    # 1. context + display message (DB true source, same as node_brain_decide).
    context = await _build_context_from_db(group_id, coordinator_id)
    display_msg = _format_display_msg(
        state.get("incoming_sender", ""), state.get("incoming_message", "")
    )

    # 2. stream brain decision (per-token WS emit best-effort) + parse.
    config = get_llm_config()
    # group-chat members get the team-interaction suffix appended to their
    # persona (same as the resident worker graph's ``sys_for_invoke`` in
    # registry.py:729-733 — single source via TEAM_INTERACTION_SUFFIX).
    sys_for_invoke = system_prompt or ""
    if coordinator_id:  # group chat (has a Leader) → append team-interaction
        sys_for_invoke = (system_prompt or "") + "\n\n" + TEAM_INTERACTION_SUFFIX
    # ── 技能注入（PL-06 · 修 handoff 断层）────────────────────────
    # handoff 迁移后群聊路径 worker 走 ``make_agent_node``，不像驻留引擎的
    # ``agent_executor.execute_agent_task`` 那样注入挂载技能 → 群聊 handoff 路径
    # 发言时 worker 不带技能知识（断层）。这里镜像 agent_executor 的注入方式：
    # 从 DB 解析 ``mounted_skills`` id 列表 → ``crud.resolve_skill_contents`` 取
    # 全文 → ``_compose_skill_prompt``（即 agent_executor._compose_system_prompt，
    # 单一真源·加「## 已挂载技能」头 + 编号技能块）拼进 ``sys_for_invoke``。
    # 本任务只做全文注入（阶段二改渐进式披露：manifest 常驻 + 全文按需 load）。
    # mounted_skills 优先用闭包绑的（build_agent_node 编译期从 DB 拉的 agent 定义，
    # 与 system_prompt 同款 staleness 窗口）；为 None 时运行时从 DB 兜底拉一次
    # （防御性·让 make_agent_node 单测直接构造节点也能命中技能注入）。
    skill_ids = list(mounted_skills) if mounted_skills else []
    if not skill_ids:
        try:
            agent_def = await crud.get_agent(agent_id)
        except Exception:  # noqa: BLE001
            agent_def = None
        if agent_def is not None:
            skill_ids = list(getattr(agent_def, "mounted_skills", None) or [])
    if skill_ids:
        try:
            skill_contents = await crud.resolve_skill_contents(skill_ids)
        except Exception:  # noqa: BLE001
            skill_contents = []
            logger.debug(
                "[worker %s] resolve_skill_contents failed (skills dropped "
                "this turn): mounted=%s", agent_name, skill_ids,
            )
        if skill_contents:
            sys_for_invoke = _compose_skill_prompt(sys_for_invoke, skill_contents)
    messages = _build_agent_invoke_messages(
        sys_for_invoke, agent_role, agent_name, context, display_msg,
    )
    try:
        reply_id, raw, tokens, elapsed_ms, model, reasoning_tokens, reasoning_text = (
            await _stream_brain_decision(config, messages, group_id, agent_id)
        )
        decision = _parse_brain_decision(raw)
        stats: dict[str, Any] | None = {
            "reply_id": reply_id,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "model": model,
            "reasoning_tokens": reasoning_tokens,
        }
        if reasoning_text:
            stats["reasoning"] = reasoning_text
        reached_failure = False
    except Exception as e:
        logger.warning("[worker %s] agent-node brain failed: %s", agent_name, e)
        decision = {
            "action": "chat",
            "content": "抱歉，我这边有点卡壳，能再说一遍吗？",
            "reasoning": "llm_error",
        }
        stats = None
        reached_failure = True
        reply_id = ""  # unbound on failure; set so the downstream msg_id fallback trips

    content = str(decision.get("content", ""))
    action = decision.get("action", "chat")
    # reply_id is only meaningful for chat/ask (the streamed brain text that
    # the persisted bubble renders). execute's ack is a templated announce with
    # no matching brain token stream, so it gets a synthetic id (avoids the
    # AIMessage id being unset / colliding). On the LLM-failure branch reply_id
    # is unbound — fall back to a fresh uuid so the AIMessage id is always set.
    msg_id = reply_id if action in ("chat", "ask") and not reached_failure else f"exe_{uuid.uuid4().hex}"

    # 3. speak (persist + emit + reply-callback @mention routing, single source).
    if action == "execute":
        # templated announce + push_task to self (engine runs the agentic loop).
        preview = content[:30]
        await _unified_reply(group_id, agent_id, f"收到，我来 {preview}...")
        await push_task(group_id, agent_id, agent_id, content, None)
        # execute ack ends the turn (no @mention handoff — the work continues
        # out-of-band via the task, not via the group graph).
        next_speaker: str | None = None
    else:
        # chat / ask — the streamed reply IS the persisted content; carry stats.
        await _unified_reply(group_id, agent_id, content, data=stats)
        # 4. resolve next speaker from the reply's @mention.
        next_speaker = await _resolve_handoff_target(
            group_id, coordinator_id, agent_id, content,
        )

    # 5. decide turn end vs handoff. Cap the handoff chain length as an
    # in-graph anti-loop backstop (recent_speakers+turn_count are the per-turn
    # guards; this is the hard ceiling so a runaway @mention loop can't burn
    # the whole recursion budget before LangGraph's own limit trips).
    turn_count = (state.get("turn_count") or 0) + 1
    recent_speakers = [agent_id]  # appended via reducer to the running list
    reached_cap = turn_count >= AGENT_NODE_MAX_HANDOFFS
    if reached_cap:
        logger.debug(
            "[worker %s] agent-node turn_count=%d reached cap=%d, end turn",
            agent_name, turn_count, AGENT_NODE_MAX_HANDOFFS,
        )
        next_speaker = None

    update: dict[str, Any] = {
        "messages": [AIMessage(content=content, name=agent_name, id=msg_id)],
        "turn_count": turn_count,
        "recent_speakers": recent_speakers,
        "current_speaker": agent_id,
    }
    # The memory reducer appends; emit nothing if this turn produced no memo.
    # (Kept minimal: agent nodes don't push memory in the first cut; the
    # field exists on GroupState for future per-turn memos without a schema
    # change.)

    if next_speaker is None:
        return Command(goto=END, update=update)
    # handoff to the peer agent node. The group graph registers agent nodes
    # under the key ``agent_<agent_id>`` (see build_group_graph, later task),
    # so the goto target is that node name — NOT the agent_id directly (avoids
    # collisions with coordinator sub-node names like "classify"/"llm_decide").
    # NOTE: LangGraph forbids ':' and '|' in node names (reserved), so the
    # convention is ``agent_<id>`` (underscore separator), not ``agent:<id>``.
    update["current_speaker"] = next_speaker
    return Command(goto=f"agent_{next_speaker}", update=update)


def build_agent_node(
    agent_id: str,
    agent_name: str,
    agent_role: str,
    system_prompt: str,
    coordinator_id: str,
    mounted_skills: list[str] | None = None,
):
    """Bind an agent's identity into a zero-arg LangGraph node function.

    ``StateGraph.add_node`` takes ``(name, fn)`` where ``fn(state) -> dict|Command``.
    This factory closes over the agent's identity (id/name/role/system_prompt/
    the group's coordinator_id / mounted_skills) so the compiled node knows who it
    is speaking as — mirroring how ``AgentEngine`` caches ``self.agent_id`` /
    ``self.name`` / ``self.role`` / ``self.system_prompt`` / ``self.coordinator_id``
    on the resident worker graph. ``mounted_skills`` is closure-bound at build
    time (PL-06 group-graph skill injection · handoff 断层修复) — same staleness
    window as ``system_prompt``: a skill mount/unmount after compile needs a
    graph recompile (reload refreshes it).

    Returns a ``functools.partial`` over ``make_agent_node`` — a callable that
    LangGraph accepts as a node (it calls ``node(state)`` positionally, and
    ``partial`` injects the bound identity kwargs). Identity is captured at
    build time (group-graph compilation), so a later agent rename requires a
    graph recompile — same staleness window as the resident engine (which
    caches ``self.name`` at ``__init__``; reload refreshes it).
    """
    import functools

    node = functools.partial(
        make_agent_node,
        agent_id=agent_id,
        agent_name=agent_name,
        agent_role=agent_role,
        system_prompt=system_prompt,
        coordinator_id=coordinator_id,
        mounted_skills=mounted_skills,
    )
    # name it for readability in LangGraph traces (not used for routing —
    # the group graph registers the node under its own ``agent_<id>`` key).
    try:
        node.__name__ = f"agent_node_{agent_id}"  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # functools.partial exposes __name__ on Py3.10+; if not, skip (cosmetic).
        pass
    return node
