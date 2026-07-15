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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import (
    AgentDefinition,
    Group,
    GroupFile,
    GroupMember,
    LlmProvider,
    McpConnection,
    Message,
    ScheduledTask,
    ScheduledTaskCreatePayload,
    ScheduledTaskRun,
    Skill,
    Task,
)
from store.entities import (
    AgentEntity,
    GroupEntity,
    LlmProviderEntity,
    McpConnectionEntity,
    MemberEntity,
    MessageEntity,
    ScheduledTaskEntity,
    ScheduledTaskRunEntity,
    SkillEntity,
    TaskEntity,
)

_PREFIX_MAP = {
    "agent": "agent_",
    "group": "group_",
    "member": "member_",
    "task": "task_",
    "msg": "msg_",
    "skill": "skill_",
    "mcp": "mcp_",
    "sched": "sched_",
    "schedrun": "schedrun_",
    "provider": "prov_",
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
            "mounted_skills": a.mounted_skills or [],
            "mounted_mcp": a.mounted_mcp or [],
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
        mounted_skills=list(getattr(payload, "mounted_skills", []) or []),
        mounted_mcp=list(getattr(payload, "mounted_mcp", []) or []),
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
    coord_id = payload.coordinator_id or ""
    # MT-02 自动指定 Leader：未指定 coordinator_id 时，自动选一个群主——
    # 优先 role=coordinator 的 agent（协调者角色最贴合），否则退化为 agent 列表
    # 首个（保证群组至少有一个可路由的 Leader，避免空 coordinator_id 的坏群）。
    # 仅在创建时填充落库；用户仍可在群设置里改 coordinator_id（update_group）。
    # 指定路径（payload.coordinator_id 非空）原样透传，行为不变。
    if not coord_id:
        agents = await list_agents()
        coord = next((a for a in agents if a.role == "coordinator"), None)
        if coord is None and agents:
            coord = agents[0]
        if coord is not None:
            coord_id = coord.id
    entity = GroupEntity(
        id=_next_id("group"),
        name=payload.name,
        coordinator_id=coord_id,
        description=payload.description,
        status="active",
        # 透传 payload.config（single_chat 等群组级标记）。后端 GroupCreatePayload 用
        # extra="allow" 容纳未声明字段，前端单聊建群时传 {single_chat:true} 落库后供
        # 左栏区分单聊群（不显示在多智能体列表）。未传 config 时为 None（默认）。
        config=payload.config if hasattr(payload, "config") else None,
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
        # MT-06: changing the Leader (coordinator_id) does NOT auto-add/drop members —
        # the new Leader must already be a member (enforced in the route layer) and the
        # old Leader stays as an ordinary member. This keeps membership and leadership as
        # two orthogonal concerns: editing leader_strategy / coordinator_id / name only
        # flips metadata, while add/remove member governs the roster.
        for k, v in data.items():
            if k == "member_ids":
                continue
            if k == "config":
                # MT-03: config is a partial update — merge the new keys into the
                # existing config dict rather than wholesale-replacing it. The
                # group settings Modal writes leader_strategy this way, and the
                # plan-direct API writes auto_confirm; a wholesale replace would
                # let one writer clobber the other's key (e.g. saving a Leader
                # strategy would drop a previously-set auto_confirm). Merge keeps
                # config as a single additive container for both keys.
                merged = dict(row.config or {})
                merged.update(v or {})
                setattr(row, k, merged)
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


async def set_task_artifact(
    task_id: str,
    artifact_path: str | None,
    artifact: dict | None,
) -> Task | None:
    """PL-12: record a completed task's workspace artifacts.

    Updates only ``artifact_path`` (the primary file's workspace-relative path)
    and ``artifact`` (the structured manifest) — a targeted update that does not
    touch status/exit_code/result_summary, which were already finalized by the
    engine before scanning. Used by ``AgentEngine._run_worker_task`` after a
    worker task completes, so the task card / download entry can surface what
    the worker produced.

    Returns the updated Task, or ``None`` if the task_id is unknown (the task
    may not be persisted — e.g. coordinator-only synthetic tasks — in which
    case there is simply no row to update and the scan result is discarded).
    """
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(TaskEntity, task_id)
        if not row:
            return None
        row.artifact_path = artifact_path
        row.artifact = artifact
        await db.commit()
        await db.refresh(row)
        return _task_to_model(row)


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


# ── Skill helpers ───────────────────────────────────────────────


def _skill_to_model(s: SkillEntity, mounted_to: list[str] | None = None) -> Skill:
    # assets（阶段三·task33）：运行时从文件系统扫描，不落 DB。旧 content-only 技能
    # 无 assets 目录 → list_skill_assets 返 []。import 在函数内避免循环依赖。
    from store import skill_assets

    return Skill.model_validate(
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "source": s.source,
            "installed": bool(s.installed),
            "content": s.content,
            "tags": s.tags or [],
            # frontmatter（阶段一地基2）：旧行经 _migrate_schema 已 ALTER 出空 list 列，
            # 这里兜底 None→[] 防御性补齐（实体 default=list 但 SQLite ALTER 出来的列
            # 在极旧库上可能短暂为 None，做一次或运算保险）。
            "requires_tools": s.requires_tools or [],
            "triggers": s.triggers or [],
            "outputs": s.outputs or [],
            "mounted_to": mounted_to or [],
            "assets": skill_assets.list_skill_assets(s.id),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
    )


async def _skill_mount_map() -> dict[str, list[str]]:
    """Build skill_id -> [agent_id,...] by scanning every agent's mounted_skills.

    Mounting is stored on the agent (mounted_skills list), not on the skill, so
    reverse-lookup is an in-memory scan of the agents table. Skill count stays
    small so this is cheap.
    """
    from store.database import SessionLocal

    out: dict[str, list[str]] = {}
    async with SessionLocal() as db:
        rows = (await db.execute(select(AgentEntity))).scalars().all()
        for a in rows:
            for sid in a.mounted_skills or []:
                out.setdefault(sid, []).append(a.id)
    return out


async def list_skills() -> list[Skill]:
    from store.database import SessionLocal

    mount_map = await _skill_mount_map()
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(SkillEntity).order_by(SkillEntity.created_at))
        ).scalars().all()
        return [_skill_to_model(r, mount_map.get(r.id, [])) for r in rows]


