"""Agent task executor — bridges the engine to the agentic loop.

This replaces the old ``cli_executor.py`` (which was a mock). The executor
reads the agent definition (system_prompt, max_turns, model) and delegates to
``run_agent_loop``, which runs an LLM + bind_tools agentic loop using
framework-internal tools (no external Claude CLI).

Mounted skills (PRD PL-06) are resolved here: the agent's ``mounted_skills``
ids are looked up to their content and appended to the system prompt, so the
worker "knows" its skills and can follow them autonomously.

Return shape is ``{"success": bool, "exit_code": int, "output": str}`` —
identical to the old mock so ``AgentEngine._run_worker_task`` is unchanged
apart from the import rename.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from engine.agent_loop import DEFAULT_MAX_TURNS, run_agent_loop, set_extra_tools
from store import crud

logger = logging.getLogger("multi-agent.agent_executor")

_SKILL_HEADER = "\n\n## 已挂载技能\n你拥有以下技能，请根据任务需要自主使用：\n"


def _compose_system_prompt(base: str, skill_contents: list[str]) -> str:
    """Append mounted-skill content to the base system prompt (PL-06)."""
    base = (base or "").strip()
    if not skill_contents:
        return base
    blocks = []
    for i, content in enumerate(skill_contents, 1):
        blocks.append(f"### 技能 {i}\n{content}")
    return base + _SKILL_HEADER + "\n\n".join(blocks)


async def execute_agent_task(
    group_id: str,
    agent: dict[str, Any],
    task_content: str,
    task_id: str,
    on_log: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute a worker task via the agentic loop.

    Reads ``system_prompt``, ``max_turns``, and ``model`` from the agent
    definition and calls ``run_agent_loop``. Mounted skills are resolved and
    injected into the system prompt (PL-06). Mounted MCP connections are
    loaded as LangChain tools and injected via ``set_extra_tools`` (PL-07).
    Returns the standard ``{success, exit_code, output}`` dict.
    """
    agent_name = agent.get("name", "agent")
    agent_id = agent.get("id", "")
    system_prompt = agent.get("system_prompt", "") or ""
    agent_model = agent.get("model", "") or ""

    raw_turns = agent.get("max_turns", 0) or 0
    max_turns = raw_turns if raw_turns > 0 else DEFAULT_MAX_TURNS

    # PL-06: resolve mounted skills → inject into the system prompt
    mounted_skills = agent.get("mounted_skills") or []
    skill_contents = await crud.resolve_skill_contents(mounted_skills)
    if skill_contents:
        system_prompt = _compose_system_prompt(system_prompt, skill_contents)
        if on_log:
            await on_log(
                f"[技能] 已加载 {len(skill_contents)} 个挂载技能到上下文"
            )

    # PL-07: load MCP tools from mounted connections, inject into the loop
    mounted_mcp = agent.get("mounted_mcp") or []
    mcp_tools = []
    if mounted_mcp:
        from engine.mcp_manager import load_mcp_tools

        try:
            mcp_tools = await load_mcp_tools(mounted_mcp)
        except Exception as exc:
            logger.warning("[agent_executor] MCP tools load failed: %s", exc)
            if on_log:
                await on_log(f"[警告] MCP 工具加载失败: {exc}")
        if mcp_tools:
            if on_log:
                await on_log(
                    f"[MCP] 已挂载 {len(mcp_tools)} 个外部工具: "
                    + ", ".join(t.name for t in mcp_tools)
                )
            set_extra_tools(mcp_tools)
        else:
            set_extra_tools([])

    logger.info(
        "[agent_executor] group=%s agent=%s task_id=%s model=%s turns=%d skills=%d mcp=%d",
        group_id, agent_name, task_id, agent_model or "(default)", max_turns,
        len(skill_contents), len(mcp_tools),
    )

    try:
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
    finally:
        # clear extra tools so concurrent runs don't bleed (set per-run)
        set_extra_tools([])
