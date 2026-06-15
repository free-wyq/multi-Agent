"""
任务模型

群主将用户需求拆解为子任务，按 DAG 依赖关系调度。
任务状态参考 A2A 协议：submitted → working → completed/failed/canceled/input-required
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
        comment="所属群组",
    )

    # 任务层级：支持子任务
    parent_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
        comment="父任务 ID（子任务场景）",
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="任务标题")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="任务描述（群主拆解后的详细指令）")

    # 任务状态（A2A 风格）
    # submitted → working → completed / failed / canceled / input-required
    status: Mapped[str] = mapped_column(String(20), default="submitted", comment="任务状态")

    # 执行者
    assigned_agent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_definitions.id", ondelete="SET NULL"), nullable=True,
        comment="负责执行的智能体定义 ID",
    )
    instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_instances.id", ondelete="SET NULL"), nullable=True,
        comment="实际执行的智能体实例 ID",
    )

    # DAG 依赖：前置任务 ID 列表
    dependencies: Mapped[list] = mapped_column(ARRAY(String), default=list, comment="依赖的前置任务 ID 列表")

    # 产出物
    artifact_path: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="产出物路径（容器内）")
    artifact: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="产出物元数据")

    # 执行详情
    exit_code: Mapped[int | None] = mapped_column(nullable=True, comment="容器退出码")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="结果摘要")

    # DAG 序号（前端展示用）
    dag_order: Mapped[int | None] = mapped_column(nullable=True, comment="DAG 节点顺序")

    # 时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="开始执行时间")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="完成时间")
