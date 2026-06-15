"""
消息 API 路由

通讯模式（类微信）：
- 用户发消息 → HTTP 立即返回（消息已入库 + 已推到 WS）
- 协调者/子智能体回复 → 后台异步执行，完成后通过 WS 推送到前端
- 前端通过 WebSocket 实时接收所有新消息，无需轮询/刷新
"""
import asyncio
import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.message import MessageCreate, MessageResponse
from app.core.database import get_db, async_session
from app.services import message_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messages", tags=["消息"])


# ── 结构化输出 Schema ────────────────────────────────────────────


class TeamMemberMessage(BaseModel):
    """团队成员消息"""
    agent_id: str = Field(description="团队成员智能体 ID")
    content: str = Field(description="该成员发送的消息内容")


class TeamResponse(BaseModel):
    """团队回复（协调者 + 可选成员）"""
    coordinator_content: str = Field(description="协调者（群主）的回复内容")
    team_messages: list[TeamMemberMessage] = Field(
        default_factory=list,
        description="其他团队成员的消息列表",
    )


# ── 消息创建入口 ─────────────────────────────────────────────────


@router.post("", response_model=MessageResponse, status_code=201)
async def create_message(body: MessageCreate, db: AsyncSession = Depends(get_db)):
    """用户发消息入口 — 微信模式：HTTP 立即返回，回复通过 WS 推送"""
    obj = await message_service.create_message(db, body.model_dump())

    # 用户消息立即通过 WS 推送给前端
    await _push_to_bus(body.group_id, obj.id, body.sender_id, body.type, body.content or "")

    # 只对用户消息触发自动回复
    if body.sender_id == "user" and body.type != "coordinator_reply":
        # 检查是否 @了子智能体
        mentioned_agent_id = await _find_mention(db, body.group_id, body.content or "")

        # 后台异步处理回复，HTTP 立即返回
        asyncio.create_task(_background_reply(body.group_id, mentioned_agent_id, body.content or "", body.sender_id))

    return obj


# ── WS 推送 ──────────────────────────────────────────────────────


