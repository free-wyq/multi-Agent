"""Group + GroupMember + GroupFile + GroupCreatePayload Pydantic models."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Group(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    coordinator_id: str = ""
    description: str | None = None
    status: str = "active"
    config: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


class GroupCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    coordinator_id: str | None = None
    description: str | None = None
    member_ids: list[str] = []


class GroupMember(BaseModel):
    """Flat structure: member fields + agent_name + agent_role.

    Frontend `GroupMember` interface accesses id/group_id/agent_id/alias/joined_at
    and agent_name/agent_role at the top level (Rust used #[serde(flatten)]).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    group_id: str
    agent_id: str
    alias: str | None = None
    joined_at: str = ""
    agent_name: str = ""
    agent_role: str = ""


class GroupFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    size: int = 0
    modified_at: str = ""
