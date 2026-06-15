"""
智能体定义模型（持久层 — 定义态）

用户在 Web 页面创建的智能体配置，对应"第1层：定义态"。
同一个定义可以被多次实例化为 AgentInstance。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="智能体名称，如'前端工程师'")
    role: Mapped[str] = mapped_column(String(50), nullable=False, comment="角色标识，如'frontend-engineer'")

    # 角色 prompt → 容器内 CLAUDE.md 的内容来源
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, comment="角色系统提示词，映射到容器内 CLAUDE.md")

    # 技能：角色模板自动映射 + 技能市场可选挂载
    skills: Mapped[list] = mapped_column(ARRAY(String), default=list, comment="技能列表，角色模板自动映射")
    extra_skills: Mapped[list] = mapped_column(ARRAY(String), default=list, comment="从技能市场额外挂载的技能")

    # 镜像：统一 Ubuntu + Claude Code CLI，暂留字段备后续扩展
    base_image: Mapped[str] = mapped_column(String(200), default="agent-base:latest", comment="基础镜像")

    # 工具权限 → 容器内 settings.json 的内容来源
    allowed_tools: Mapped[list] = mapped_column(ARRAY(String), default=list, comment="允许使用的工具")
    denied_tools: Mapped[list] = mapped_column(ARRAY(String), default=list, comment="禁止使用的工具")

    # 启动策略：on_demand(默认) / pooled / always_on
    startup_strategy: Mapped[str] = mapped_column(String(20), default="on_demand", comment="启动策略")

    # LLM 参数（群主用，子智能体由 Claude Code 自带）
    model: Mapped[str] = mapped_column(String(100), default="claude-sonnet-4-6-20250514", comment="LLM 模型")
    max_turns: Mapped[int] = mapped_column(default=50, comment="最大对话轮数")

    # 元数据
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="智能体描述")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, default=dict, comment="扩展元数据")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
