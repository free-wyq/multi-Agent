"""Agentic execution loop — LangGraph ``create_agent`` + ``astream_events``.

Replaces the hand-rolled ReAct ``for`` loop with LangGraph's factory-built
agent graph (``langchain.agents.create_agent``), so the framework — not our
code — owns the model→tool→model iteration. We only subscribe to the graph's
event stream (``astream_events(version="v2")``) and project events onto the
``on_log`` callback so the frontend ``task_log`` WS stream keeps working.

Contracts preserved (agent_executor / registry depend on these):
- ``run_agent_loop(...) -> {"success", "exit_code", "output"}``
- ``set_extra_tools(tools: list) -> None``
- ``DEFAULT_MAX_TURNS = 15``
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable
from uuid import uuid4

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL
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
    """Pull the text content from an on_chain_end|model output (list[Command]).

    Each item in the list is a ``Command`` whose ``.update`` dict has a
    ``messages`` key containing ``[AIMessage]``. We want the last AIMessage's
    ``content`` string (the model's text reply).
    """
    if not isinstance(output, list):
        output = [output]
    for item in reversed(output):
        upd = getattr(item, "update", None)
        if isinstance(upd, dict):
            msgs = upd.get("messages", [])
            for m in reversed(msgs):
                if isinstance(m, AIMessage) and m.content:
                    return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _extract_tool_calls(output: Any) -> list:
    """Extract tool_calls from the on_chain_end|model output (list[Command])."""
    if not isinstance(output, list):
        output = [output]
    calls: list = []
    for item in output:
        upd = getattr(item, "update", None)
        if isinstance(upd, dict):
            msgs = upd.get("messages", [])
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
    on_log: Callable[[str], Awaitable[None]] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    system_prompt: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Run the agentic loop via LangGraph ``create_agent`` + ``astream_events``.

    Returns ``{"success": bool, "exit_code": int, "output": str}``.
    """
    model_name = agent_model or LLM_MODEL
    tools = tools_for_group(group_id)
    mcp_tools = list(_EXTRA_TOOLS)
    tools = tools + mcp_tools

    # ── build the agent graph (factory owns the ReAct loop) ──
    try:
        model = ChatOpenAI(
            model=model_name,
            base_url=OPENAI_BASE_URL,
            api_key=OPENAI_API_KEY,
            temperature=0,
        )
    except Exception as exc:
        logger.exception("[agent_loop %s] failed to init model", agent_name)
        if on_log:
            await on_log(f"[错误] 模型初始化失败: {exc}")
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
        agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=sys_content,
            checkpointer=MemorySaver(),
        )
    except Exception as exc:
        logger.exception("[agent_loop %s] create_agent failed", agent_name)
        if on_log:
            await on_log(f"[错误] 智能体图构建失败: {exc}")
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
            f"[开始] 智能体 {agent_name} 开始执行任务（max_turns={max_turns}, recursion_limit={recursion_limit}）"
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
                    await on_log(f"[工具] {name}({summary})")

            elif etype == "on_tool_end":
                raw_output = data.get("output", "")
                if hasattr(raw_output, "content"):
                    out_str = str(raw_output.content)
                else:
                    out_str = str(raw_output)
                last_tool_output = out_str
                if on_log:
                    await on_log(f"[工具] {name} → {out_str[:200]}")

            elif etype == "on_chain_end" and name == "model":
                # Model node finished — check if this is the final answer
                model_output = data.get("output")
                ai_content = _extract_ai_content(model_output)
                if ai_content:
                    output = ai_content
                    # If no tool_calls → final text answer → log [完成]
                    tool_calls = _extract_tool_calls(model_output)
                    if not tool_calls and on_log:
                        await on_log(f"[完成] {output[:200]}")

    except GraphRecursionError:
        logger.warning(
            "[agent_loop %s] recursion limit %d reached", agent_name, recursion_limit
        )
        if on_log:
            await on_log(
                f"[停止] 达到最大轮次 {max_turns}（recursion_limit={recursion_limit}）"
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
            await on_log(f"[错误] 执行异常: {exc}")
        return {"success": False, "exit_code": 1, "output": f"execution error: {exc}"}

    if not output:
        # Stream ended without a final text answer; fall back to last tool output
        output = last_tool_output or "(无输出)"
        if on_log:
            await on_log(f"[完成] {output[:200]}")

    return {"success": True, "exit_code": 0, "output": output[:2000]}
