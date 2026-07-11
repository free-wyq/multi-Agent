"""System routes: health check (Electron readiness), data dir, agent status
(per-group + SA-02 aggregate all-groups), LLM config (CF-04 GET/PUT /api/config),
LLM provider CRUD (多模型服务商配置)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

import config as _config
from config import DATA_DIR, get_config_public, set_config
from engine.registry import registry

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/data-dir")
async def data_dir() -> dict[str, str]:
    return {"path": DATA_DIR}


@router.get("/api/status")
async def all_status() -> dict[str, list[dict]]:
    """Return every group's agent statuses in one call (SA-02).

    Aggregates ``registry.list_all_status()`` → ``{group_id: [agent status, ...]}``
    so the frontend can pull all groups' live agent state with a *single* request
    instead of one ``GET /api/status/{groupId}`` per group per tick (the N+1
    polling this replaces). Each agent status dict has the same shape as the
    per-group route (id / name / role / status / current_task_id).

    Groups with no live engines are absent from the dict (the frontend treats a
    missing key as "no agents / all offline"). Distinct path from
    ``GET /api/status/{group_id}`` (no path segment) so the two routes don't
    collide — FastAPI matches the segment-less path here and the parameterized
    path there.
    """
    return registry.list_all_status()


@router.get("/api/status/{group_id}")
async def group_status(group_id: str) -> list[dict]:
    """Return each agent's status (idle|executing|offline) for a group."""
    return registry.list_group_status(group_id)


# ── LLM config (CF-04: single config endpoint, key masked) ──────────────


class ConfigUpdateBody(BaseModel):
    """PUT /api/config body. Only ``model`` is mutable; optional + non-empty
    so an absent/blank field is a no-op (echoes current state without clobber)."""

    model: str | None = None


@router.get("/api/config")
async def get_llm_config_route() -> dict:
    """Return the current LLM config with the API key masked.

    The raw secret never leaves the process — ``api_key`` is a short
    preview (first 3 + last 3 chars) and ``has_key`` lets the UI show
    "configured" without exposing the key.
    """
    return get_config_public()


@router.put("/api/config")
async def update_llm_config_route(body: ConfigUpdateBody) -> dict:
    """Hot-switch the active LLM model.

    Persists the new model to the ACTIVE provider in DB (if one exists) and
    refreshes the in-memory cache so ``get_config()`` returns it on the next
    sync call — no restart needed (CF-05). If there is no active provider row,
    falls back to the old ``set_config(model)`` env path. Blank/None ``model``
    is a no-op that echoes the current config. Returns the masked post-write
    state (same shape as GET).
    """
    if body.model:
        from store import crud

        active = await crud.get_active_provider_entity()
        if active:
            row = await crud.update_provider_model(active.id, body.model)
            if row:
                _config.set_active_cache(
                    {
                        "provider": row.provider,
                        "model": row.model,
                        "base_url": row.base_url,
                        "api_key": row.api_key or "",
                        "temperature": row.temperature,
                        "max_tokens": row.max_tokens,
                    }
                )
            else:
                set_config(model=body.model)
        else:
            set_config(model=body.model)
    return get_config_public()


# ── LLM Provider CRUD (多模型服务商配置) ──────────────────────

from models import LlmProvider, LlmProviderCreatePayload  # noqa: E402


@router.get("/api/providers")
async def list_providers_route() -> list[LlmProvider]:
    """List all configured LLM providers (api_key masked on each)."""
    from store import crud

    return await crud.list_providers()


@router.post("/api/providers")
async def create_provider_route(body: LlmProviderCreatePayload) -> LlmProvider:
    """Create a new provider. If ``is_active`` is True, all others are
    deactivated (single-active invariant) and the cache is refreshed."""
    from store import crud

    provider = await crud.create_provider(body)
    if provider.is_active:
        entity = await crud.get_active_provider_entity()
        if entity:
            _config.set_active_cache(
                {
                    "provider": entity.provider,
                    "model": entity.model,
                    "base_url": entity.base_url,
                    "api_key": entity.api_key or "",
                    "temperature": entity.temperature,
                    "max_tokens": entity.max_tokens,
                }
            )
    return provider


@router.put("/api/providers/{provider_id}")
async def update_provider_route(
    provider_id: str, body: LlmProviderCreatePayload
) -> LlmProvider | dict:
    """Update a provider's fields. ``api_key`` empty/None means "leave
    unchanged" (so editing other fields doesn't wipe the stored key). If the
    updated provider is the active one, the cache is refreshed."""
    from store import crud

    provider = await crud.update_provider(provider_id, body)
    if provider is None:
        return {"ok": False, "error": "provider not found"}
    if provider.is_active:
        entity = await crud.get_active_provider_entity()
        if entity:
            _config.set_active_cache(
                {
                    "provider": entity.provider,
                    "model": entity.model,
                    "base_url": entity.base_url,
                    "api_key": entity.api_key or "",
                    "temperature": entity.temperature,
                    "max_tokens": entity.max_tokens,
                }
            )
    return provider