async def get_skill(skill_id: str) -> Skill | None:
    from store.database import SessionLocal

    mount_map = await _skill_mount_map()
    async with SessionLocal() as db:
        row = await db.get(SkillEntity, skill_id)
        return _skill_to_model(row, mount_map.get(skill_id, [])) if row else None


async def create_skill(payload: Any) -> Skill:
    from store.database import SessionLocal

    ts = _now_iso()
    entity = SkillEntity(
        id=_next_id("skill"),
        name=payload.name,
        description=payload.description or "",
        content=payload.content or "",
        source=getattr(payload, "source", "custom") or "custom",
        installed=1,
        tags=list(getattr(payload, "tags", []) or []),
        # frontmatter（阶段一地基2）：从 payload 取，缺省空 list（SkillCreatePayload 已默认 []）
        requires_tools=list(getattr(payload, "requires_tools", []) or []),
        triggers=list(getattr(payload, "triggers", []) or []),
        outputs=list(getattr(payload, "outputs", []) or []),
        created_at=ts,
        updated_at=ts,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _skill_to_model(entity)


async def update_skill(skill_id: str, payload: Any) -> Skill | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(SkillEntity, skill_id)
        if not row:
            return None
        for k, v in data.items():
            if k in ("name", "description", "content", "source", "installed", "tags",
                     "requires_tools", "triggers", "outputs"):
                setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return await get_skill(skill_id)


async def delete_skill(skill_id: str) -> bool:
    from store.database import SessionLocal
    from store import skill_assets

    async with SessionLocal() as db:
        row = await db.get(SkillEntity, skill_id)
        if not row:
            return False
        # detach from any agent that has it mounted
        agents = (
            await db.execute(select(AgentEntity).where(AgentEntity.mounted_skills.contains(skill_id)))
        ).scalars().all()
        for a in agents:
            a.mounted_skills = [s for s in (a.mounted_skills or []) if s != skill_id]
        await db.delete(row)
        await db.commit()
    # 阶段三·task33：DB 行删掉后清磁盘资产目录（scripts/templates）。best-effort：
    # rmtree 失败只 log 不 raise，不阻塞 DB 删除（skill_assets.delete_skill_assets 已兜底）。
    skill_assets.delete_skill_assets(skill_id)
    return True


async def mount_skill(agent_id: str, skill_id: str) -> AgentDefinition | None:
    """Append skill_id to the agent's mounted_skills (idempotent, PRD AG-08)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        agent = await db.get(AgentEntity, agent_id)
        if not agent:
            return None
        skill = await db.get(SkillEntity, skill_id)
        if not skill:
            return None
        mounted = list(agent.mounted_skills or [])
        if skill_id not in mounted:
            mounted.append(skill_id)
            agent.mounted_skills = mounted
            agent.updated_at = _now_iso()
            await db.commit()
            await db.refresh(agent)
    return await get_agent(agent_id)


async def unmount_skill(agent_id: str, skill_id: str) -> AgentDefinition | None:
    """Remove skill_id from the agent's mounted_skills (PRD AG-09)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        agent = await db.get(AgentEntity, agent_id)
        if not agent:
            return None
        mounted = [s for s in (agent.mounted_skills or []) if s != skill_id]
        agent.mounted_skills = mounted
        agent.updated_at = _now_iso()
        await db.commit()
        await db.refresh(agent)
    return await get_agent(agent_id)


async def resolve_skill_contents(skill_ids: list[str]) -> list[str]:
    """Resolve a list of mounted skill ids to their content strings.

    Used by the worker executor to inject mounted-skill content into the system
    prompt (PL-06). Missing ids are skipped silently.

    This is the **full-content** path (stage 1 injection). Stage 2 progressive
    disclosure splits this into a manifest (name+description, cheap, always-on)
    + full load (on demand) — see ``resolve_skill_manifest`` /
    ``resolve_skill_full`` below; this function is kept as the legacy/兜底 full
    injection path.
    """
    from store.database import SessionLocal

    if not skill_ids:
        return []
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(SkillEntity).where(SkillEntity.id.in_(skill_ids)))
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        out: list[str] = []
        for sid in skill_ids:
            row = by_id.get(sid)
            if row and row.content:
                out.append(row.content)
        return out


