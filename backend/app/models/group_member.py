"""
群组成员关联模型

群组与子智能体之间的多对多关系。
群主（coordinator）不在这里，在 Group.coordinator_id 中。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GroupMember(Base):
    __tablename__ = "group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False,
        comment="子智能体定义 ID",
    )

    # 成员别名（同一个智能体定义在多个群组中可以有不同的称呼）
    alias: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="群组内别名")

    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
