"""
智能体实例模型（运行时 — 就绪态 / 执行态）

对应"第2/3层：就绪态 / 执行态"。由 AgentDefinition 实例化而来，
绑定 Docker 容器，任务完成后释放或回收。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False,
        comment="关联的智能体定义",
    )

    # Docker 容器信息
    container_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="Docker 容器 ID")
    container_name: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="Docker 容器名")

    # Claude Code 会话信息
    session_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="Claude Code 会话 ID")

    # 状态：idle / running / error / stopped
    status: Mapped[str] = mapped_column(String(20), default="idle", comment="实例状态")

    # 当前任务
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="当前执行的任务 ID")

    # 工作目录（容器内路径）
    work_dir: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="容器内工作目录")

    # 扩展信息
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, default=dict, comment="运行时元数据")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="停止时间")
