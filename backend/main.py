"""FastAPI entrypoint.

Lifespan: init_db (create_all + seed demo data) then spin up the LangGraph
AgentRegistry (one resident asyncio.Task engine per agent). CORS allows the
Vite dev server and the packaged file:// origin.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import (
    agents,
    groups,
    mcp,
    messages,
    plan,
    scheduled_tasks,
    skills,
    system,
    tasks,
    websocket,
)
from engine.registry import registry
from engine.scheduler import load_from_store as load_schedule, shutdown_scheduler
from store.database import init_db

logger = logging.getLogger("multi-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: create tables + seed demo data
    await init_db()
    logger.info("init_db done")
    # spin up all agent engines (coordinator + members) from the store
    await registry.load_from_store()
    logger.info("registry loaded")
    # rebuild scheduled-task jobs from the store (APScheduler)
    await load_schedule()
    yield
    # shutdown: stop scheduler + all engines
    await shutdown_scheduler()
    await registry.shutdown_all()
    logger.info("registry shut down")


app = FastAPI(title="Multi-Agent Backend", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "file://",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(agents.router)
app.include_router(groups.router)
app.include_router(tasks.router)
app.include_router(messages.router)
app.include_router(skills.router)
app.include_router(mcp.router)
app.include_router(scheduled_tasks.router)
app.include_router(websocket.router)
app.include_router(plan.router)
