"""System routes: health check (Electron readiness), data dir, agent status."""
from __future__ import annotations

from fastapi import APIRouter

from config import DATA_DIR
from engine.registry import registry

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/data-dir")
async def data_dir() -> dict[str, str]:
    return {"path": DATA_DIR}


@router.get("/api/status/{group_id}")
async def group_status(group_id: str) -> list[dict]:
    """Return each agent's status (idle|executing|offline) for a group."""
    return registry.list_group_status(group_id)
