"""Group + member + file routes (M2: SQLite-backed via store.crud).

Routes map to frontend `groupApi`:
  GET    /api/groups                          → list_groups
  POST   /api/groups/generate-name            → generate_group_name_desc (MT-04)
  GET    /api/groups/{id}                     → get_group
  POST   /api/groups                          → create_group     (body = GroupCreatePayload)
  PUT    /api/groups/{id}                     → update_group     (body = partial)
  DELETE /api/groups/{id}                     → delete_group
  GET    /api/groups/{groupId}/members        → group_list_members   (flat: +agent_name/role)
  POST   /api/groups/{groupId}/members        → group_add_member     (body = {agentId, alias?})
  DELETE /api/groups/{groupId}/members/{mid}  → group_remove_member
  GET    /api/groups/{groupId}/files           → group_list_files
  GET    /api/groups/{groupId}/files/{name}   → download_file       (PL-12 artifact download)
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engine.registry import registry
from engine.workspace import safe_path
from events import emit_coordinator_plan, emit_message_added
from llm import build_group_name_desc_prompt, chat_completion, extract_json, get_llm_config
from models import Group, GroupCreatePayload, GroupFile, GroupMember
from store import crud

logger = logging.getLogger("multi-agent.groups")

router = APIRouter(prefix="/api/groups", tags=["groups"])


class AddMemberBody(BaseModel):
    agentId: str
    alias: str | None = None


class GroupUpdateBody(BaseModel):
    """Partial-update body for PUT /api/groups/{id}.

    All fields optional so a caller can patch a single attribute (e.g. just
    ``config.leader_strategy`` from the group settings Modal) without resupplying
    the required ``name``. Mirrors ``plan._GroupConfigUpdate``: ``config`` is a
    free-form dict (extra="allow") that ``crud.update_group`` merges key-wise
    rather than wholesale-replacing, so partial config writes co-exist with
    other config keys (auto_confirm ↔ leader_strategy).
    """

    model_config = {"extra": "allow"}

    name: str | None = None
    coordinator_id: str | None = None
    description: str | None = None
    member_ids: list[str] | None = None
    config: dict[str, Any] | None = None
    status: str | None = None


class GenerateNameDescBody(BaseModel):
    """MT-04: body for POST /api/groups/generate-name.

    ``coordinator_id`` + ``member_ids`` identify the prospective team; the
    endpoint resolves their names/roles and asks the LLM to synthesize a
    project-style team name + one-line description. All optional so a brand-new
    team with only a coordinator (no members yet) still gets a suggestion.
    """

    coordinator_id: str | None = None
    member_ids: list[str] = []


async def _generate_group_name_desc(
    coordinator_id: str | None, member_ids: list[str]
) -> dict:
    """Call the LLM to synthesize a team name + description from the roster (MT-04).

    Resolves coordinator + members to ``(name, role)`` tuples, builds the prompt
    via ``build_group_name_desc_prompt``, calls the LLM, and parses strict JSON
    into ``{name, description}``. Falls back to a roster-derived name + empty
    description if the LLM call or JSON parse fails (mirrors
    ``_generate_agent_via_llm`` / ``_generate_skill_via_llm`` — never raises so
    the create-group flow always gets a usable suggestion).

    The fallback name concatenates member names so it is at least descriptive
    of who's on the team even without LLM help.
    """
    members_full: list[tuple[str, str, str]] = []
    coord_full: tuple[str, str, str] | None = None

    if coordinator_id:
        coord = await crud.get_agent(coordinator_id)
        if coord:
            coord_full = (coord.id, coord.name, coord.role)

    for aid in member_ids or []:
        a = await crud.get_agent(aid)
        if a:
            members_full.append((a.id, a.name, a.role))

    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            [{"role": "user", "content": build_group_name_desc_prompt(coord_full, members_full)}],
        )
        parsed = extract_json(raw)
    except Exception as exc:
        logger.warning("[groups] generate-name LLM failed: %s", exc)
        parsed = None

    if not parsed:
        # fallback: roster-derived name (coordinator name + "团队")
        roster_names = (
            ([coord_full[1]] if coord_full else []) + [m[1] for m in members_full]
        )
        base = "、".join(roster_names) if roster_names else "新团队"
        return {"name": f"{base}团队"[:64], "description": ""}

    name = str(parsed.get("name") or "").strip()
    if not name:
        roster_names = (
            ([coord_full[1]] if coord_full else []) + [m[1] for m in members_full]
        )
        name = f"{'、'.join(roster_names) if roster_names else '新团队'}团队"
    description = str(parsed.get("description") or "").strip()
    return {"name": name[:64], "description": description[:200]}


@router.get("")
async def list_groups() -> list[Group]:
    return await crud.list_groups()


@router.post("/generate-name")
async def generate_group_name_desc(body: GenerateNameDescBody) -> dict:
    """MT-04: synthesize a team name + description from a prospective roster.

    ``POST /api/groups/generate-name`` body ``{coordinator_id?, member_ids?}`` →
    resolves the agents' names/roles → LLM generates a project-style team name
    + one-line description → returns ``{name, description}`` (never raises:
    LLM failure falls back to a roster-derived name). The create-group flow
    calls this after the user picks a coordinator + members, then prefills the
    name/description fields so the user can review before creating — the
    suggestion is advisory, the user can still edit.

    Declared before ``GET /{group_id}`` because ``generate-name`` is a literal
    segment that must match before the ``{group_id}`` path param would swallow
    it as ``group_id="generate-name"`` (same precedence rule as ``/templates``).
    """
    return await _generate_group_name_desc(body.coordinator_id, body.member_ids)


@router.get("/{group_id}")
async def get_group(group_id: str) -> Group | None:
    return await crud.get_group(group_id)


@router.post("")
async def create_group(payload: GroupCreatePayload) -> Group:
    return await crud.create_group(payload)


@router.put("/{group_id}")
async def update_group(group_id: str, payload: GroupUpdateBody) -> Group | None:
    """Partial-update a group (name/description/coordinator_id/config/status).

    MT-06: editing ``coordinator_id`` (换 Leader) requires the new Leader to already
    be a member of the group — the coordinator is selected from the roster, not
    invented. If the requested ``coordinator_id`` is neither the current Leader nor
    an existing member, the update is rejected with 409 (rather than silently
    promoting a non-member to Leader, which would leave the group routing to an
    agent that has no member row). The Leader swap does not touch membership: the
    old Leader stays as an ordinary member, the new Leader's member row (if any) is
    untouched. Use add/remove member to change the roster itself.

    B11 pending-restart 文档化（换群主不重建驻留引擎）：此 PUT 只落 DB 行的
    ``coordinator_id``，不触发 ``registry`` 重建该群驻留引擎。引擎身份层
    (``is_coordinator`` / ``graph_kind`` / ``coordinator_id``) 在启动烘焙
    (``AgentEngine.__init__``) 时落定、生命周期内不再变——见 ``AgentEngine`` 类
    docstring 的时效口径契约。换群主的后果：(1) ``route_user_message`` /
    ``route_plan_resume`` 等入站路由每条消息 ``crud.get_group`` 现读新群主 id，
    把用户消息/计划确认 notify 推给新群主引擎；(2) 但新群主引擎仍跑建群时烘焙的
    worker 图（当时是成员），其 ``_handle_notify`` 走 worker 分支而非 coordinator
    图调度——即「能收信但不调度」；(3) 老群主的 coordinator 图被现读路由旁路闲置，
    其驻留 ``_dispatch_plan`` 状态保留但不再被入站消息唤醒。完整换群主生效需进程
    重启（``load_from_store`` 按新 ``coordinator_id`` 重建图）或解散群组重建。
    有意分层：图身份 ≠ 消息级配置，本端点不擅自重建引擎（重建须停 inbox、作废
    checkpointer 线程的 dispatch_plan/interrupt 状态、迁移驻留对话记忆，高风险未做）。
    """
    new_coord = payload.coordinator_id
    if new_coord is not None:
        cur = await crud.get_group(group_id)
        if cur is None:
            return None
        if new_coord != cur.coordinator_id:
            members = await crud.list_group_members_with_agent(group_id)
            member_ids = {m.agent_id for m in members}
            if new_coord not in member_ids:
                raise HTTPException(
                    status_code=409,
                    detail="新群主必须是该群组的现有成员，请先将该智能体添加为成员再设为群主",
                )
    return await crud.update_group(group_id, payload)


@router.delete("/{group_id}")
async def delete_group(group_id: str) -> bool:
    """Delete a group (解散团队): cascade members/tasks/messages, then stop engines.

    MT-07: deleting a team must also tear down its resident engines — otherwise
    the engines keep running their inbox loops, holding references to the deleted
    group and leaking until process shutdown. ``registry.stop_group`` stops every
    engine in the group (cancels run loop, unregisters inbox, emits offline status)
    and returns the count; the DB cascade (members/tasks/messages) happens in
    ``crud.delete_group`` regardless. Returns ``True`` once the group row + its
    engines are gone.
    """
    await registry.stop_group(group_id)
    return await crud.delete_group(group_id)


@router.get("/{group_id}/members")
async def list_members(group_id: str) -> list[GroupMember]:
    return await crud.list_group_members_with_agent(group_id)


@router.post("/{group_id}/members")
async def add_member(group_id: str, body: AddMemberBody) -> GroupMember | None:
    """Add an agent to the group as a member.

    MT-06: adding an agent that is already a member (or is the coordinator) violates the
    ``uq_group_agent`` unique constraint — instead of surfacing a raw 500, detect it and
    return 409 with a readable message. The route (not crud) owns this because the
    constraint violation is a data-level condition best explained at the API boundary.
    """
    # coordinator_id is a 1:1 leadership pointer, not a member row — but conceptually
    # the Leader is "in the group", so reject re-adding the coordinator too.
    cur = await crud.get_group(group_id)
    if cur is not None and cur.coordinator_id == body.agentId:
        raise HTTPException(status_code=409, detail="该智能体已是群主")
    existing = await crud.list_group_members_with_agent(group_id)
    if any(m.agent_id == body.agentId for m in existing):
        raise HTTPException(status_code=409, detail="该智能体已在群组中")
    return await crud.add_member(group_id, body.agentId, body.alias)


@router.delete("/{group_id}/members/{member_id}")
async def remove_member(group_id: str, member_id: str) -> bool:
    return await crud.remove_member(group_id, member_id)


@router.post("/{group_id}/reset-session")
async def reset_session(group_id: str) -> dict[str, Any]:
    """BE-02: clear a group's conversation + resident engine instance state.

    ``POST /api/groups/{id}/reset-session`` is the backend of the ``/new`` slash
    command — a fresh-conversation reset without disbanding the team. It does
    three things, in order:

    1. **Persisted messages** — ``crud.clear_messages_by_group`` wipes the
       ``messages`` rows for the group (same store used by ``DELETE
       /api/messages``). The frontend also calls ``messageApi.clearByGroup``
       for its optimistic local clear; the server clear is the authoritative
       one and makes the reset survive a reload.
    2. **Resident engine state** — ``registry.reset_group_session`` clears
       ``_memory`` / ``_dispatch_plan`` / ``_recent_routes`` / ``_pending_tasks``
       on every engine in the group (coordinator + workers). This is the
       方案 B in-memory plan/memory wipe: the engines stay live (compiled
       LangGraph graph + inbox channel + run loop untouched) but their
       cross-invoke state is empty, so the next user message starts a clean
       conversation. If any engine is mid-execution it is cancelled first
       (see ``AgentEngine.reset_session``).
    3. **Bus event** — emits an empty ``coordinator_plan`` (``plan: []``) so
       any connected client (GroupPage / MonitorPage / the new ChatPanel)
       drops its resident plan card immediately — without this, a stale
       ``pending`` plan would linger on the UI until a reload.

    Returns ``{"ok": true, "group_id": ..., "messages_cleared": <bool>,
    "engines_reset": <count>}``. Never raises on a cold/unknown group: a group
    that was never started has no engines (``engines_reset=0``) but its
    messages are still cleared if any existed.
    """
    # 1. persisted messages (authoritative — survives reload)
    cleared = await crud.clear_messages_by_group(group_id)
    # 2. resident engine instance state (memory + dispatch plan)
    result = await registry.reset_group_session(group_id)
    # 3. push an empty plan so connected clients drop the plan card
    group = await crud.get_group(group_id)
    coord_id = group.coordinator_id if group else ""
    await emit_coordinator_plan(group_id, coord_id, [])
    return {
        "ok": True,
        "group_id": group_id,
        "messages_cleared": cleared,
        "engines_reset": result.get("reset", 0),
    }


@router.get("/{group_id}/files")
async def list_files(group_id: str) -> list[GroupFile]:
    return await crud.list_files(group_id)


@router.get("/{group_id}/files/{file_name:path}")
async def download_file(group_id: str, file_name: str) -> FileResponse:
    """PL-12: download a workspace artifact file by name.

    ``file_name`` is resolved against the group workspace via ``safe_path`` so a
    path-traversal attempt (e.g. ``../../etc/passwd``) is rejected before any
    file is opened — the resolved path must live inside the workspace root.
    A 404 is returned when the file does not exist (the task may not have
    produced it, or it was cleared). The MIME type is guessed from the
    extension (defaulting to ``application/octet-stream``) so browsers render
    text/HTML/images inline where appropriate; ``filename`` is set so the
    browser saves it under the original basename.

    The ``{file_name:path}`` converter captures the rest of the URL *including
    slashes*, so a sub-directory artifact recorded by
    ``scan_workspace_artifacts`` as a POSIX-relative ``path``
    (e.g. ``login-api/index.js``) is delivered as a single parameter. A leading
    slash is stripped defensively so a client that joined ``group_id + "/" +
    name`` with a stray slash still resolves.
    """
    rel = file_name
    # strip a leading slash so "/sub/f.md" (defensive client join) works
    if rel.startswith("/"):
        rel = rel.lstrip("/")
    try:
        target = safe_path(group_id, rel)
    except ValueError as exc:
        # path escaped the workspace root — refuse rather than serving anything
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"file not found: {file_name}",
        )

    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
    )
