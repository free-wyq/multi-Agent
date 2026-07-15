"""Agentic execution loop — LangGraph ``create_react_agent`` + ``astream_events``.

Replaces the hand-rolled ReAct ``for`` loop with LangGraph's factory-built
agent graph (``langgraph.prebuilt.create_react_agent``), so the framework —
not our code — owns the model→tool→model iteration. We only subscribe to the
graph's event stream (``astream_events(version="v2")``) and project events onto
the ``on_log`` callback so the frontend ``task_log`` WS stream keeps working.

Why ``create_react_agent`` and not ``langchain.agents.create_agent``:
  ``create_agent`` (the newer API) calls the model via ``model_.ainvoke()``,
  which is non-streaming — ``on_chat_model_stream`` never fires at the graph
  layer, so per-token streaming (PL-08) is impossible. ``create_react_agent``
  uses the model's streaming path, so ``on_chat_model_stream`` delivers every
  token delta. It is the framework-provided streaming-capable agent factory
  (still part of LangGraph, just re-exported from ``langgraph.prebuilt``; the
  ``langchain.agents.create_agent`` re-export is the non-streaming successor).

PL-08: ``on_chat_model_stream`` chunks are forwarded as ``on_log("token", ...)``
deltas for live per-token rendering — the frontend can show thinking/answers
as the model generates them, instead of waiting for ``on_chain_end|model`` to
deliver the complete text. The complete text is *still* extracted on
``on_chain_end|model`` (via the ``output`` message list) as task_think/task_answer,
so existing consumers are unaffected (additive, non-breaking).

Contracts preserved (agent_executor / registry depend on these):
- ``run_agent_loop(...) -> {"success", "exit_code", "output"}``
- ``set_extra_tools(tools: list) -> None``
- ``DEFAULT_MAX_TURNS = 15``
- ``on_log`` kinds: log / tool_start / tool_end / think / answer / token
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from config import get_config
from engine.tools import tools_for_group

logger = logging.getLogger("multi-agent.agent_loop")

DEFAULT_MAX_TURNS = 15

# extra tools beyond the framework-internal set (MCP, injected per-run)
_EXTRA_TOOLS: list = []

_TOOL_SYSTEM_SUFFIX = """

You have access to the following tools for operating on files and running
commands inside your workspace:
- read_file(path): read a file (truncated to 8KB)
- write_file(path, content): create or overwrite a file
- edit_file(path, old_text, new_text): precise string replacement in a file
- list_dir(path="."): list directory entries
- run_command(command, timeout=30): run a shell command in the workspace

