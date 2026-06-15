"""
消息模型

智能体之间的通信记录，包括群主派发任务、子智能体回报结果、
用户澄清输入等。消息是协调机制，文件是数据载体。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
        comment="所属群组",
    )
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
        comment="关联的任务",
    )

    # 发送方：可以是智能体定义 ID 或 "user" / "coordinator"
    sender_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="发送方标识")
    # 接收方：同上，或 "broadcast" 表示群播
    receiver_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="接收方标识")

    # 消息类型：task_dispatch / task_complete / task_failed / input_required / user_input / log
    type: Mapped[str] = mapped_column(String(50), nullable=False, comment="消息类型")

    # 消息内容
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="消息正文")

    # 结构化数据（如 task_complete 中的 artifact 信息）
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="结构化数据")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