async def resolve_skill_manifest(skill_ids: list[str]) -> list[dict]:
    """Resolve mounted skill ids to a lightweight manifest (stage 2 progressive disclosure).

    Returns ``[{id, name, description, requires_tools, triggers, outputs}, ...]``
    — only metadata, no ``content``. This is what gets常驻拼进 the system prompt
    so the worker knows *which* skills it has and can decide which one to load
    fully on demand. Cheap: O(n) rows, small payload, no big content blobs.

    Missing ids are skipped silently (same lenient contract as
    ``resolve_skill_contents``).
    """
    from store.database import SessionLocal

    if not skill_ids:
        return []
    async with SessionLocal() as db:
        rows = (
            await db.execute(select(SkillEntity).where(SkillEntity.id.in_(skill_ids)))
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        out: list[dict] = []
        for sid in skill_ids:
            row = by_id.get(sid)
            if not row:
                continue
            out.append({
                "id": row.id,
                "name": row.name,
                "description": row.description or "",
                "requires_tools": row.requires_tools or [],
                "triggers": row.triggers or [],
                "outputs": row.outputs or [],
            })
        return out


async def resolve_skill_full(skill_id: str) -> str | None:
    """Load a single skill's full content on demand (stage 2 progressive disclosure).

    Called by the worker brain when it decides a specific skill is needed — the
    manifest (常驻) tells it what skills exist; this fetches one skill's full
    ``content`` to inject. Returns ``None`` for missing/empty skills (caller
    skips silently, same lenient contract).
    """
    from store.database import SessionLocal

    if not skill_id:
        return None
    async with SessionLocal() as db:
        row = await db.get(SkillEntity, skill_id)
        return row.content if (row and row.content) else None


# ── MCP connection helpers ──────────────────────────────────────


def _mcp_to_model(m: McpConnectionEntity) -> McpConnection:
    return McpConnection.model_validate(
        {
            "id": m.id,
            "name": m.name,
            "transport": m.transport,
            "command": m.command,
            "args": m.args or [],
            "env": m.env,
            "url": m.url,
            "headers": m.headers,
            "enabled": bool(m.enabled),
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }
    )


def _mcp_connection_config(m: McpConnectionEntity) -> dict:
    """Build a langchain-mcp-adapters connection dict from an entity.

    stdio: {transport, command, args, env?}
    sse:   {transport, url, headers?}
    """
    if m.transport == "sse":
        cfg: dict = {"transport": "sse", "url": m.url}
        if m.headers:
            cfg["headers"] = m.headers
        return cfg
    cfg = {"transport": "stdio", "command": m.command, "args": list(m.args or [])}
    if m.env:
        cfg["env"] = m.env
    return cfg


async def list_mcp_connections() -> list[McpConnection]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(McpConnectionEntity).order_by(McpConnectionEntity.created_at)
            )
        ).scalars().all()
        return [_mcp_to_model(r) for r in rows]


async def get_mcp_connection(mcp_id: str) -> McpConnection | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(McpConnectionEntity, mcp_id)
        return _mcp_to_model(row) if row else None


