"""Mention routing + 30s anti-loop (Rust middleware.rs).

``find_mentions`` scans content for ``@token`` sequences stripping trailing
punctuation. ``resolve_mention`` matches against group members by agent_id,
agent name, or alias substring. ``route_mentions`` deduplicates per
(sender->target, 30s) key to prevent routing loops. ``route_user_message``
routes an inbound user message: @mention -> target agent, otherwise -> coordinator.
"""
from __future__ import annotations

import time
from typing import Any

from engine.inbox import push_notify, push_task
from store import crud

# trailing punctuation to strip from mention tokens (Chinese + ASCII)
_TRAIL = "，。：！？.,:!?、"  # include 、 for enumerations


def find_mentions(content: str) -> list[str]:
    """Scan ``content`` for ``@name`` tokens, stripping trailing punctuation.

    Tokens are delimited by whitespace; trailing punctuation chars are removed.
    Duplicates are preserved (caller dedups as needed).
    """
    tokens: list[str] = []
    i = 0
    while i < len(content):
        if content[i] == "@":
            start = i + 1
            j = start
            while j < len(content) and not content[j].isspace():
                j += 1
            if j > start:
                name = content[start:j].rstrip(_TRAIL)
                if name:
                    tokens.append(name)
            i = j
        else:
            i += 1
    return tokens


def resolve_mention(
    members: list[Any],
    mention: str,
    agents: list[Any],
) -> str | None:
    """Three-tier match: (a) agent_id in members, (b) agent name in members, (c) alias contains token.

    ``members`` and ``agents`` may be dicts or Pydantic models; both are
    normalized to attribute access via ``_get``. Returns the matched agent_id
    or ``None``.
    """

    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # (a) agent_id direct hit on a member
    for m in members:
        if _get(m, "agent_id") == mention:
            return _get(m, "agent_id")
    # (b) agent name hit on a member's agent
    for a in agents:
        if _get(a, "name") == mention and any(
            _get(m, "agent_id") == _get(a, "id") for m in members
        ):
            return _get(a, "id")
    # (c) alias contains the token
    for m in members:
        alias = _get(m, "alias")
        if alias and mention in alias:
            return _get(m, "agent_id")
    return None


async def route_mentions(
    group_id: str,
    sender_id: str,
    sender_name: str,
    content: str,
    recent_routes: dict[str, float],
) -> None:
    """Outbound mention routing with 30s anti-loop.

    Scans ``content`` for @mentions, resolves each to a target agent, and
    ``push_task`` to the target. The ``recent_routes`` dict (mutated in place,
    owned by the AgentEngine) records ``f"{sender_id}->{target_id}"`` -> timestamp
    and skips a pair if it was routed within the last 30s.
    """
    mentions = find_mentions(content)
    if not mentions:
        return

    now = time.time()
    # prune entries older than 30s
    stale = [k for k, t in recent_routes.items() if now - t >= 30.0]
    for k in stale:
        recent_routes.pop(k, None)

    members = await crud.list_group_members_with_agent(group_id)
    agents = await crud.list_agents()

    for mention in mentions:
        # skip self (by id or name)
        if mention == sender_id or mention == sender_name:
            continue
        target_id = resolve_mention(members, mention, agents)
        if not target_id or target_id == sender_id:
            continue
        key = f"{sender_id}->{target_id}"
        if key in recent_routes:
            continue  # anti-loop: already routed this pair within 30s
        recent_routes[key] = now
        await push_task(group_id, sender_id, target_id, content, None)


async def route_user_message(group_id: str, content: str) -> None:
    """Route an inbound user message by @mention, else to the coordinator.

    If the message mentions a group member, ``push_notify`` to that agent with
    kind ``agent_reply``. Otherwise ``push_notify`` to the group coordinator with
    kind ``coordinator_reply``. The notify wakes the target engine's run loop.
    """
    mentions = find_mentions(content)
    if mentions:
        members = await crud.list_group_members_with_agent(group_id)
        agents = await crud.list_agents()
        for mention in mentions:
            target_id = resolve_mention(members, mention, agents)
            if target_id:
                await push_notify(
                    group_id, "agent_reply", "user", target_id, content, None
                )
                return  # route to the first @mentioned agent only
    # no mention hit -> coordinator
    group = await crud.get_group(group_id)
    if group and group.coordinator_id:
        await push_notify(
            group_id,
            "coordinator_reply",
            "user",
            group.coordinator_id,
            content,
            None,
        )