When you need to create or modify files, call the appropriate tool directly.
Work step by step: read existing files if needed, then write/edit. When the
task is done, reply with a concise text summary (no tool call).
"""


def set_extra_tools(tools: list) -> None:
    """Inject additional tools (MCP) for the next run (PRD PL-07).

    Set by the executor before calling ``run_agent_loop``. Cleared after the
    loop so concurrent agent runs on different groups don't bleed tool sets.
    """
    global _EXTRA_TOOLS
    _EXTRA_TOOLS = list(tools)


def _format_tool_names(tools: list) -> str:
    """Render tool names for the system prompt."""
    if not tools:
        return ""
    return ", ".join(t.name for t in tools)


def _summarize_args(args: Any) -> str:
    """Render tool-call args into a short summary string for logging."""
    try:
        if isinstance(args, dict):
            parts = []
            for k, v in args.items():
                sv = str(v)
                if len(sv) > 60:
                    sv = sv[:60] + "..."
                parts.append(f"{k}={sv}")
            return ", ".join(parts)
        return str(args)[:80]
    except Exception:
        return str(args)[:80]


def _extract_ai_content(output: Any) -> str:
    """Pull the text content from an on_chain_end|model output.

    ``create_react_agent`` emits the model node's output as a state-diff dict
    (``{"messages": [AIMessage, ...]}``) on ``on_chain_end``; older shapes
    (list of Command, bare AIMessage) are handled too for robustness. We want
    the last AIMessage's ``content`` string (the model's text reply).
    """
    # state-diff dict: {"messages": [...]}
    if isinstance(output, dict):
        msgs = output.get("messages", [])
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and m.content:
                return m.content if isinstance(m.content, str) else str(m.content)
        return ""
    if not isinstance(output, list):
        output = [output]
    for item in reversed(output):
        upd = getattr(item, "update", None)
        if isinstance(upd, dict):
            msgs = upd.get("messages", [])
            for m in reversed(msgs):
                if isinstance(m, AIMessage) and m.content:
                    return m.content if isinstance(m.content, str) else str(m.content)
        # bare AIMessage in the list
        if isinstance(item, AIMessage) and item.content:
            return item.content if isinstance(item.content, str) else str(item.content)
    return ""


def _extract_tool_calls(output: Any) -> list:
    """Extract tool_calls from the on_chain_end|model output."""
    msgs: list = []
    if isinstance(output, dict):
        msgs = output.get("messages", [])
    else:
        if not isinstance(output, list):
            output = [output]
        for item in output:
            upd = getattr(item, "update", None)
            if isinstance(upd, dict):
                msgs.extend(upd.get("messages", []))
            if isinstance(item, AIMessage):
                msgs.append(item)
    calls: list = []
    for m in msgs:
        if isinstance(m, AIMessage):
            tc = getattr(m, "tool_calls", None)
            if tc:
                calls.extend(tc)
    return calls


async def run_agent_loop(
    group_id: str,
    agent_id: str,
    agent_name: str,
    task_content: str,
    task_id: str,
    on_log: Callable[[str, str, dict | None], Awaitable[None]] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    system_prompt: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Run the agentic loop via LangGraph ``create_react_agent`` + ``astream_events``.

    Returns ``{"success": bool, "exit_code": int, "output": str}``.
    """
    cfg = get_config()
    model_name = agent_model or cfg["model"]
    tools = tools_for_group(group_id)
    mcp_tools = list(_EXTRA_TOOLS)
    tools = tools + mcp_tools
    # ── build the agent graph (factory owns the ReAct loop) ──
    # ChatOpenAI connection-level kwargs: only pass non-default values so the
    # framework's own defaults apply when the provider hasn't configured them
    # (passing max_retries=0 would disable retries; passing timeout=None would
    # wait forever — so omit rather than override with a falsy placeholder).
    # cfg carries the 13-key active-provider cache (see config.set_active_cache).
    try:
        chat_kwargs: dict[str, Any] = {
            "model": model_name,
            "base_url": cfg["base_url"],
            "api_key": cfg["api_key"],
            "temperature": cfg["temperature"],
        }
        # max_tokens: only pass when explicitly configured (>0); ChatOpenAI's
        # default (None = provider max) is preferable to a low cap.
        max_tokens = cfg.get("max_tokens")
        if max_tokens and int(max_tokens) > 0:
            chat_kwargs["max_tokens"] = int(max_tokens)
        # max_retries: provider-tunable retry count (default 2 in cache). Only
        # forward when set; langchain's own default applies otherwise.
        max_retries = cfg.get("max_retries")
        if max_retries is not None:
            chat_kwargs["max_retries"] = int(max_retries)
        # timeout: per-request wall-clock (default 120s). Forward as float;
        # langchain accepts httpx-style timeout.
        request_timeout = cfg.get("request_timeout")
        if request_timeout and float(request_timeout) > 0:
            chat_kwargs["timeout"] = float(request_timeout)
        # organization: OpenAI org header (some compatible endpoints use it).
        # Empty string = not configured → omit (don't send empty header).
        organization = (cfg.get("organization") or "").strip()
        if organization:
            chat_kwargs["organization"] = organization
        # default_headers: merge extra_headers (provider-configured custom
        # headers like X-Org-Id). None/empty → omit. Non-dict → skip defensively.
        extra_headers = cfg.get("extra_headers")
        if isinstance(extra_headers, dict) and extra_headers:
            chat_kwargs["default_headers"] = dict(extra_headers)
        # proxy: langchain ChatOpenAI has no direct proxy kwarg, but httpx
        # picks up HTTP_PROXY/HTTPS_PROXY env vars. Provider-level proxy is
        # honored by chat_completion/chat_completion_stream (direct httpx);
        # the langchain path relies on env or a custom http_client (future).
        model = ChatOpenAI(**chat_kwargs)
    except Exception as exc:
        logger.exception("[agent_loop %s] failed to init model", agent_name)
        if on_log:
            await on_log("log", f"[错误] 模型初始化失败: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"model init error: {exc}"}

    sys_content = (system_prompt or "").strip()
    if sys_content:
        sys_content += "\n"
    sys_content += _TOOL_SYSTEM_SUFFIX
    if mcp_tools:
        sys_content += (
            "\nYou also have access to these external (MCP) tools: "
            + _format_tool_names(mcp_tools)
            + ". Use them when the task requires an external capability.\n"
        )

    try:
        # create_react_agent: framework-provided streaming-capable agent factory.
        # ``prompt=str`` prepends a SystemMessage to every model call (uniform
        # system prompt + tool suffix across the conversation). ``checkpointer``
        # enables recursion-limit recovery via aget_state.
        agent = create_react_agent(
            model,
            tools,
            prompt=sys_content,
            checkpointer=MemorySaver(),
        )
    except Exception as exc:
        logger.exception("[agent_loop %s] create_react_agent failed", agent_name)
        if on_log:
            await on_log("log", f"[错误] 智能体图构建失败: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"create_agent error: {exc}"}

    # recursion_limit: each "model call + tool exec" ≈ 2 super-steps
    recursion_limit = max_turns * 2 + 4

    # unique thread_id per invocation so MemorySaver never collides.
    # 命名口径（见 docs/naming-conventions.md §2.1/§2.3）：有 task_id 时复用作 thread_id
    # （task-scoped 检查点，同 task 多轮 tool 调用共享状态）；否则用新鲜 uuid4（per-exec
    # 独立检查点，不与历史 task 串话）。与驻留引擎图的 {group}:{agent} 稳定键是两型 thread_id。
    thread_id = task_id or str(uuid4())
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }

    if on_log:
        await on_log(
            "log",
            f"[开始] 智能体 {agent_name} 开始执行任务（max_turns={max_turns}, recursion_limit={recursion_limit}）",
            None,
        )

    output = ""
    last_tool_output = ""

    try:
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=task_content)]},
            config=config,
            version="v2",
        ):
            etype = event["event"]
            name = event.get("name", "")
            data = event.get("data", {})

            if etype == "on_tool_start":
                args_input = data.get("input", {})
                summary = _summarize_args(args_input)
                if on_log:
                    await on_log(
                        "tool_start",
                        f"[工具] {name}({summary})",
                        {"name": name, "args": args_input},
                    )

            elif etype == "on_tool_end":
                raw_output = data.get("output", "")
                if hasattr(raw_output, "content"):
                    out_str = str(raw_output.content)
                else:
                    out_str = str(raw_output)
                last_tool_output = out_str
                if on_log:
                    await on_log(
                        "tool_end",
                        f"[工具] {name} → {out_str[:200]}",
                        {"name": name, "output": out_str[:2000]},
                    )

            elif etype == "on_chat_model_stream":
                # PL-08: per-token streaming. Each model delta is forwarded as
                # a "token" log kind so the frontend can render thinking/answers
                # live (逐字流式) instead of waiting for on_chain_end|model to
                # deliver the complete text. The complete text is *still* emitted
                # on on_chain_end|model (task_think/task_answer), so existing
                # consumers are unaffected — this is additive, non-breaking.
                #
                # We can't yet tell mid-stream whether this model call is
                # reasoning-before-a-tool (thinking) or the final answer, so
                # all deltas carry phase="streaming"; the follow-up
                # on_chain_end|model event (phase=thinking|final) finalizes the
                # label. Empty deltas (e.g. pure tool_call chunks) are skipped
                # to avoid no-op emits.
                chunk = data.get("chunk")
                delta = ""
                if chunk is not None:
                    c = getattr(chunk, "content", None)
                    if isinstance(c, str):
                        delta = c
                if delta and on_log:
                    await on_log("token", delta, {"phase": "streaming"})

            elif etype == "on_chat_model_end":
                # Model call finished — ``create_react_agent`` delivers the raw
                # ``AIMessage`` (not a state-diff wrapper) on this event, with
                # ``.content`` (text) and ``.tool_calls`` populated. This fires
                # exactly once per model call, whether or not tools follow, so
                # it's the reliable place to extract the complete text answer
                # (on_chain_end|model is noisy and may omit content).
                msg = data.get("output")
                if isinstance(msg, AIMessage):
                    ai_content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    if ai_content:
                        output = ai_content
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            # intermediate reasoning before a tool call
                            if on_log:
                                await on_log(
                                    "think",
                                    ai_content,
                                    {"phase": "thinking"},
                                )
                        else:
                            # final text answer
                            if on_log:
                                await on_log(
                                    "answer",
                                    output[:200],
                                    {"phase": "final"},
                                )

            elif etype == "on_chain_end" and name == "model":
                # Fallback extraction for graphs that emit a state-diff here
                # instead of on_chat_model_end (older factory shapes). Best-effort,
                # non-breaking — only acts if on_chat_model_end didn't already set
                # content for this turn.
                model_output = data.get("output")
                ai_content = _extract_ai_content(model_output)
                if ai_content and not output:
                    output = ai_content
                    tool_calls = _extract_tool_calls(model_output)
                    if tool_calls:
                        if on_log:
                            await on_log(
                                "think",
                                ai_content,
                                {"phase": "thinking"},
                            )
                    else:
                        if on_log:
                            await on_log(
                                "answer",
                                output[:200],
                                {"phase": "final"},
                            )

    except GraphRecursionError:
        logger.warning(
            "[agent_loop %s] recursion limit %d reached", agent_name, recursion_limit
        )
        if on_log:
            await on_log(
                "log",
                f"[停止] 达到最大轮次 {max_turns}（recursion_limit={recursion_limit}）",
                None,
            )
        # Try to recover last known output from checkpoint state
        if not output:
            try:
                state = await agent.aget_state(config)
                msgs = state.values.get("messages", [])
                for m in reversed(msgs):
                    if isinstance(m, AIMessage) and m.content:
                        output = (
                            m.content if isinstance(m.content, str) else str(m.content)
                        )
                        break
            except Exception:
                # state-read recovery best-effort: we already fell through to
                # the last-tool-output fallback below; a checkpoint read failure
                # here is not fatal. Logged at debug (not exception): this is a
                # secondary recovery path inside an already-handled
                # GraphRecursionError, and exception-level logging would duplicate
                # the recursion warning above (B31 错误处理重巡航——原 `pass`
                # 静默吞没，checkpoint 读取失败不可观测).
                logger.debug(
                    "[agent_loop %s] checkpoint state read failed during "
                    "recursion-limit recovery", agent_name, exc_info=True,
                )
        if not output:
            output = last_tool_output or "(达到最大轮次，无最终输出)"
        return {"success": True, "exit_code": 0, "output": output[:2000]}

    except Exception as exc:
        logger.exception("[agent_loop %s] execution error", agent_name)
        if on_log:
            await on_log("log", f"[错误] 执行异常: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"execution error: {exc}"}

    if not output:
        # Stream ended without a final text answer; fall back to last tool output
        output = last_tool_output or "(无输出)"
        if on_log:
            await on_log("log", f"[完成] {output[:200]}", None)

    return {"success": True, "exit_code": 0, "output": output[:2000]}


# ── skill run loop (Claude Skills 化 · 阶段四·task38) ────────────────────
# ``run_agent_loop`` is wired for the resident execute path: it hardcodes the
# group-workspace tools (``tools_for_group``) + the per-run ``_EXTRA_TOOLS``
# (MCP / skill tools set via ``set_extra_tools``), and emits via ``on_log``
# onto a group's WS bus (``emit_task_*``). The skill **run endpoint** needs a
# different topology: a transient agent with only the skill's controlled tools
# (``file_read``/``file_write``/``bash_run`` bound to that skill's sandbox),
# no group, no group WS bus — and it streams its events onto an SSE response
# (task38) instead of a group WS channel. Rather than overload ``run_agent_loop``
# with a sprawl of branches (group vs skill, WS vs SSE, group-tools vs
# skill-tools), the skill run path is its own loop here, reusing the same
# ``create_react_agent`` factory + ``astream_events`` projection so the
# model→tool→model iteration stays framework-owned (memory
# ``engines-use-frameworks-not-handrolled`` — no hand-rolled dispatcher).

_SKILL_RUN_SYSTEM_SUFFIX = """

You are running inside your skill's sandbox workspace. You have these tools:
- file_read(path): read a file in the sandbox (truncated 8KB)
- file_write(path, content): create or overwrite a file (products → output/)
- bash_run(command, timeout=30): run a denylist-gated shell command in the sandbox

Follow the skill's instructions. Produce deliverables under the output/ dir.
When done, reply with a concise summary of what you produced.
"""


async def run_skill_loop(
    *,
    skill_id: str,
    skill_name: str,
    skill_content: str,
    prompt: str,
    tools: list,
    on_event: Callable[[str, str, dict | None], Awaitable[None]] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    agent_model: str = "",
) -> dict[str, Any]:
    """Run a transient skill agent: ``create_react_agent`` + ``astream_events``.

    Same framework-owned ReAct loop as ``run_agent_loop`` but decoupled from any
    group: ``tools`` are the skill's controlled tools (bound to that skill's
    sandbox), and ``on_event`` is an SSE-projecting callback (task38), not the
    group-bus ``emit_task_*``. Events are the same kinds (token / tool_start /
    tool_end / think / answer / log) so the frontend CodeBuddy bubble renderer
    (task39) reuses one code path.

    Returns ``{"success": bool, "exit_code": int, "output": str}`` — same shape
    as ``run_agent_loop`` so callers share handling.
    """
    cfg = get_config()
    model_name = agent_model or cfg["model"]

    try:
        chat_kwargs: dict[str, Any] = {
            "model": model_name,
            "base_url": cfg["base_url"],
            "api_key": cfg["api_key"],
            "temperature": cfg["temperature"],
        }
        max_tokens = cfg.get("max_tokens")
        if max_tokens and int(max_tokens) > 0:
            chat_kwargs["max_tokens"] = int(max_tokens)
        max_retries = cfg.get("max_retries")
        if max_retries is not None:
            chat_kwargs["max_retries"] = int(max_retries)
        request_timeout = cfg.get("request_timeout")
        if request_timeout and float(request_timeout) > 0:
            chat_kwargs["timeout"] = float(request_timeout)
        organization = (cfg.get("organization") or "").strip()
        if organization:
            chat_kwargs["organization"] = organization
        extra_headers = cfg.get("extra_headers")
        if isinstance(extra_headers, dict) and extra_headers:
            chat_kwargs["default_headers"] = dict(extra_headers)
        model = ChatOpenAI(**chat_kwargs)
    except Exception as exc:
        logger.exception("[skill_loop %s] failed to init model", skill_name)
        if on_event:
            await on_event("log", f"[错误] 模型初始化失败: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"model init error: {exc}"}

    sys_content = ((skill_content or "")).strip()
    if sys_content:
        sys_content += "\n"
    sys_content += _SKILL_RUN_SYSTEM_SUFFIX

    try:
        agent = create_react_agent(
            model,
            tools,
            prompt=sys_content,
            checkpointer=MemorySaver(),
        )
    except Exception as exc:
        logger.exception("[skill_loop %s] create_react_agent failed", skill_name)
        if on_event:
            await on_event("log", f"[错误] 智能体图构建失败: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"create_agent error: {exc}"}

    recursion_limit = max_turns * 2 + 4
    thread_id = f"skill_run_{skill_id}_{uuid4().hex}"
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }

    if on_event:
        await on_event(
            "log",
            f"[开始] 技能 {skill_name} 运行（max_turns={max_turns}, recursion_limit={recursion_limit}）",
            None,
        )

    output = ""
    last_tool_output = ""

    async def _emit(kind: str, content: str, data: dict | None = None) -> None:
        if on_event is not None:
            await on_event(kind, content, data)

    try:
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=prompt)]},
            config=config,
            version="v2",
        ):
            etype = event["event"]
            name = event.get("name", "")
            data = event.get("data", {})

            if etype == "on_tool_start":
                args_input = data.get("input", {})
                await _emit(
                    "tool_start",
                    f"[工具] {name}({_summarize_args(args_input)})",
                    {"name": name, "args": args_input},
                )
            elif etype == "on_tool_end":
                raw_output = data.get("output", "")
                if hasattr(raw_output, "content"):
                    out_str = str(raw_output.content)
                else:
                    out_str = str(raw_output)
                last_tool_output = out_str
                await _emit(
                    "tool_end",
                    f"[工具] {name} → {out_str[:200]}",
                    {"name": name, "output": out_str[:2000]},
                )
            elif etype == "on_chat_model_stream":
                chunk = data.get("chunk")
                delta = ""
                if chunk is not None:
                    c = getattr(chunk, "content", None)
                    if isinstance(c, str):
                        delta = c
                if delta:
                    await _emit("token", delta, {"phase": "streaming"})
            elif etype == "on_chat_model_end":
                msg = data.get("output")
                if isinstance(msg, AIMessage):
                    ai_content = (
                        msg.content if isinstance(msg.content, str) else str(msg.content)
                    )
                    if ai_content:
                        output = ai_content
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            await _emit("think", ai_content, {"phase": "thinking"})
                        else:
                            await _emit("answer", output[:200], {"phase": "final"})
            elif etype == "on_chain_end" and name == "model":
                model_output = data.get("output")
                ai_content = _extract_ai_content(model_output)
                if ai_content and not output:
                    output = ai_content
                    tool_calls = _extract_tool_calls(model_output)
                    if tool_calls:
                        await _emit("think", ai_content, {"phase": "thinking"})
                    else:
                        await _emit("answer", output[:200], {"phase": "final"})

    except GraphRecursionError:
        logger.warning(
            "[skill_loop %s] recursion limit %d reached", skill_name, recursion_limit
        )
        await _emit(
            "log", f"[停止] 达到最大轮次 {max_turns}（recursion_limit={recursion_limit}）", None
        )
        if not output:
            output = last_tool_output or "(达到最大轮次，无最终输出)"
        return {"success": True, "exit_code": 0, "output": output[:2000]}
    except Exception as exc:
        logger.exception("[skill_loop %s] execution error", skill_name)
        await _emit("log", f"[错误] 执行异常: {exc}", None)
        return {"success": False, "exit_code": 1, "output": f"execution error: {exc}"}

    if not output:
        output = last_tool_output or "(无输出)"
        await _emit("log", f"[完成] {output[:200]}", None)

    return {"success": True, "exit_code": 0, "output": output[:2000]}