async def _push_to_bus(
    group_id: str,
    msg_id: str,
    sender_id: str,
    msg_type: str,
    content: str,
) -> None:
    """将消息推送到 Redis 消息总线，WS 客户端会实时收到"""
    from datetime import datetime, timezone
    from app.bus.core import get_bus, CHANNEL_PREFIX

    try:
        bus = get_bus()
        channel = f"{CHANNEL_PREFIX}{group_id}"
        message = {
            "id": msg_id,
            "group_id": group_id,
            "sender_id": sender_id,
            "receiver_id": "broadcast",
            "type": msg_type,
            "content": content,
            "task_id": None,
            "data": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await bus.publish(channel, message)
    except Exception as e:
        logger.warning("Failed to push message to bus: %s", e)


# ── 后台回复任务 ─────────────────────────────────────────────────


async def _background_reply(group_id: str, mentioned_agent_id: str | None) -> None:
    """后台异步生成回复并通过 WS 推送，不阻塞 HTTP 响应"""
    try:
        async with async_session() as db:
            if mentioned_agent_id:
                await _route_to_agent(db, group_id, mentioned_agent_id)
            else:
                await _coordinator_reply(db, group_id)
    except Exception as e:
        logger.error("Background reply failed: %s", e)


# ── @解析 ─────────────────────────────────────────────────────────


async def _find_mention(db: AsyncSession, group_id: str, content: str) -> str | None:
    """解析消息中 @的子智能体"""
    import sqlalchemy as sa
    from app.models.agent_definition import AgentDefinition
    from app.models.group_member import GroupMember

    mentions = re.findall(r"@(\S+)", content)
    if not mentions:
        return None

    mention = mentions[0]

    result = await db.execute(
        sa.select(GroupMember.agent_id, GroupMember.alias)
        .where(GroupMember.group_id == group_id)
    )
    members = {row[0]: row[1] for row in result.all()}
    if not members:
        return None

    if mention in members:
        return mention

    result = await db.execute(
        sa.select(AgentDefinition.id, AgentDefinition.name)
        .where(AgentDefinition.id.in_(list(members.keys())))
    )
    for agent_id, name in result.all():
        if mention == name:
            return agent_id

    for agent_id, alias in members.items():
        if alias and mention in alias:
            return agent_id

    return None


# ── 子智能体路由 ─────────────────────────────────────────────────


async def _route_to_agent(db: AsyncSession, group_id: str, agent_id: str, content: str = "", sender_id: str = "user") -> None:
    """路由到子智能体引擎"""
    logger.info("消息路由到子智能体: %s (from: %s)", agent_id[:8], sender_id)
    try:
        from app.agent_engine import get_registry
        registry = get_registry()
        routed = await registry.route_message(agent_id, {
            "type": "chat",
            "content": content,
            "sender_id": sender_id,
            "group_id": group_id,
        }, group_id=group_id)
        if not routed:
            logger.warning("AgentEngine 不在线，群主兜底")
            await _coordinator_reply(db, group_id)
    except ImportError:
        await _coordinator_reply(db, group_id)


# ── 协调者自动回复 ──────────────────────────────────────────────


async def _coordinator_reply(db: AsyncSession, group_id: str) -> None:
    """群主自动回复 — 后台执行，完成后通过 WS 推送"""
    import sqlalchemy as sa
    from app.models.agent_definition import AgentDefinition
    from app.models.group import Group
    from app.models.group_member import GroupMember
    from app.coordinator.llm import _get_llm

    # 获取最近用户消息
    recent_msgs = await message_service.list_messages_by_group(db, group_id, 3)
    user_message = ""
    for m in recent_msgs:
        if m.sender_id == "user" and m.content:
            user_message = m.content
            break
    if not user_message:
        return

    # 群主信息
    result = await db.execute(
        sa.select(Group.coordinator_id).where(Group.id == group_id)
    )
    row = result.one_or_none()
    if not row:
        return
    coordinator_id = row[0]

    result = await db.execute(
        sa.select(AgentDefinition.name, AgentDefinition.system_prompt)
        .where(AgentDefinition.id == coordinator_id)
    )
    coord_row = result.one_or_none()
    if not coord_row:
        return
    coord_name = coord_row[0]
    coord_prompt = (coord_row[1] or "")[:500]

    # 群成员
    result = await db.execute(
        sa.select(AgentDefinition.id, AgentDefinition.name, AgentDefinition.role)
        .join(GroupMember, GroupMember.agent_id == AgentDefinition.id)
        .where(GroupMember.group_id == group_id)
    )
    members = result.all()
    agent_names = {m.id: m.name for m in members}
    agent_names[coordinator_id] = coord_name

    all_recent = await message_service.list_messages_by_group(db, group_id, 8)
    chat_history = "\n".join(
        f"[{agent_names.get(m.sender_id, m.sender_id)}]: {m.content}"
        for m in reversed(all_recent) if m.content
    )

    try:
        llm = _get_llm(temperature=0.7)

        member_context = ""
        for m in members:
            member_context += f"- {m.name} (ID: {m.id}, 角色标识: {m.role})\n"

        prompt = (
            f"你是一个{coord_name}，角色描述：{coord_prompt}\n\n"
            f"你管理的团队成员（ID 必须精确使用）：\n{member_context}\n\n"
            f"群聊最近的对话历史：\n{chat_history}\n\n"
            f"---\n\n"
            f"用户刚刚发来消息：{user_message}\n\n"
            f"请以{coord_name}的身份回复用户。"
            f"如果用户提出的需求涉及具体开发任务，你应该分派给对应的团队成员。\n\n"
            f"请按以下 JSON 格式回复（不要加 markdown 代码块标记）：\n"
            f'{{\n'
            f'  "coordinator_content": "给用户的回复内容",\n'
            f'  "team_messages": [\n'
            f'    {{"agent_id": "成员ID", "content": "该成员的发言"}}\n'
            f'  ]\n'
            f'}}\n\n'
            f"要求：\n"
            f"1. coordinator_content 是你的回复\n"
            f"2. 如果涉及开发任务，让对应团队成员在群里确认接收\n"
            f"3. 如果只是日常对话，team_messages 留空数组 []\n"
            f"4. agent_id 必须使用上面提供的精确 ID\n"
            f"5. team_messages 最多2条"
        )

        reply_text = await llm.ainvoke(prompt)
        raw = reply_text.content if hasattr(reply_text, "content") else str(reply_text)

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = {"coordinator_content": raw, "team_messages": []}

        coord_content = data.get("coordinator_content", raw)
        await _save_and_push(db, group_id, coordinator_id, "coordinator_reply", coord_content)

        for tm in (data.get("team_messages") or []):
            aid = tm.get("agent_id", "")
            if aid not in agent_names:
                logger.warning("Unknown agent_id in team message: %s", aid)
                continue
            await _save_and_push(db, group_id, aid, "team_reply", tm.get("content", ""))

    except Exception as e:
        logger.warning("LLM auto-reply failed: %s", e)
        fallback = "收到你的消息，我来看看怎么安排。请详细描述需要完成的工作，我会协调团队成员来执行。"
        await _save_and_push(db, group_id, coordinator_id, "coordinator_reply", fallback)


async def _save_and_push(
    db: AsyncSession,
    group_id: str,
    sender_id: str,
    msg_type: str,
    content: str,
) -> None:
    """保存消息到 DB 并通过 WS 推送到前端"""
    msg_id = str(uuid.uuid4())
    await message_service.create_message(db, {
        "id": msg_id,
        "group_id": group_id,
        "sender_id": sender_id,
        "receiver_id": "broadcast",
        "type": msg_type,
        "content": content,
    })
    await db.commit()

    # 通过 bus → WS 推送
    await _push_to_bus(group_id, msg_id, sender_id, msg_type, content)


# ── 查询 ─────────────────────────────────────────────────────────


@router.get("/group/{group_id}", response_model=list[MessageResponse])
async def list_group_messages(group_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)):
    return await message_service.list_messages_by_group(db, group_id, limit)


@router.get("/task/{task_id}", response_model=list[MessageResponse])
async def list_task_messages(task_id: str, limit: int = 100, db: AsyncSession = Depends(get_db)):
    return await message_service.list_messages_by_task(db, task_id, limit)
