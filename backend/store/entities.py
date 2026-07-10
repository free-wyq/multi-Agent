"""ORM entities for the five core tables.

Column names are snake_case and match the Rust serde shapes and the frontend
TS interfaces exactly (verified against src-tauri/src/core/types.rs). JSON
columns use sqlalchemy.JSON so Python reads/writes native list/dict. Timestamps
are stored as ISO8601 strings (front-end expects strings, not DateTime types).

Note on Message.type: the database column is named `type`; the ORM attribute is
`type_` to avoid shadowing the Python builtin. Serialization to the frontend
uses the key `type` (handled in crud.py by aliasing on the Pydantic model).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AgentEntity(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default="")
    system_prompt: Mapped[str] = mapped_column(String, nullable=False, default="")
    skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    extra_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    denied_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    startup_strategy: Mapped[str] = mapped_column(String, nullable=False, default="")
    model: Mapped[str] = mapped_column(String, nullable=False, default="")
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata_", JSON, nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class GroupEntity(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    coordinator_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)


class MemberEntity(Base):
    __tablename__ = "members"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    alias: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)

    __table_args__ = (
        UniqueConstraint("group_id", "agent_id", name="uq_group_agent"),
    )


class TaskEntity(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="submitted")
    assigned_agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dependencies: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    artifact_path: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    dag_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class MessageEntity(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_id: Mapped[str] = mapped_column(String, nullable=False)
    receiver_id: Mapped[str] = mapped_column(String, nullable=False)
    # Column is "type" in DB; ORM attr is type_ to avoid builtin clash.
    type_: Mapped[str] = mapped_column("type", String, nullable=False, default="agent_reply")
    content: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=_now_iso, index=True)
