"""Group + GroupMember + GroupFile + GroupCreatePayload Pydantic models."""
from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict


class GroupConfig(TypedDict, total=False):
    """Agreed schema for ``Group.config`` (the free-form JSON column).

    MT-03 codifies the config-dict key convention here so the read path
    (coordinator ``node_llm_decide``) and the write path (group settings Modal
    via ``update_group``) share one source of truth for key names + defaults.

    Kept as a ``TypedDict`` for documentation / type-checking only — the wire
    format stays ``dict[str, Any]`` (SQLite JSON column + frontend raw dict),
    so unknown keys (future extensions) are tolerated. ``total=False`` because
    every key is optional (a group may have no config at all).

    Keys:
      auto_confirm: PL-02/PL-03 plan-confirmation switch. False (default) → the
        coordinator announces a plan then ENDS, waiting for explicit user
        confirmation; True ("直接干") → fan out immediately. Written by the
        plan-direct API (api/plan.py), read per ainvoke in registry.
      leader_strategy: MT-03 Leader 指挥策略 — free-text guidance the user
        writes for the group's Leader (e.g. "注重代码质量，每步必须自测通过
        再交付"). Injected into the coordinator LLM prompt by
        ``node_llm_decide`` so the Leader's 拆解/派工 decisions honour it.
        Empty string when unset (coordinator runs with no extra strategy).
        Written by the group settings Modal (GroupPage) via ``update_group``.
      collaboration_mode: 群组协作模式 — "centralized" (default, 群主主导
        supervisor 子图 + 拆计划派工) or "decentralized" (纯 swarm，群里无群主
        概念，裸消息群主当首发对标 LangGraph ``create_swarm`` 的
        ``default_active_agent`` 兜底，@群主合法 handoff). 走 auto_confirm 同款
        config 链路（model→state→每回合现读注入），但额外让 route_entry 按 mode
        分流 + compile_graph 按 mode 决定 coordinator 是否纳入 members（做法 A 的
        图级二选一，切换 mode 触发 recompile_group 重编译). 单聊群 (single_chat)
        不进群图，mode 对单聊无意义。老群组兜底 "centralized"。
    """

    auto_confirm: bool
    leader_strategy: str
    collaboration_mode: str


def get_leader_strategy(config: dict[str, Any] | None) -> str:
    """Safe accessor for ``Group.config["leader_strategy"]`` (MT-03).

    Returns the Leader 指挥策略 string, or ``""`` when the group has no config
    or the key is unset (coordinator runs with no extra strategy). Centralized
    here so the read path (coordinator ``node_llm_decide``) has one source of
    truth for the default + key name, mirroring how ``auto_confirm`` is read
    inline as ``config.get("auto_confirm", False)``.
    """
    if not config:
        return ""
    return str(config.get("leader_strategy", "") or "")


def get_collaboration_mode(config: dict[str, Any] | None) -> str:
    """Safe accessor for ``Group.config["collaboration_mode"]``.

    Returns ``"centralized"`` (default) or ``"decentralized"`` — never ``""``.
    Centralized here so the read path (``GroupRuntime._resolve_group_config`` +
    ``_resolve_members`` + ``route_entry`` + ``_resolve_handoff_target``) has
    one source of truth for the default + key name. ``"centralized"`` is the
    default (also for老群组兜底 — a group with no config / missing key reads
    centralized, preserving the current supervisor-subgraph behaviour).

    The mode drives both the图装配 (decentralized includes the coordinator as
    an普通 member with an ``agent_<coordinator_id>`` node; centralized excludes
    it as today — 做法 A 图级二选一) and the route_entry 分流 (decentralized
    裸消息群主当首发对标 swarm ``default_active_agent`` / @群主 合法 handoff;
    centralized 裸消息 → classify).
    """
    if not config:
        return "centralized"
    mode = str(config.get("collaboration_mode", "") or "")
    return mode if mode in ("centralized", "decentralized") else "centralized"


class Group(BaseModel):
    """A collaboration team: Leader + members.

    ``config`` is the free-form JSON column whose agreed schema is
    :class:`GroupConfig` (``auto_confirm`` + ``leader_strategy``). It stays
    ``dict[str, Any]`` on the wire for backward compat with the SQLite JSON
    column and the frontend raw dict; use :func:`get_leader_strategy` for the
    typed read.
    """

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
