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
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

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
    """
    configs = await crud.resolve_mcp_configs(mcp_ids)
    if not configs:
        return []

    client = _build_client(configs)
    tools: list[BaseTool] = []
    for name, _cfg in configs:
        try:
            server_tools = await client.get_tools(server_name=name)
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