async def create_mcp_connection(payload: Any) -> McpConnection:
    from store.database import SessionLocal

    ts = _now_iso()
    entity = McpConnectionEntity(
        id=_next_id("mcp"),
        name=payload.name,
        transport=payload.transport or "stdio",
        command=payload.command or "",
        args=list(payload.args or []),
        env=payload.env,
        url=payload.url or "",
        headers=payload.headers,
        enabled=1 if getattr(payload, "enabled", True) else 0,
        created_at=ts,
        updated_at=ts,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _mcp_to_model(entity)


async def update_mcp_connection(mcp_id: str, payload: Any) -> McpConnection | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(McpConnectionEntity, mcp_id)
        if not row:
            return None
        for k, v in data.items():
            if k in ("name", "transport", "command", "args", "env", "url", "headers", "enabled"):
                setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return await get_mcp_connection(mcp_id)


async def set_mcp_enabled(mcp_id: str, enabled: bool) -> McpConnection | None:
    """Toggle a connection's enabled state (PRD MC-03 启用/禁用)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(McpConnectionEntity, mcp_id)
        if not row:
            return None
        row.enabled = 1 if enabled else 0
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return await get_mcp_connection(mcp_id)


async def delete_mcp_connection(mcp_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(McpConnectionEntity, mcp_id)
        if not row:
            return False
        # detach from any agent that has it mounted (MC-04 cleanup)
        agents = (
            await db.execute(select(AgentEntity).where(AgentEntity.mounted_mcp.contains(mcp_id)))
        ).scalars().all()
        for a in agents:
            a.mounted_mcp = [s for s in (a.mounted_mcp or []) if s != mcp_id]
        await db.delete(row)
        await db.commit()
        return True


async def mount_mcp(agent_id: str, mcp_id: str) -> AgentDefinition | None:
    """Append mcp_id to the agent's mounted_mcp (PRD MC-06)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        agent = await db.get(AgentEntity, agent_id)
        if not agent:
            return None
        mcp = await db.get(McpConnectionEntity, mcp_id)
        if not mcp:
            return None
        mounted = list(agent.mounted_mcp or [])
        if mcp_id not in mounted:
            mounted.append(mcp_id)
            agent.mounted_mcp = mounted
            agent.updated_at = _now_iso()
            await db.commit()
            await db.refresh(agent)
    return await get_agent(agent_id)


async def unmount_mcp(agent_id: str, mcp_id: str) -> AgentDefinition | None:
    """Remove mcp_id from the agent's mounted_mcp."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        agent = await db.get(AgentEntity, agent_id)
        if not agent:
            return None
        mounted = [s for s in (agent.mounted_mcp or []) if s != mcp_id]
        agent.mounted_mcp = mounted
        agent.updated_at = _now_iso()
        await db.commit()
        await db.refresh(agent)
    return await get_agent(agent_id)


async def resolve_mcp_configs(mcp_ids: list[str]) -> list[tuple[str, dict]]:
    """Resolve mounted mcp ids to (id, connection_config) for enabled ones.

    Used by the worker executor to build a MultiServerMCPClient and load tools
    (PL-07). Disabled connections are skipped so toggling off a server
    immediately removes its tools from the agent.
    """
    from store.database import SessionLocal

    if not mcp_ids:
        return []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(McpConnectionEntity).where(McpConnectionEntity.id.in_(mcp_ids))
            )
        ).scalars().all()
        by_id = {r.id: r for r in rows}
        out: list[tuple[str, dict]] = []
        for mid in mcp_ids:
            row = by_id.get(mid)
            if row and row.enabled:
                out.append((row.name, _mcp_connection_config(row)))
        return out



# ── Scheduled task helpers ─────────────────────────────────────


def _sched_to_model(s: ScheduledTaskEntity) -> ScheduledTask:
    return ScheduledTask.model_validate(
        {
            "id": s.id,
            "name": s.name,
            "content": s.content,
            "agent_id": s.agent_id,
            "group_id": s.group_id,
            "schedule_type": s.schedule_type,
            "cron": s.cron,
            "interval_seconds": s.interval_seconds,
            "run_at": s.run_at,
            "enabled": bool(s.enabled),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }
    )


def _schedrun_to_model(r: ScheduledTaskRunEntity) -> ScheduledTaskRun:
    return ScheduledTaskRun.model_validate(
        {
            "id": r.id,
            "scheduled_task_id": r.scheduled_task_id,
            "status": r.status,
            "result": r.result,
            "started_at": r.started_at,
            "finished_at": r.finished_at or "",
        }
    )


async def list_scheduled_tasks() -> list[ScheduledTask]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ScheduledTaskEntity).order_by(ScheduledTaskEntity.created_at)
            )
        ).scalars().all()
        return [_sched_to_model(r) for r in rows]


async def get_scheduled_task(task_id: str) -> ScheduledTask | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(ScheduledTaskEntity, task_id)
        return _sched_to_model(row) if row else None


async def create_scheduled_task(payload: Any) -> ScheduledTask:
    from store.database import SessionLocal

    ts = _now_iso()
    entity = ScheduledTaskEntity(
        id=_next_id("sched"),
        name=payload.name,
        content=payload.content,
        agent_id=payload.agent_id,
        group_id=payload.group_id,
        schedule_type=payload.schedule_type or "interval",
        cron=payload.cron or "",
        interval_seconds=int(payload.interval_seconds or 0),
        run_at=payload.run_at or "",
        enabled=1 if getattr(payload, "enabled", True) else 0,
        created_at=ts,
        updated_at=ts,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _sched_to_model(entity)


async def update_scheduled_task(task_id: str, payload: Any) -> ScheduledTask | None:
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(ScheduledTaskEntity, task_id)
        if not row:
            return None
        for k, v in data.items():
            if k in ("name", "content", "agent_id", "group_id", "schedule_type",
                     "cron", "interval_seconds", "run_at", "enabled"):
                setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return await get_scheduled_task(task_id)


async def set_scheduled_task_enabled(task_id: str, enabled: bool) -> ScheduledTask | None:
    """Toggle a scheduled task's enabled state (PRD TM-05 暂停/恢复)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(ScheduledTaskEntity, task_id)
        if not row:
            return None
        row.enabled = 1 if enabled else 0
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return await get_scheduled_task(task_id)


async def delete_scheduled_task(task_id: str) -> bool:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(ScheduledTaskEntity, task_id)
        if not row:
            return False
        # cascade: delete run history too
        await db.execute(
            delete(ScheduledTaskRunEntity).where(
                ScheduledTaskRunEntity.scheduled_task_id == task_id
            )
        )
        await db.delete(row)
        await db.commit()
        return True


async def list_scheduled_task_runs(task_id: str, limit: int = 50) -> list[ScheduledTaskRun]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        stmt = (
            select(ScheduledTaskRunEntity)
            .where(ScheduledTaskRunEntity.scheduled_task_id == task_id)
            .order_by(ScheduledTaskRunEntity.started_at.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [_schedrun_to_model(r) for r in rows]


async def create_scheduled_task_run(task_id: str) -> ScheduledTaskRun:
    """Insert a 'running' run record and return it (TM-07)."""
    from store.database import SessionLocal

    entity = ScheduledTaskRunEntity(
        id=_next_id("schedrun"),
        scheduled_task_id=task_id,
        status="running",
        started_at=_now_iso(),
        finished_at=None,
    )
    async with SessionLocal() as db:
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _schedrun_to_model(entity)


async def finish_scheduled_task_run(
    run_id: str, success: bool, result: str
) -> ScheduledTaskRun | None:
    """Mark a run finished with success/failed + result (TM-07)."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(ScheduledTaskRunEntity, run_id)
        if not row:
            return None
        row.status = "success" if success else "failed"
        row.result = result[:2000] if result else None
        row.finished_at = _now_iso()
        await db.commit()
        await db.refresh(row)
        return _schedrun_to_model(row)


# ── LLM Provider helpers ────────────────────────────────────────


def _provider_to_model(p: LlmProviderEntity) -> LlmProvider:
    """Map ORM row to masked Pydantic LlmProvider.

    The raw ``api_key`` is masked via ``config._mask_key`` — the model's
    ``api_key`` field carries a preview (first 3 + last 3 chars), NEVER the
    raw secret. ``has_key`` lets the UI show configured status without the key.

    Multi-model catalog: ``models`` is the provider's list of LlmModel entries
    (capability metadata per model); the 6 connection-level fields
    (``api_version``/``organization``/``extra_headers``/``request_timeout``/
    ``max_retries``/``proxy``) are passed through so the UI can render the
    full connection config. The flat ``model`` column is output as the
    *resolved* active model (via :func:`_select_model`) — NOT the raw legacy
    column — so the UI card shows the model the engine actually uses
    (catalog ``is_default`` wins over the legacy column; a stale legacy value
    left by ``PUT /api/config`` no longer mislabels the card).
    """
    import config as _config

    raw_key = p.api_key or ""
    # `model` 输出解析后的生效模型（_select_model：is_default → 匹配 model 列 →
    # catalog 首个 → model 列 → 默认），而非原始 legacy `model` 列。legacy 列是
    # 编辑器不暴露的死字段（PUT /api/config 热切换会写它，但生效仍走 catalog
    # is_default），原样吐给前端会导致「卡片显示的 model ≠ 后端实际生效的 model」
    # （用户实测：卡片显 deepseek-v4-flash，实际生效 kimi-k2.6）。输出解析值让
    # 「看着生效的就是生效的」，legacy 列退回纯内部 fallback 用。
    return LlmProvider.model_validate(
        {
            "id": p.id,
            "name": p.name,
            "provider": p.provider,
            "model": _select_model(p),
            "base_url": p.base_url,
            "api_key": _config._mask_key(raw_key),
            "has_key": bool(raw_key),
            "temperature": p.temperature,
            "max_tokens": p.max_tokens,
            "is_active": bool(p.is_active),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            # Multi-model catalog (provider owns N models, one is_default).
            "models": p.models or [],
            # Connection-level config (applies to the endpoint, shared by all models).
            "api_version": p.api_version or "",
            "organization": p.organization or "",
            "extra_headers": p.extra_headers,
            "request_timeout": p.request_timeout,
            "max_retries": p.max_retries,
            "proxy": p.proxy or "",
        }
    )


def _provider_to_cache_dict(p: LlmProviderEntity) -> dict[str, Any]:
    """Build the raw config dict (with raw api_key) for ``config.set_active_cache``.

    INTERNAL only — never returned over HTTP. The raw key is needed so the
    engine can actually authenticate to the provider.

    Outputs the full 13-key cache shape (6 legacy + ``models`` catalog + 6
    connection-level fields). The active ``model`` is resolved via
    :func:`_select_model` so the cache reflects the catalog's is_default
    entry (not just the legacy flat ``model`` column) — e.g. a provider
    whose catalog marks ``deepseek-reasoner`` as default will run that model
    even if the legacy ``model`` column still holds ``deepseek-chat``.
    ``set_active_cache`` re-normalizes, so passing the entity's raw values
    here (with None/empty tolerated) is safe.
    """
    return {
        # Legacy flat config.
        "provider": p.provider,
        "model": _select_model(p),
        "base_url": p.base_url,
        "api_key": p.api_key or "",
        "temperature": p.temperature,
        "max_tokens": p.max_tokens,
        # Multi-model catalog (list of LlmModel-shaped dicts; [] = legacy).
        "models": p.models or [],
        # Connection-level config (applies to the endpoint, shared by all models).
        "api_version": p.api_version or "",
        "organization": p.organization or "",
        "extra_headers": p.extra_headers,
        "request_timeout": p.request_timeout,
        "max_retries": p.max_retries,
        "proxy": p.proxy or "",
    }


def _select_model(p: LlmProviderEntity) -> str:
    """Resolve the active ``model_id`` for a provider entity (single source).

    Delegates to :func:`config.select_active_model` so the 5-level fallback
    chain (is_default → match ``entity.model`` → first catalog entry →
    legacy ``model`` column → ``_DEFAULT_MODEL``) lives in exactly one place.
    ``entity.models`` is the JSON-column list of LlmModel-shaped dicts (may be
    ``[]`` for a legacy / unmigrated row → falls back to ``entity.model``).

    Returns the model id string that should be sent to the upstream
    ``/chat/completions`` ``model`` field. Used by ``_provider_to_cache_dict``
    (to set the cache's active model) and any code that needs to know which
    model a provider will actually run.
    """
    import config

    return config.select_active_model(
        {
            "model": p.model,
            "models": p.models or [],
        }
    )


def _migrate_legacy_models(row: LlmProviderEntity) -> None:
    """Seed a single is_default model entry when ``row.models`` is empty.

    Legacy provider rows (created before the multi-model catalog) have an
    empty ``models`` JSON list and rely on the flat ``model`` column. This
    back-fills a one-entry catalog from that column so the UI / engine see a
    consistent ``models`` list (the active model is the seeded entry, marked
    ``is_default=True``). Idempotent: a non-empty ``models`` list is left
    untouched (the user has explicitly configured a catalog). A row whose
    ``model`` column is also empty gets an empty catalog (caller falls back
    to ``config._DEFAULT_MODEL``).

    Called from ``create_provider`` (when the payload omits ``models``) and
    ``update_provider`` (same) so neither path needs to special-case the
    legacy column. Mutates ``row.models`` in place; the caller commits.
    """
    if row.models:
        return  # catalog already configured — don't clobber user data.
    legacy_model = (row.model or "").strip()
    if not legacy_model:
        # No model info at all — leave the catalog empty; _select_model falls
        # back to config._DEFAULT_MODEL.
        row.models = []
        return
    row.models = [
        {
            "model_id": legacy_model,
            "display_name": legacy_model,
            "context_window": 0,
            "supports_function_calling": True,
            "supports_vision": False,
            "supports_streaming": True,
            "is_default": True,
        }
    ]


async def list_providers() -> list[LlmProvider]:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(LlmProviderEntity).order_by(LlmProviderEntity.created_at)
            )
        ).scalars().all()
        return [_provider_to_model(r) for r in rows]


async def get_provider(provider_id: str) -> LlmProvider | None:
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(LlmProviderEntity, provider_id)
        return _provider_to_model(row) if row else None


async def get_provider_entity(provider_id: str) -> LlmProviderEntity | None:
    """Return the ORM row of a provider by id (raw api_key intact).

    INTERNAL use only — probe.test_provider / fetch_models need the raw key
    to actually authenticate to the upstream. NOT for HTTP output (use
    ``get_provider`` for that, which masks). Mirrors
    ``get_active_provider_entity`` but for an explicit id rather than the
    active row.
    """
    from store.database import SessionLocal

    async with SessionLocal() as db:
        return await db.get(LlmProviderEntity, provider_id)


async def get_active_provider_entity() -> LlmProviderEntity | None:
    """Return the ORM row of the active provider (raw api_key intact).

    INTERNAL use only — cache loader + routes that need the raw key to
    persist. NOT for HTTP output (use ``get_provider`` for that, which masks).
    """
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(LlmProviderEntity).where(LlmProviderEntity.is_active == 1)
            )
        ).scalars().first()
        return row


async def _deactivate_all(db: AsyncSession) -> None:
    """Set is_active=0 on all provider rows (single-active invariant)."""
    rows = (
        await db.execute(select(LlmProviderEntity).where(LlmProviderEntity.is_active == 1))
    ).scalars().all()
    for r in rows:
        r.is_active = 0


async def create_provider(payload: Any) -> LlmProvider:
    """Insert a new provider. If ``is_active`` is True, deactivate all others
    first (single-active invariant). Returns the masked model.

    Multi-model catalog: ``payload.models`` is persisted as the JSON ``models``
    column (list of LlmModel-shaped dicts). When the payload omits ``models``
    (``None``), the catalog is seeded from the legacy ``model`` column via
    :func:`_migrate_legacy_models` so a legacy-style create (model only, no
    catalog) still yields a one-entry catalog. The 6 connection-level fields
    default to their entity defaults when the payload omits them.
    """
    from store.database import SessionLocal

    ts = _now_iso()
    # Normalize payload.models → list[dict]. None (omitted) → defer to
    # _migrate_legacy_models (seed from `model`); [] (explicit empty) → keep
    # empty catalog (user intends no catalog yet).
    raw_models = getattr(payload, "models", None)
    if raw_models is None:
        models_value: list = []  # placeholder; _migrate_legacy_models fills it
        seed_from_legacy = True
    else:
        # Pydantic LlmModel instances → dict for JSON column storage.
        models_value = [
            m.model_dump() if hasattr(m, "model_dump") else dict(m)
            for m in raw_models
        ]
        seed_from_legacy = False

    entity = LlmProviderEntity(
        id=_next_id("provider"),
        name=payload.name,
        provider=payload.provider or "openai",
        model=payload.model or "",
        base_url=payload.base_url or "",
        api_key=payload.api_key or "",
        temperature=float(payload.temperature if payload.temperature is not None else 0.0),
        max_tokens=int(payload.max_tokens if payload.max_tokens is not None else 4096),
        # Multi-model catalog + connection-level config (defaults via entity
        # mapped_column when payload omits them; `or ""`/`or 0.0` coerces
        # None → default to match the NOT NULL columns).
        models=models_value,
        api_version=payload.api_version or "",
        organization=payload.organization or "",
        extra_headers=payload.extra_headers,
        request_timeout=float(payload.request_timeout if payload.request_timeout is not None else 120.0),
        max_retries=int(payload.max_retries if payload.max_retries is not None else 2),
        proxy=payload.proxy or "",
        is_active=1 if getattr(payload, "is_active", False) else 0,
        created_at=ts,
        updated_at=ts,
    )
    # payload omitted models → seed a single is_default entry from the legacy
    # `model` column so the catalog is never empty on a legacy-style create.
    if seed_from_legacy:
        _migrate_legacy_models(entity)
    async with SessionLocal() as db:
        if entity.is_active:
            await _deactivate_all(db)
        db.add(entity)
        await db.commit()
        await db.refresh(entity)
    return _provider_to_model(entity)


async def update_provider(provider_id: str, payload: Any) -> LlmProvider | None:
    """Update whitelisted fields on a provider. ``api_key`` empty/None means
    "leave unchanged" (so editing other fields doesn't wipe the stored key).
    If ``is_active`` is set True, deactivate all others. Returns masked model.

    Multi-model catalog: ``models`` (when present in the payload) is persisted
    as the JSON column after enforcing the single-default invariant — at most
    one entry may be ``is_default=True``; if multiple are marked default, only
    the first is kept and the rest are demoted (so the catalog is never in an
    inconsistent state). ``models=None`` (omitted) leaves the catalog unchanged;
    ``models=[]`` (explicit empty) clears it. The 6 connection-level fields are
    whitelisted alongside the legacy fields.

    The legacy ``model`` column is NOT written by this path — the active model
    is owned by the catalog's ``is_default`` entry (see ``_select_model``). The
    frontend payload still carries ``model`` for backward-compat, but writing it
    here would split state (legacy column ≠ catalog default), and the output
    mapper (``_provider_to_model``) now returns the resolved active model, so a
    stale legacy column would otherwise be invisible-but-present. ``PUT /api/config``
    (``update_provider_model``) still writes the legacy column for the hot-switch
    path; that's the only writer now.
    """
    from store.database import SessionLocal

    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    async with SessionLocal() as db:
        row = await db.get(LlmProviderEntity, provider_id)
        if not row:
            return None
        for k, v in data.items():
            if k == "api_key":
                # empty/None → leave unchanged (don't clobber the stored key)
                if v:
                    row.api_key = v
                continue
            if k in ("name", "provider", "base_url", "temperature", "max_tokens"):
                setattr(row, k, v)
            elif k == "model":
                # 旧扁平 `model` 列不再由前端驱动——生效模型一律由 catalog 的
                # is_default 决定（_select_model）。前端 payload 仍带 model 字段
                # （LlmProviderCreatePayload 保留它向后兼容），但写入它会制造
                # 「legacy 列 ≠ catalog 默认」的分裂态：_provider_to_model 现输出
                # 解析后的生效模型，但 update_provider_model（PUT /api/config 热切换）
                # 仍会写 legacy 列——保留该列供热切换用，只是 ProviderEditor 保存时
                # 不再写它（避免编辑保存把 legacy 列刷成与 catalog 默认不一致的值）。
                continue
            elif k == "is_active":
                if v and not row.is_active:
                    await _deactivate_all(db)
                    row.is_active = 1
                elif not v:
                    row.is_active = 0
            elif k == "models":
                # Normalize to list[dict] (LlmModel → dict for JSON storage).
                norm = [
                    m.model_dump() if hasattr(m, "model_dump") else dict(m)
                    for m in v
                ]
                # Single-default invariant: at most one is_default=True. If the
                # payload marks several, keep the first True and demote the rest
                # rather than rejecting — the catalog stays usable and the UI
                # gets a consistent state (a hard reject would lose the user's
                # other edits). Empty catalog ([]) is allowed (legacy mode).
                seen_default = False
                for entry in norm:
                    if entry.get("is_default"):
                        if seen_default:
                            entry["is_default"] = False
                        else:
                            seen_default = True
                row.models = norm
            elif k in ("api_version", "organization", "proxy"):
                setattr(row, k, v or "")
            elif k == "extra_headers":
                setattr(row, k, v)  # None = "do not attach" (nullable column)
            elif k in ("request_timeout", "max_retries"):
                setattr(row, k, v)
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return _provider_to_model(row)


async def delete_provider(provider_id: str) -> tuple[bool, bool]:
    """Delete a provider. Returns (deleted, reassigned).

    If the deleted provider was the active one, pick the first remaining
    provider and mark it active (so there's always an active provider if any
    exists). ``reassigned`` is True when this reassignment happened, so the
    caller knows to refresh the cache from the new active.
    """
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(LlmProviderEntity, provider_id)
        if not row:
            return (False, False)
        was_active = bool(row.is_active)
        await db.delete(row)
        await db.commit()
        reassigned = False
        if was_active:
            # pick first remaining by created_at
            remaining = (
                await db.execute(
                    select(LlmProviderEntity).order_by(LlmProviderEntity.created_at)
                )
            ).scalars().first()
            if remaining:
                remaining.is_active = 1
                remaining.updated_at = _now_iso()
                await db.commit()
                reassigned = True
        return (True, reassigned)


async def set_active_provider(provider_id: str) -> LlmProvider | None:
    """Set all is_active=0, target is_active=1, commit. Returns masked model."""
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(LlmProviderEntity, provider_id)
        if not row:
            return None
        await _deactivate_all(db)
        row.is_active = 1
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
    return _provider_to_model(row)


async def update_provider_model(provider_id: str, model: str) -> LlmProviderEntity | None:
    """Targeted update of just the ``model`` column on a provider.

    Used by ``PUT /api/config`` hot-switch so the model change persists to the
    active provider in DB (not just the cache). Returns the ORM row (raw key
    intact) so the caller can refresh the cache, or None if not found.
    """
    from store.database import SessionLocal

    async with SessionLocal() as db:
        row = await db.get(LlmProviderEntity, provider_id)
        if not row:
            return None
        row.model = model
        row.updated_at = _now_iso()
        await db.commit()
        await db.refresh(row)
        return row


async def load_active_provider_into_cache() -> None:
    """Populate ``config._ACTIVE_CACHE`` from the DB-backed active provider.

    Called from ``init_db`` at startup. If an active provider row exists, its
    raw config (with the real api_key) is loaded into the cache so the sync
    ``get_config()`` path returns it. If NO provider row exists at all (first
    run on an existing install), seed one from env (preserving the .env-driven
    behavior) and cache it.
    """
    import config

    from store.database import SessionLocal

    active = await get_active_provider_entity()
    if active:
        config.set_active_cache(_provider_to_cache_dict(active))
        return

    # No active provider — check if any provider row exists at all.
    async with SessionLocal() as db:
        any_row = (
            await db.execute(select(LlmProviderEntity).limit(1))
        ).scalars().first()
        if any_row:
            # rows exist but none active — activate the first one
            any_row.is_active = 1
            any_row.updated_at = _now_iso()
            await db.commit()
            await db.refresh(any_row)
            config.set_active_cache(_provider_to_cache_dict(any_row))
            return

        # No provider row at all — seed one from env (preserves .env behavior)
        import os

        env_model = os.environ.get("LLM_MODEL", "glm-5.1")
        ts = _now_iso()
        seeded = LlmProviderEntity(
            id=_next_id("provider"),
            name="默认",
            provider=os.environ.get("LLM_PROVIDER", "openai"),
            model=env_model,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", ""),
            temperature=0.0,
            max_tokens=4096,
            # Seed a single-entry catalog from the env model so the cache/UI see
            # a consistent models list (is_default entry = the env model). This
            # mirrors create_provider's legacy-seed path and keeps the env-seeded
            # provider indistinguishable from a user-created one.
            models=[],
            # Connection-level config: env seed has none — emit the entity defaults
            # (empty strings / None / 120.0 / 2) so _provider_to_cache_dict
            # produces a complete 13-key cache.
            api_version="",
            organization="",
            extra_headers=None,
            request_timeout=120.0,
            max_retries=2,
            proxy="",
            is_active=1,
            created_at=ts,
            updated_at=ts,
        )
        # Seed the is_default catalog entry from the env model (same logic as
        # create_provider's legacy path — _migrate_legacy_models fills models
        # from the `model` column when the catalog is empty).
        _migrate_legacy_models(seeded)
        db.add(seeded)
        await db.commit()
        await db.refresh(seeded)
        config.set_active_cache(_provider_to_cache_dict(seeded))
