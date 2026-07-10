"""Agent task executor — bridges the engine to the agentic loop.

This replaces the old ``cli_executor.py`` (which was a mock). The executor
reads the agent definition (system_prompt, max_turns, model) and delegates to
``run_agent_loop``, which runs an LLM + bind_tools agentic loop using
framework-internal tools (no external Claude CLI).

Return shape is ``{"success": bool, "exit_code": int, "output": str}`` —
identical to the old mock so ``AgentEngine._run_worker_task`` is unchanged
apart from the import rename.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from engine.agent_loop import DEFAULT_MAX_TURNS, run_agent_loop

logger = logging.getLogger("multi-agent.agent_executor")


async def execute_agent_task(
    group_id: str,
    agent: dict[str, Any],
    task_content: str,
    task_id: str,
    on_log: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute a worker task via the agentic loop.

    Reads ``system_prompt``, ``max_turns``, and ``model`` from the agent
    definition and calls ``run_agent_loop``. Returns the standard
    ``{success, exit_code, output}`` dict.
    """
    agent_name = agent.get("name", "agent")
    agent_id = agent.get("id", "")
    system_prompt = agent.get("system_prompt", "") or ""
    agent_model = agent.get("model", "") or ""

    raw_turns = agent.get("max_turns", 0) or 0
    max_turns = raw_turns if raw_turns > 0 else DEFAULT_MAX_TURNS

    logger.info(
        "[agent_executor] group=%s agent=%s task_id=%s model=%s turns=%d",
        group_id, agent_name, task_id, agent_model or "(default)", max_turns,
    )

    return await run_agent_loop(
        group_id=group_id,
        agent_id=agent_id,
        agent_name=agent_name,
        task_content=task_content,
        task_id=task_id,
        on_log=on_log,
        max_turns=max_turns,
        system_prompt=system_prompt,
        agent_model=agent_model,
    )
