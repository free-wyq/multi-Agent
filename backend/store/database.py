"""SQLAlchemy async engine + session for SQLite.

Engine: create_async_engine with the aiosqlite driver. init_db creates all
tables and seeds demo data on first run. WAL journal mode is enabled once
per database file via a sync sqlite3 connection at import time (the aiosqlite
adapter runs connections in a worker thread whose cursor().execute() returns
a coroutine, so the per-connection sync event listener cannot run PRAGMAs
directly).
"""
from __future__ import annotations

import logging
import pathlib
import sqlite3

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import DATA_DIR

logger = logging.getLogger("multi-agent.database")

# SQLite file lives under the platform data directory (<DATA_DIR>/data.db).
DB_PATH = pathlib.Path(DATA_DIR) / "data.db"
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"


def _enable_wal_once() -> None:
    """Set ``journal_mode=WAL`` on the database file once at import time.

    WAL is a persistent property of the database file (not the connection),
    so setting it once via a sync sqlite3 connection is sufficient. This avoids
    the aiosqlite worker-thread limitation where the sync ``connect`` event
    listener cannot call ``cursor.execute`` (it returns a coroutine).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        conn.close()
    except Exception:
        # best-effort: WAL/foreign_keys are performance/integrity hints, not
        # gating — create_all will still build a working DB in the default
        # rollback journal mode. Logged at debug (not exception): import-time
        # PRAGMA failures are environment/setup issues, surfaced so a missing
        # WAL isn't a silent degradation (B31 错误处理重巡航——原 `pass` 静默吞没).
        logger.debug("[database] WAL/foreign_keys PRAGMA failed", exc_info=True)


def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    """Add ``column`` to ``table`` if it is missing (additive migration).

    SQLAlchemy ``create_all`` creates missing tables but will not ALTER an
    existing table to add a new column. When the schema grows (e.g. M7 adds
    ``agents.mounted_skills``), pre-existing databases would otherwise crash on
    reads. This runs ``PRAGMA table_info`` and issues a guarded ``ALTER TABLE``
    so old DBs upgrade in place. Safe to run every startup (no-op when present).
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()


def _migrate_schema() -> None:
    """Apply additive column migrations the async engine cannot do at runtime.

    Runs once at import (sync sqlite3) right after WAL setup. Only adds columns
    that are missing; never drops. New tables are created by ``create_all``.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        _ensure_column(conn, "agents", "mounted_skills", "JSON NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "agents", "mounted_mcp", "JSON NOT NULL DEFAULT '[]'")
        # Multi-model provider catalog (PRD 多模型服务商): models JSON list +
        # 6 connection-level columns. Defaults mirror LlmProviderEntity /
        # LlmProvider output model so a legacy row upgrades to a usable config
        # (empty catalog falls back to the legacy `model` column via
        # crud._select_model). See backend/store/entities.py.
        _ensure_column(conn, "llm_providers", "models", "JSON NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "llm_providers", "api_version", "VARCHAR NOT NULL DEFAULT ''")
        _ensure_column(conn, "llm_providers", "organization", "VARCHAR NOT NULL DEFAULT ''")
        _ensure_column(conn, "llm_providers", "extra_headers", "JSON")
        _ensure_column(conn, "llm_providers", "request_timeout", "FLOAT NOT NULL DEFAULT 120.0")
        _ensure_column(conn, "llm_providers", "max_retries", "INTEGER NOT NULL DEFAULT 2")
        _ensure_column(conn, "llm_providers", "proxy", "VARCHAR NOT NULL DEFAULT ''")
        conn.close()
    except Exception:
        # best-effort additive migration: pre-existing DBs that don't have a
        # column yet get ALTER'd; a failure here is logged (not swallowed) so
        # a silently-stuck schema doesn't hide behind `pass`. create_all still
        # builds the tables for a fresh DB, so this is a graceful degradation
        # for the upgrade-in-place path, not a fatal error (B31 错误处理重巡航——
        # 原 `pass` 静默吞没，schema 升级失败不可观测).
        logger.debug("[database] additive column migration failed", exc_info=True)


_enable_wal_once()
_migrate_schema()

# check_same_thread=False: SQLAlchemy manages thread access; aiosqlite spawns
# its own thread anyway. pool_pre_ping recovers stale connections.
engine = create_async_engine(
    DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

# async session factory: expire_on_commit=False so returned Pydantic models
# (built before commit) stay valid after the session closes.
SessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields an AsyncSession and closes it on exit."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables (if absent) and seed demo data on first run.

    ``create_all`` only adds missing tables, not columns; the additive column
    migration (``_migrate_schema``) runs at import for pre-existing DBs.
    """
    from store.crud import load_active_provider_into_cache
    from store.entities import Base
    from store.seed import seed_demo_data

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await seed_demo_data(SessionLocal)
    # Load the active LLM provider into the sync cache so get_config() returns
    # the DB-backed config (not the env fallback) from the very first call.
    await load_active_provider_into_cache()
