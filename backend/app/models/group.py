"""
群组模型

群组 = 多个智能体的协作单元。群主（coordinator）是群组的核心，
负责接收需求、意图分析、任务拆解和调度。
中间件由 Claude Code 运行时自装自起，群组本身不存储中间件配置。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="群组名称")

    # 群主：必须是某个智能体定义
    coordinator_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False,
        comment="群主智能体定义 ID",
    )

    # 环境卷：群组共享的 Docker Volume 名称
    volume_name: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="环境卷名称")

    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="群组描述")

    # 状态：active / archived
    status: Mapped[str] = mapped_column(String(20), default="active", comment="群组状态")

    # 扩展配置
    config: Mapped[dict | None] = mapped_column(JSONB, default=dict, comment="群组扩展配置")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
