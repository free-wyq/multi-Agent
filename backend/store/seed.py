"""Seed demo data on first run (when the agents table is empty).

Mirrors the M1 mock.py seed so verification steps and the front-end demo
experience stay identical.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from store.entities import (
    AgentEntity,
    GroupEntity,
    MemberEntity,
    MessageEntity,
    SkillEntity,
    TaskEntity,
)

logger = logging.getLogger("multi-agent.seed")

_SEED_TS = "2026-07-10T00:00:00Z"

# T86 豆包式常驻助手：全平台唯一一个 platform_assistant agent。
# 侧栏【会话】列的所有会话都绑它；智能体广场点 agent 是「体验对话」（transient=1）。
PLATFORM_ASSISTANT_ID = "agent_platform_assistant"
PLATFORM_ASSISTANT_SLUG = "platform_assistant"


async def seed_demo_data(SessionLocal: async_sessionmaker) -> None:
    """Insert demo rows if and only if no agents exist yet."""
    async with SessionLocal() as db:
        existing = (
            await db.execute(select(AgentEntity).limit(1))
        ).scalars().first()
        if existing is not None:
            return

        # ── builtin skills (PRD SK-09: platform ships a skill library) ──
        skills = [
            SkillEntity(
                id="skill_code_review",
                name="代码审查",
                description="对代码进行质量、安全、可维护性审查，给出改进建议",
                source="builtin",
                installed=1,
                content=(
                    "# 代码审查技能\n\n## 用途\n审查代码变更，发现缺陷与改进点。\n\n"
                    "## 使用步骤\n1. 阅读目标代码文件\n2. 逐项检查：命名、边界、错误处理、性能\n"
                    "3. 输出结构化审查清单（严重/建议/可选）\n"
                ),
                tags=["质量", "审查"],
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
            SkillEntity(
                id="skill_api_doc",
                name="API 文档生成",
                description="根据 API 代码自动生成接口文档",
                source="builtin",
                installed=1,
                content=(
                    "# API 文档生成技能\n\n## 用途\n从 FastAPI/路由代码生成接口文档。\n\n"
                    "## 使用步骤\n1. 读取路由定义\n2. 提取路径/方法/参数/响应\n"
                    "3. 输出 Markdown 接口说明\n"
                ),
                tags=["文档", "API"],
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
            SkillEntity(
                id="skill_test_gen",
                name="测试用例生成",
                description="根据函数签名与实现生成单元测试用例",
                source="builtin",
                installed=1,
                content=(
                    "# 测试用例生成技能\n\n## 用途\n为目标函数生成 pytest 单元测试。\n\n"
                    "## 使用步骤\n1. 读取目标函数\n2. 分析输入输出与边界\n"
                    "3. 生成正常/边界/异常用例\n"
                ),
                tags=["测试"],
                created_at=_SEED_TS,
                updated_at=_SEED_TS,
            ),
        ]
        db.add_all(skills)

        # ── agents ──
        agents = [
            AgentEntity(
                id="agent_coord_1",
                name="协调者",
                role="coordinator",
                system_prompt="你是群组协调者，负责需求分析与任务调度。",
                skills=["需求分析", "任务拆解", "调度"],
                extra_skills=["DAG 规划"],
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
                mounted_skills=["skill_api_doc"],
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
                mounted_skills=["skill_code_review", "skill_test_gen"],
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
            conversation_id="group_demo_1",
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
            conversation_id="group_demo_1",
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


async def ensure_platform_assistant(SessionLocal: async_sessionmaker) -> None:
    """T86 平台助手种子——启动幂等补种，让存量库也能拿到。

    与 ``seed_demo_data`` 的「首跑才种」不同：本函数每次启动都跑，按
    ``slug='platform_assistant'`` 查 AgentEntity，无则插入平台助手 row。
    这样存量库（已有 demo agents 但无平台助手）升级后也能立即用上
    「侧栏新建会话绑平台助手」语义，无需 drop 库。

    幂等：已有则跳过（不覆盖用户对该 agent 的任何编辑——name/system_prompt 等）。

    兼容存量库的死列：``allowed_tools`` / ``denied_tools`` 在实体已删（死代码清理 ·
    2026-07-27），但物理列可能仍残留在老库的 ``agents`` 表上（NOT NULL 约束）。
    本函数检测此情况并 DROP COLUMN，让 INSERT 平台助手不撞 NOT NULL 约束。
    """
    # 兼容老库：若 agents 表仍残留 allowed_tools / denied_tools 死列（NOT NULL），
    # 任何 INSERT 都会撞约束。先 DROP COLUMN 清掉（SQLAlchemy ORM 已不映射它们）。
    # SQLite 3.35+ 支持 DROP COLUMN；aiosqlite 跑的是 SQLite 3.37.2，OK。
    # try/except 兜住已不存在 / 不支持的情况，best-effort。
    async with SessionLocal() as db:
        for col in ("allowed_tools", "denied_tools"):
            try:
                cols = {
                    row[1]
                    for row in (await db.execute(text("PRAGMA table_info(agents)"))).all()
                }
                if col in cols:
                    await db.execute(text(f"ALTER TABLE agents DROP COLUMN {col}"))
                    await db.commit()
                    logger.info(
                        "[seed] dropped legacy column agents.%s (dead code, "
                        "NOT NULL was blocking T86 platform_assistant INSERT)",
                        col,
                    )
            except Exception:
                # best-effort：列可能已不存在 / SQLite 版本不支持 DROP COLUMN。
                # 不 raise——失败的话后续 INSERT 若撞约束会以 ValueError 上报。
                await db.rollback()

    async with SessionLocal() as db:
        existing = (
            await db.execute(
                select(AgentEntity).where(AgentEntity.slug == PLATFORM_ASSISTANT_SLUG)
            )
        ).scalars().first()
        if existing is not None:
            return
        platform = AgentEntity(
            id=PLATFORM_ASSISTANT_ID,
            name="平台助手",
            role="platform_assistant",
            system_prompt=(
                "你是平台助手，用户的常驻AI助手。帮助用户处理各类问题，"
                "回答简洁友好，需要时主动追问澄清需求。"
            ),
            skills=[],
            extra_skills=[],
            mounted_skills=[],
            startup_strategy="",
            model="",
            max_turns=0,
            description="平台常驻助手，轻度用户的默认对话伙伴",
            slug=PLATFORM_ASSISTANT_SLUG,
            icon_emoji=None,
            created_at=_SEED_TS,
            updated_at=_SEED_TS,
        )
        db.add(platform)
        await db.commit()
        logger.info(
            "[seed] platform_assistant inserted (id=%s) — 侧栏会话默认绑此 agent",
            PLATFORM_ASSISTANT_ID,
        )
