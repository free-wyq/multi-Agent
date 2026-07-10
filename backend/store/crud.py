"""Async CRUD for the five core entities.

Every function opens its own session (async with SessionLocal()) so the route
layer doesn't have to thread a db dependency through. Each returns Pydantic
models (not ORM objects): model_validate is used to convert. The Message ORM
attribute `type_` is remapped to the key `type` when building the dict so the
frontend receives the expected field name.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from models import (
    AgentDefinition,
    Group,
    GroupFile,
    GroupMember,
    Message,
    Task,
)
from store.entities import (
    AgentEntity,
    GroupEntity,
    MemberEntity,
    MessageEntity,
    TaskEntity,
)

_PREFIX_MAP = {
    "agent": "agent_",
    "group": "group_",
    "member": "member_",
    "task": "task_",
    "msg": "msg_",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_id(prefix: str) -> str:
    """Generate a prefixed id like the Rust `new_id` (prefix + uuid hex)."""
    p = _PREFIX_MAP.get(prefix, f"{prefix}_")
    return f"{p}{uuid.uuid4().hex}"


# ── Agent helpers ────────────────────────────────────────────────

def _agent_to_model(a: AgentEntity) -> AgentDefinition:
    return AgentDefinition.model_validate(
        {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "system_prompt": a.system_prompt,
            "skills": a.skills or [],
            "extra_skills": a.extra_skills or [],
            "allowed_tools": a.allowed_tools or [],
            "denied_tools": a.denied_tools or [],
            "startup_strategy": a.startup_strategy,
            "model": a.model,
            "max_turns": a.max_turns,
            "description": a.description,
            "metadata_": a.metadata_,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }
    )


async def list_agents() -> list[AgentDefinition]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        rows = (await db.execute(select(AgentEntity).order_by(AgentEntity.created_at))).scalars().all()
        return [_agent_to_model(r) for r in rows]


async def get_agent(agent_id: str) -> AgentDefinition | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(AgentEntity, agent_id)
        return _agent_to_model(row) if row else None


async def create_agent(payload: Any) -> AgentDefinition:
    from store.database import SessionLocal

    ts = _now_iso()
    entity = AgentEntity(
        id=_next_id("agent"),
        name=payload.name,
        role=payload.role,
        system_prompt=payload.system_prompt or "",
        skills=list(getattr(payload, "skills", []) or []),
        extra_skills=list(getattr(payload, "extra_skills", []) or []),
        allowed_tools=[],
        denied_tools=[],
        startup_strategy="",
        model="",
        max_turns=0,
        description=payload.description,
        created_at=ts,
        updated_at=ts,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _agent_to_model(entity)


async def update_agent(agent_id: str, payload: Any) -> AgentDefinition | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(AgentEntity, agent_id)
        if not row:
            return None
        for k, v in data.items():
            # front-end may send metadata_ explicitly
            setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
        return _agent_to_model(row)


async def delete_agent(agent_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(AgentEntity, agent_id)
        if not row:
            return False
        await db.delete(row)
        await db.commit()
        return True


# ── Group helpers ────────────────────────────────────────────────

def _group_to_model(g: GroupEntity) -> Group:
    return Group.model_validate(
        {
            "id": g.id,
            "name": g.name,
            "coordinator_id": g.coordinator_id,
            "description": g.description,
            "status": g.status,
            "config": g.config,
            "created_at": g.created_at,
            "updated_at": g.updated_at,
        }
    )


async def list_groups() -> list[Group]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        rows = (await db.execute(select(GroupEntity).order_by(GroupEntity.created_at))).scalars().all()
        return [_group_to_model(r) for r in rows]


async def get_group(group_id: str) -> Group | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(GroupEntity, group_id)
        return _group_to_model(row) if row else None


async def create_group(payload: Any) -> Group:
    from store.database import SessionLocal

    ts = _now_iso()
    entity = GroupEntity(
        id=_next_id("group"),
        name=payload.name,
        coordinator_id=payload.coordinator_id or "",
        description=payload.description,
        status="active",
        created_at=ts,
        updated_at=ts,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
        group = _group_to_model(entity)
        # eagerly add members requested in payload.member_ids
        for aid in payload.member_ids or []:
            await _add_member_inner(db, entity.id, aid, None)
        await db.commit()
    return group


async def update_group(group_id: str, payload: Any) -> Group | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(GroupEntity, group_id)
        if not row:
            return None
        for k, v in data.items():
            if k == "member_ids":
                continue
            setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
        return _group_to_model(row)


async def delete_group(group_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(GroupEntity, group_id)
        if not row:
            return False
        # cascade: remove members + tasks + messages of this group
        await db.execute(delete(MemberEntity).where(MemberEntity.group_id == group_id))
        await db.execute(delete(TaskEntity).where(TaskEntity.group_id == group_id))
        await db.execute(delete(MessageEntity).where(MessageEntity.group_id == group_id))
        await db.delete(row)
        await db.commit()
        return True


# ── Member helpers ───────────────────────────────────────────────

def _member_row_to_model(m: MemberEntity, agent_name: str, agent_role: str) -> GroupMember:
    return GroupMember.model_validate(
        {
            "id": m.id,
            "group_id": m.group_id,
            "agent_id": m.agent_id,
            "alias": m.alias,
            "joined_at": m.joined_at,
            "agent_name": agent_name,
            "agent_role": agent_role,
        }
    )


async def list_group_members_with_agent(group_id: str) -> list[GroupMember]:
    """Flat join MemberEntity + AgentEntity: returns GroupMember with agent_name/agent_role."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = (
            select(MemberEntity, AgentEntity)
            .join(AgentEntity, MemberEntity.agent_id == AgentEntity.id)
            .where(MemberEntity.group_id == group_id)
            .order_by(MemberEntity.joined_at)
        )
        rows = (await db.execute(stmt)).all()
        return [_member_row_to_model(m, a.name, a.role) for m, a in rows]


