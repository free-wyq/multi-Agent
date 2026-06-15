"""
消息相关 Pydantic Schema
"""
from datetime import datetime

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    """创建消息"""
    group_id: str = Field(..., description="所属群组")
    task_id: str | None = None
    sender_id: str = Field(..., description="发送方标识")
    receiver_id: str = Field(..., description="接收方标识")
    type: str = Field(..., description="消息类型")
    content: str | None = None
    data: dict | None = None


class MessageResponse(BaseModel):
    """消息响应"""
    id: str
    group_id: str
    task_id: str | None
    sender_id: str
    receiver_id: str
    type: str
    content: str | None
    data: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}
