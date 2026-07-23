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
        # Skill frontmatter（Claude Skills 化 · 阶段一地基2）：三列 additive，
        # 旧 skills 表缺这三列时补上空 list 默认值，旧行读到 [] 而非 KeyError。
        _ensure_column(conn, "skills", "requires_tools", "JSON NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "skills", "triggers", "JSON NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "skills", "outputs", "JSON NOT NULL DEFAULT '[]'")
        # Path C (single-chat entity split, strict rename ``group_id`` →
        # ``conversation_id`` on Message/Task): development-period data is
        # disposable (user 拍板: 不写迁移脚本，直接 drop+recreate). When the
        # legacy ``group_id`` column is still present on ``messages``/``tasks``
        # (pre-rename schema), drop those tables so ``create_all`` rebuilds them
        # with ``conversation_id``. Runs at import (here) AND in ``init_db`` so
        # tests that import ``main`` without triggering the FastAPI lifespan
        # still see the renamed schema. A fresh DB is unaffected (no group_id
        # column → skip).
        try:
            msgs_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "group_id" in msgs_cols and "conversation_id" not in msgs_cols:
                conn.execute("DROP TABLE IF EXISTS messages")
                conn.execute("DROP TABLE IF EXISTS tasks")
                conn.commit()
                logger.info("[database] Path C: dropped legacy messages/tasks (group_id→conversation_id)")
        except Exception:
            logger.debug("[database] Path C drop/recreate check failed", exc_info=True)

        # Path C 收尾：drop 之后必须同步重建 messages/tasks/conversations 三张表，
        # 因为 async ``Base.metadata.create_all`` 只在 ``init_db``（FastAPI lifespan）
        # 里跑；测试 import ``main`` 不触发 lifespan，drop 完表没重建 →
        # ``no such table: messages``。用一个同步 SQLAlchemy engine 对同一 DB 文件
        # 跑 ``create_all``（只建这三张表，其余由 init_db 兜底），确保 import 后表
        # 一定存在。对照当前 ``ConversationEntity``/``MessageEntity``/``TaskEntity``
        # schema 建表，新库（无 legacy group_id）也建上，省一次 create_all 往返。
        try:
            from sqlalchemy import create_engine as _create_sync_engine

            from store.entities import (
                Base,
                ConversationEntity,
                MessageEntity,
                TaskEntity,
            )

            # 同步 engine 指向同一 SQLite 文件；check_same_thread=False 与 async
            # engine 一致（import 期单线程用完即弃，无并发问题）。
            sync_engine = _create_sync_engine(
                f"sqlite:///{DB_PATH}",
                connect_args={"check_same_thread": False},
            )
            try:
                Base.metadata.create_all(
                    sync_engine,
                    tables=[
                        ConversationEntity.__table__,
                        MessageEntity.__table__,
                        TaskEntity.__table__,
                    ],
                )
            finally:
                sync_engine.dispose()
            logger.info(
                "[database] Path C: synced rebuild of messages/tasks/conversations OK"
            )
        except Exception:
            # 重建失败不能吞——后续所有读 messages/tasks 的测试都会 no such table。
            # 降级日志（debug）保留 traceback，便于诊断；不 raise 以免阻断 import
            # （init_db 还有机会 create_all 兜底）。
            logger.debug(
                "[database] Path C: messages/tasks/conversations sync rebuild failed",
                exc_info=True,
            )
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

    Path C (single-chat entity split, strict rename ``group_id`` →
    ``conversation_id`` on Message/Task): development-period data is
    disposable, so we drop the ``messages`` / ``tasks`` tables when their
    schema no longer matches (presence of a ``group_id`` column signals the
    pre-rename schema). ``create_all`` then rebuilds them with the new
    ``conversation_id`` column. A fresh DB simply creates them new. This
    mirrors the user's拍板决策「开发期数据可弃直接 drop+recreate 表，不写迁移脚本」.
    """
    from store.crud import load_active_provider_into_cache
    from store.entities import Base
    from store.seed import seed_demo_data

    # Path C: if messages/tasks still have the legacy ``group_id`` column, drop
    # them so create_all rebuilds with ``conversation_id``. Development-period
    # data is disposable (user 拍板: 不写迁移脚本).
    try:
        conn = sqlite3.connect(str(DB_PATH))
        msgs_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "group_id" in msgs_cols and "conversation_id" not in msgs_cols:
            conn.execute("DROP TABLE IF EXISTS messages")
            conn.execute("DROP TABLE IF EXISTS tasks")
            conn.commit()
        conn.close()
    except Exception:
        logger.debug("[database] Path C drop/recreate migration failed", exc_info=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await seed_demo_data(SessionLocal)
    # Load the active LLM provider into the sync cache so get_config() returns
    # the DB-backed config (not the env fallback) from the very first call.
    await load_active_provider_into_cache()