async def _add_member_inner(db, group_id: str, agent_id: str, alias: str | None) -> GroupMember | None:
    """Inner helper that reuses the given session. Returns the flat GroupMember or None if agent missing."""
    agent = await db.get(AgentEntity, agent_id)
    if not agent:
        return None
    member = MemberEntity(
        id=_next_id("member"),
        group_id=group_id,
        agent_id=agent_id,
        alias=alias,
        joined_at=_now_iso(),
    )
    db.add(member)
    await db.flush()
    return _member_row_to_model(member, agent.name, agent.role)


async def add_member(group_id: str, agent_id: str, alias: str | None = None) -> GroupMember | None:
    """Insert a member row after checking the agent exists. Returns the flat GroupMember."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        result = await _add_member_inner(db, group_id, agent_id, alias)
        if result is None:
            return None
        await db.commit()
        return result


async def remove_member(group_id: str, member_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(MemberEntity, member_id)
        if not row or row.group_id != group_id:
            return False
        await db.delete(row)
        await db.commit()
        return True


# ── Task helpers ────────────────────────────────────────────────

def _task_to_model(t: TaskEntity) -> Task:
    return Task.model_validate(
        {
            "id": t.id,
            "group_id": t.group_id,
            "parent_task_id": t.parent_task_id,
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "assigned_agent_id": t.assigned_agent_id,
            "instance_id": t.instance_id,
            "dependencies": t.dependencies or [],
            "artifact_path": t.artifact_path,
            "artifact": t.artifact,
            "exit_code": t.exit_code,
            "error_message": t.error_message,
            "result_summary": t.result_summary,
            "dag_order": t.dag_order,
            "created_at": t.created_at,
            "started_at": t.started_at,
            "completed_at": t.completed_at,
        }
    )


async def list_tasks(group_id: str | None = None) -> list[Task]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = select(TaskEntity).order_by(TaskEntity.created_at)
        if group_id:
            stmt = stmt.where(TaskEntity.group_id == group_id)
        rows = (await db.execute(stmt)).scalars().all()
        return [_task_to_model(r) for r in rows]


async def list_ready_tasks(group_id: str | None = None) -> list[Task]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = select(TaskEntity).where(TaskEntity.status == "submitted").order_by(TaskEntity.created_at)
        if group_id:
            stmt = stmt.where(TaskEntity.group_id == group_id)
        rows = (await db.execute(stmt)).scalars().all()
        return [_task_to_model(r) for r in rows]


async def get_task(task_id: str) -> Task | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(TaskEntity, task_id)
        return _task_to_model(row) if row else None


async def create_task(payload: Any) -> Task:
    from store.database import SessionLocal

    entity = TaskEntity(
        id=_next_id("task"),
        group_id=payload.group_id,
        parent_task_id=None,
        title=payload.title,
        description=payload.description,
        status="submitted",
        assigned_agent_id=payload.assigned_agent_id,
        instance_id=None,
        dependencies=list(payload.dependencies or []),
        artifact_path=None,
        artifact=None,
        exit_code=None,
        error_message=None,
        result_summary=None,
        dag_order=payload.dag_order,
        created_at=_now_iso(),
        started_at=None,
        completed_at=None,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _task_to_model(entity)


async def update_task(task_id: str, payload: Any) -> Task | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(TaskEntity, task_id)
        if not row:
            return None
        for k, v in data.items():
            setattr(row, k, v)
        await db.commit()
        await db.refresh(row)
        return _task_to_model(row)


async def delete_task(task_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(TaskEntity, task_id)
        if not row:
            return False
        await db.delete(row)
        await db.commit()
        return True


# ── Message helpers ─────────────────────────────────────────────

def _message_to_model(m: MessageEntity) -> Message:
    """Map ORM row to Pydantic Message. The ORM attr type_ maps to the key `type`."""
    return Message.model_validate(
        {
            "id": m.id,
            "group_id": m.group_id,
            "task_id": m.task_id,
            "sender_id": m.sender_id,
            "receiver_id": m.receiver_id,
            "type": m.type_,
            "content": m.content,
            "data": m.data,
            "created_at": m.created_at,
        }
    )


async def list_messages(group_id: str | None = None, limit: int = 100) -> list[Message]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = select(MessageEntity).order_by(MessageEntity.created_at)
        if group_id:
            stmt = stmt.where(MessageEntity.group_id == group_id)
        rows = (await db.execute(stmt)).scalars().all()
        msgs = [_message_to_model(r) for r in rows]
        return msgs[-limit:] if limit else msgs


async def list_messages_by_task(task_id: str, limit: int = 100) -> list[Message]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = (
            select(MessageEntity)
            .where(MessageEntity.task_id == task_id)
            .order_by(MessageEntity.created_at)
        )
        rows = (await db.execute(stmt)).scalars().all()
        msgs = [_message_to_model(r) for r in rows]
        return msgs[-limit:] if limit else msgs


async def create_message(payload: Any) -> Message:
    """Persist a message row. payload may be a Pydantic MessageCreatePayload or a dict."""
    from store.database import SessionLocal

    if hasattr(payload, "model_dump"):
        data = payload.model_dump(exclude_unset=False)
    else:
        data = dict(payload)

    entity = MessageEntity(
        id=_next_id("msg"),
        group_id=data["group_id"],
        task_id=data.get("task_id"),
        sender_id=data["sender_id"],
        receiver_id=data.get("receiver_id") or "broadcast",
        type_=data.get("type") or "user_input",
        content=data.get("content"),
        data=data.get("data"),
        created_at=_now_iso(),
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _message_to_model(entity)


async def clear_messages_by_group(group_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(delete(MessageEntity).where(MessageEntity.group_id == group_id))
        await db.commit()
        return (result.rowcount or 0) > 0


# ── Files (placeholder until M5) ────────────────────────────────

async def list_files(group_id: str) -> list[GroupFile]:
    """List files in the group's shared workspace (DATA_DIR/workspaces/{group_id}/).

    Returns top-level files with name/size/modified_at. Empty list if the
    workspace directory does not exist yet (no task has produced artifacts).
    """
    from engine.workspace import workspace_path

    ws = workspace_path(group_id)
    if not ws.exists():
        return []
    out: list[GroupFile] = []
    for entry in sorted(ws.iterdir(), key=lambda p: p.name):
        if entry.is_file():
            st = entry.stat()
            out.append(
                GroupFile(
                    name=entry.name,
                    size=st.st_size,
                    modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
            )
    return out

