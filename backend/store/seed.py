"""Seed demo data on first run (when the agents table is empty).

Mirrors the M1 mock.py seed so verification steps and the front-end demo
experience stay identical.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from store.entities import (
    AgentEntity,
    GroupEntity,
    MemberEntity,
    MessageEntity,
    TaskEntity,
)

_SEED_TS = "2026-07-10T00:00:00Z"


async def seed_demo_data(SessionLocal: async_sessionmaker) -> None:
    """Insert demo rows if and only if no agents exist yet."""
    async with SessionLocal() as db:
        existing = (
            await db.execute(select(AgentEntity).limit(1))
        ).scalars().first()
        if existing is not None:
            return

        # ── agents ──
        agents = [
            AgentEntity(
                id="agent_coord_1",
                name="协调者",
                role="coordinator",
                system_prompt="你是群组协调者，负责需求分析与任务调度。",
                skills=["需求分析", "任务拆解", "调度"],
                extra_skills=["DAG 规划"],
                allowed_tools=[],
                denied_tools=[],
                startup_strategy="",
                model="",
                max_turns=0,
                description="群组调度大脑",
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
            AgentEntity(
                id="agent_frontend_1",
                name="前端工程师",
                role="frontend_engineer",
                system_prompt="你是前端工程师，负责页面与组件开发。",
                skills=["React", "TypeScript", "CSS"],
                extra_skills=["Ant Design", "ReactFlow"],
                allowed_tools=[],
                denied_tools=[],
                startup_strategy="",
                model="",
                max_turns=0,
                description="前端开发智能体",
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
            AgentEntity(
                id="agent_backend_1",
                name="后端工程师",
                role="backend_engineer",
                system_prompt="你是后端工程师，负责 API 与数据层开发。",
                skills=["Python", "FastAPI", "SQL"],
                extra_skills=["LangGraph"],
                allowed_tools=[],
                denied_tools=[],
                startup_strategy="",
                model="",
                max_turns=0,
                description="后端开发智能体",
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
        ]
        db.add_all(agents)

        # ── group ──
        group = GroupEntity(
            id="group_demo_1",
            name="演示协作组",
            coordinator_id="agent_coord_1",
            description="用于端到端验证的演示群组",
            status="active",
            created_at=_SEED_TS,
            updated_at=_SEED_TS,
        )
        db.add(group)

        # ── members ──
        members = [
            MemberEntity(
                id="member_1",
                group_id="group_demo_1",
                agent_id="agent_coord_1",
                alias=None,
                joined_at=_SEED_TS,
            ),
            MemberEntity(
                id="member_2",
                group_id="group_demo_1",
                agent_id="agent_frontend_1",
                alias="前端",
                joined_at=_SEED_TS,
            ),
            MemberEntity(
                id="member_3",
                group_id="group_demo_1",
                agent_id="agent_backend_1",
                alias="后端",
                joined_at=_SEED_TS,
            ),
        ]
        db.add_all(members)

        # ── task ──
        task = TaskEntity(
            id="task_demo_1",
            group_id="group_demo_1",
            parent_task_id=None,
            title="搭建登录页面",
            description="实现登录表单与校验",
            status="submitted",
            assigned_agent_id="agent_frontend_1",
            instance_id=None,
            dependencies=[],
            artifact_path=None,
            artifact=None,
            exit_code=None,
            error_message=None,
            result_summary=None,
            dag_order=1,
            created_at=_SEED_TS,
            started_at=None,
            completed_at=None,
        )
        db.add(task)

        # ── message ──
        message = MessageEntity(
            id="msg_demo_1",
            group_id="group_demo_1",
            task_id=None,
            sender_id="user",
            receiver_id="broadcast",
            type_="user_input",
            content="你好，请帮我做一个登录功能",
            data=None,
            created_at=_SEED_TS,
        )
        db.add(message)

        await db.commit()
