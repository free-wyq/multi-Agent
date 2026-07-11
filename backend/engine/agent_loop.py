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
    try:
        model = ChatOpenAI(
            model=model_name,
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            temperature=cfg["temperature"],
        )
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

    # unique thread_id per invocation so MemorySaver never collides
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
                pass
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
