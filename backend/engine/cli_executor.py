"""M3 placeholder CLI executor (Rust workspace.rs LocalWorkspace.execute).

M3 returns a mock success result so the dispatch -> worker execute -> report ->
continue chain can be exercised end-to-end without spawning a real Claude Code
CLI subprocess. M5 will replace ``execute_claude_cli`` with an
``asyncio.create_subprocess_exec`` call to ``claude --print`` with per-line
``on_log`` streaming.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("multi-agent.cli_executor")


async def execute_claude_cli(
    group_id: str,
    agent: dict[str, Any],
    task_content: str,
    task_id: str,
    on_log: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """M3 mock executor. Returns a canned success result.

    M5 will replace the body with a real ``asyncio.create_subprocess_exec``
    invocation of the Claude Code CLI (``claude --print``) with streaming
    stdout/stderr -> ``on_log``. The return shape stays the same so callers
    (``AgentEngine._run_worker_task``) do not change.
    """
    logger.info(
        "[cli_executor] M3 mock execute: group=%s agent=%s task_id=%s content=%s",
        group_id, agent.get("name"), task_id, task_content[:60],
    )
    if on_log:
        await on_log(f"[M3 mock] 开始执行任务: {task_content[:50]}...")
        await on_log("[M3 mock] 任务已模拟执行完成")

    return {
        "success": True,
        "exit_code": 0,
        "output": "[M3 mock] 任务已模拟执行完成",
    }