@router.delete("/api/providers/{provider_id}")
async def delete_provider_route(provider_id: str) -> dict:
    """Delete a provider. If the active one was deleted, the first remaining
    provider is auto-activated and the cache is refreshed from it."""
    from store import crud

    deleted, reassigned = await crud.delete_provider(provider_id)
    if not deleted:
        return {"ok": False, "error": "provider not found"}
    if reassigned:
        entity = await crud.get_active_provider_entity()
        if entity:
            _config.set_active_cache(
                {
                    "provider": entity.provider,
                    "model": entity.model,
                    "base_url": entity.base_url,
                    "api_key": entity.api_key or "",
                    "temperature": entity.temperature,
                    "max_tokens": entity.max_tokens,
                }
            )
    return {"ok": True}


@router.post("/api/providers/{provider_id}/activate")
async def activate_provider_route(provider_id: str) -> LlmProvider | dict:
    """Set a provider as the active one (deactivates all others) and refresh
    the cache so ``get_config()`` returns this provider's config immediately."""
    from store import crud

    provider = await crud.set_active_provider(provider_id)
    if provider is None:
        return {"ok": False, "error": "provider not found"}
    entity = await crud.get_active_provider_entity()
    if entity:
        _config.set_active_cache(
            {
                "provider": entity.provider,
                "model": entity.model,
                "base_url": entity.base_url,
                "api_key": entity.api_key or "",
                "temperature": entity.temperature,
                "max_tokens": entity.max_tokens,
            }
        )
    return provider


# ── Slash helper (BE-01: backend parsing the frontend can't do alone) ────


class SlashBody(BaseModel):
    """``POST /api/slash`` body. ``command`` is the slash token without the
    leading ``/`` (e.g. ``"tools"``); ``agent_id``/``group_id`` are optional
    context some commands need (``/tools`` uses both)."""

    command: str
    agent_id: str | None = None
    group_id: str | None = None


async def _slash_tools(body: SlashBody) -> dict[str, Any]:
    """Aggregate the tools an agent will actually bind (internal + mounted MCP).

    Single source of truth lives in the backend: the internal tools are defined
    in ``engine.tools.tools_for_group`` (hard-coding them on the frontend would
    drift), and MCP tools need an async load (the frontend hitting
    ``GET /api/mcp/{id}/tools`` per connection is N+1 and can't see the merged
    set). One call here returns both. Internal tool names/descriptions are
    workspace-independent (the closure only binds a workspace when *invoked*),
    so a missing ``group_id`` still yields the internal roster.
    """
    from engine.tools import tools_for_group
    from engine.mcp_manager import list_mcp_tools
    from store import crud

    internal = [
        {"name": t.name, "description": (t.description or "")[:200]}
        for t in tools_for_group(body.group_id or "")
    ]

    mcp_tools: list[dict[str, Any]] = []
    if body.agent_id:
        agent = await crud.get_agent(body.agent_id)
        mounted = (agent.mounted_mcp if agent else None) or []
        if mounted:
            try:
                mcp_tools = await list_mcp_tools(mounted)
            except Exception as exc:
                # MCP load is best-effort: return what we have + flag the failure
                # so the frontend can surface it rather than show an empty list.
                return {
                    "ok": False,
                    "command": "tools",
                    "error": f"MCP tools load failed: {exc}",
                    "tools": {"internal": internal, "mcp": []},
                    "total": len(internal),
                }

    return {
        "ok": True,
        "command": "tools",
        "agent_id": body.agent_id,
        "group_id": body.group_id,
        "tools": {"internal": internal, "mcp": mcp_tools},
        "total": len(internal) + len(mcp_tools),
    }


@router.post("/api/slash")
async def slash_helper(body: SlashBody) -> dict[str, Any]:
    """Backend parser for slash commands the frontend can't resolve alone (BE-01).

    Most slash commands (``/new`` ``/model`` ``/status`` …) are pure frontend,
    but a few need a backend truth source — e.g. ``/tools`` whose "actually
    bound tools" lives in the engine. This routes ``command`` to a handler and
    returns a structured result; unsupported commands return ``{ok: False}``
    rather than raising so the frontend can fall back to its own parsing.
    """
    cmd = (body.command or "").strip().lstrip("/").lower()
    if cmd == "tools":
        return await _slash_tools(body)
    return {
        "ok": False,
        "command": body.command,
        "error": f"unsupported slash command: {body.command!r}",
    }
