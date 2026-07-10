"""McpConnection + McpConnectionCreatePayload Pydantic models (PRD 3.4).

An MCP connection is an external tool source the agent can call at execution
time (PL-07). Two transports are supported (MC-02):
- ``stdio``: spawn a local command (command + args + env)
- ``sse``: connect to a remote SSE endpoint (url + headers)

Connections are mounted onto agents by id (``AgentEntity.mounted_mcp``); at
execution time the engine resolves them and loads LangChain tools via
``langchain-mcp-adapters`` (``MultiServerMCPClient``).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class McpConnection(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    transport: str = "stdio"  # stdio | sse
    # stdio transport
    command: str = ""
    args: list[str] = []
    env: dict[str, str] | None = None
    # sse transport
    url: str = ""
    headers: dict[str, Any] | None = None
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class McpConnectionCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, Any] | None = None
    enabled: bool = True
