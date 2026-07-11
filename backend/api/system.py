"""System routes: health check (Electron readiness), data dir, agent status
(per-group + SA-02 aggregate all-groups), LLM config (CF-04 GET/PUT /api/config)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

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

    Writes the new model back to ``os.environ`` via ``set_config`` so the
    engine picks it up on the next invoke (CF-05 — no restart). Blank/None
    ``model`` is a no-op that echoes the current config. Returns the masked
    post-write state (same shape as GET).
    """
    set_config(model=body.model)
    return get_config_public()
