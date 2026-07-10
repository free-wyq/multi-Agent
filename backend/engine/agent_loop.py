"""Agentic execution loop — LLM + bind_tools, iterates until done or max_turns.

This replaces the old ``claude --print`` CLI subprocess approach. The worker
is now a true agentic loop: a ``ChatOpenAI`` model with ``bind_tools`` calls
framework-internal tools (read_file/write_file/edit_file/list_dir/run_command)
that operate on the group's workspace. Each step (tool call, result, or final
text) is streamed via ``on_log`` to the frontend as a ``task_log`` line.

This is the core of M5: no external Claude CLI is invoked.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from config import LLM_MODEL, OPENAI_API_KEY, OPENAI_BASE_URL
from engine.tools import tools_for_group

logger = logging.getLogger("multi-agent.agent_loop")

DEFAULT_MAX_TURNS = 15

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
    """Run the agentic loop: LLM + bind_tools, iterate to completion.

    Returns ``{"success": bool, "exit_code": int, "output": str}``.
    """
    model_name = agent_model or LLM_MODEL
    tools = tools_for_group(group_id)

    try:
        model = ChatOpenAI(
            model=model_name,
            base_url=OPENAI_BASE_URL,
            api_key=OPENAI_API_KEY,
            temperature=0,
        ).bind_tools(tools)
    except Exception as exc:
        logger.exception("[agent_loop %s] failed to init model", agent_name)
        if on_log:
            await on_log(f"[错误] 模型初始化失败: {exc}")
        return {"success": False, "exit_code": 1, "output": f"model init error: {exc}"}

    sys_content = (system_prompt or "").strip()
    if sys_content:
        sys_content += "\n"
    sys_content += _TOOL_SYSTEM_SUFFIX

    messages: list[Any] = [SystemMessage(content=sys_content)]
    messages.append(HumanMessage(content=task_content))

    if on_log:
        await on_log(f"[开始] 智能体 {agent_name} 开始执行任务（最多 {max_turns} 轮）")

    tool_map: dict[str, Any] = {t.name: t for t in tools}
    output = ""

    for turn in range(1, max_turns + 1):
        try:
            resp: AIMessage = await model.ainvoke(messages)
        except Exception as exc:
            logger.exception("[agent_loop %s] LLM invoke error turn %d", agent_name, turn)
            if on_log:
                await on_log(f"[错误] LLM 调用失败（第{turn}轮）: {exc}")
            return {"success": False, "exit_code": 1, "output": f"LLM error: {exc}"}

        messages.append(resp)

        tool_calls = getattr(resp, "tool_calls", None) or []

        if not tool_calls:
            # No tool calls → pure text reply → done
            output = resp.content if isinstance(resp.content, str) else str(resp.content)
            if on_log:
                await on_log(f"[完成] {output[:200]}")
            return {"success": True, "exit_code": 0, "output": output}

        # Process each tool call
        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_args = tc.get("args", {})
            tc_id = tc.get("id", "")
            summary = _summarize_args(tc_args)

            if tc_name not in tool_map:
                err_msg = f"未知工具: {tc_name}"
                if on_log:
                    await on_log(f"[工具] {tc_name}({summary}) → {err_msg}")
                messages.append(
                    ToolMessage(
                        content=err_msg,
                        tool_call_id=tc_id,
                    )
                )
                continue

            try:
                result = await tool_map[tc_name].ainvoke(tc_args)
            except Exception as exc:
                result = f"工具执行异常: {exc}"
                logger.exception("[agent_loop %s] tool %s error", agent_name, tc_name)

            result_str = str(result)
            if on_log:
                await on_log(
                    f"[工具] {tc_name}({summary}) → {result_str[:200]}"
                )
            messages.append(
                ToolMessage(content=result_str, tool_call_id=tc_id)
            )

        # Continue to next turn for the LLM to process tool results

    # Max turns exhausted
    if on_log:
        await on_log(f"[停止] 达到最大轮次 {max_turns}，停止执行")
    if not output:
        last = messages[-1]
        output = last.content if hasattr(last, "content") else str(last)
        if not isinstance(output, str):
            output = str(output)
    return {"success": True, "exit_code": 0, "output": output[:2000]}
