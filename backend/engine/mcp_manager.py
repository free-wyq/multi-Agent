"""MCP tool manager — loads LangChain tools from mounted MCP connections.

Wraps ``langchain-mcp-adapters``' ``MultiServerMCPClient``. For a list of
mounted MCP connection ids (PRD MC-06), this resolves the enabled connections
(via ``crud.resolve_mcp_configs``) and loads their tools as LangChain
``BaseTool`` instances (PRD PL-07). The worker agent then merges these with
the framework-internal tools and binds them all to the LLM.

Connection lifecycle: each ``get_tools`` call builds a fresh
``MultiServerMCPClient`` and loads tools. The underlying MCP stdio subprocess
or SSE session is spun up by the adapter to fetch the tool list; for stdio
servers the process is terminated after the tool listing is complete (the
adapter re-spawns on each tool invocation). This keeps config changes (toggle
on/off, edit command) effective without a restart.

任务16c（spawn 泄漏/超时）加固：
  - ``load_mcp_tools`` 用 ``asyncio.wait_for(timeout=MCP_INTROSPECT_TIMEOUT)`` 包住
    ``get_tools``，stdio server 启动失败/自省挂死时不再拖到 worker 看门狗（300s）才
    降级——超时即 ``TimeoutError``，被 ``agent_executor`` 的 try/except 捕获，降级为
    「无该 MCP 工具」并 warn（一个挂的 server 不阻塞整个 agent，对齐既有 per-connection
    容错语义）。
  - ``_build_client`` 保持不变：adapter ``__aexit__`` 已 NotImplemented（不支持 async with
    client），每个 ``get_tools``/``ainvoke`` 自开自关会话，子进程在会话 ``async with``
    退出时由 ``stdio_client`` 的 SIGTERM→SIGKILL 兜底终止，无需我们持有 session 句柄。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from config import MCP_INTROSPECT_TIMEOUT
from store import crud

logger = logging.getLogger("multi-agent.mcp")

# avoid holding subprocess sessions across the whole agentic loop; each
# load_mcp_tools opens a short-lived session. If a tool is later invoked the
# adapter re-spawns. This matches langchain-mcp-adapters' default behaviour.


def _build_client(configs: list[tuple[str, dict]]) -> MultiServerMCPClient:
    """Build a MultiServerMCPClient from (name, connection_config) pairs."""
    connections: dict[str, dict] = {}
    for name, cfg in configs:
        # de-duplicate by name; if the same name appears twice (shouldn't),
        # suffix with index to avoid clobbering
        key = name
        i = 1
        while key in connections:
            key = f"{name}_{i}"
            i += 1
        connections[key] = cfg
    return MultiServerMCPClient(connections)


async def load_mcp_tools(mcp_ids: list[str]) -> list[BaseTool]:
    """Load LangChain tools from the given mounted MCP connection ids.

    Skips disabled or unresolvable connections. Returns an empty list if no
    enabled connections are mounted. Logs but does not raise on per-connection
    failures so one broken MCP server doesn't break the whole agent.

    任务16c：每个 server 的 ``get_tools`` 自省用 ``MCP_INTROSPECT_TIMEOUT``
    （默认 20s）兜底——stdio server 启动失败/自省挂死时，超时即把该 server 当
    失败跳过（warn），不再无限阻塞到 worker 看门狗（300s）。整段失败仍向上让
    ``agent_executor`` 的 try/except 接住降级。
    """
    configs = await crud.resolve_mcp_configs(mcp_ids)
    if not configs:
        return []

    client = _build_client(configs)
    tools: list[BaseTool] = []
    for name, _cfg in configs:
        try:
            # 任务16c：get_tools spawn stdio 子进程并 tools/list，挂死时不要拖到
            # WORKER_TASK_TIMEOUT（300s）才降级——独立更短超时，让单 server 失败
            # 不阻塞整个 worker 任务。TimeoutError 同其他异常一样归入 per-connection
            # 容错分支（warn + continue），其余 server 仍正常加载。
            server_tools = await asyncio.wait_for(
                client.get_tools(server_name=name),
                timeout=MCP_INTROSPECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[mcp] introspection of '%s' timed out after %.0fs — skipping",
                name, MCP_INTROSPECT_TIMEOUT,
            )
            continue
        except Exception as exc:
            logger.warning(
                "[mcp] failed to load tools from '%s': %s", name, exc
            )
            continue
        tools.extend(server_tools)
        logger.info(
            "[mcp] loaded %d tool(s) from '%s'", len(server_tools), name
        )
    return tools


async def list_mcp_tools(mcp_ids: list[str]) -> list[dict[str, Any]]:
    """Return a serializable preview of tools each mounted MCP provides.

    Used by the API to show what tools a connection exposes (introspection).
    Returns ``[{name, description}]`` per connection, flattened.
    """
    tools = await load_mcp_tools(mcp_ids)
    return [
        {"name": t.name, "description": (t.description or "")[:200]}
        for t in tools
    ]
