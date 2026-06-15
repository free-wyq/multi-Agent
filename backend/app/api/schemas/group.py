"""
群组相关 Pydantic Schema
"""
from datetime import datetime

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    """创建群组"""
    name: str = Field(..., min_length=1, max_length=100, description="群组名称")
    coordinator_id: str = Field(..., description="群主智能体定义 ID")
    description: str | None = None
    config: dict | None = None


class GroupUpdate(BaseModel):
    """更新群组"""
    name: str | None = None
    coordinator_id: str | None = None
    description: str | None = None
    config: dict | None = None
    status: str | None = None


class GroupResponse(BaseModel):
    """群组响应"""
    id: str
    name: str
    coordinator_id: str
    volume_name: str | None
    description: str | None
    status: str
    config: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GroupMemberAdd(BaseModel):
    """添加群组成员"""
    agent_id: str = Field(..., description="子智能体定义 ID")
    alias: str | None = None


class GroupMemberResponse(BaseModel):
    """群组成员响应"""
    id: str
    group_id: str
    agent_id: str
    alias: str | None
    joined_at: datetime

    model_config = {"from_attributes": True}


class GroupMemberWithAgentResponse(BaseModel):
    """群组成员（含智能体详情）响应"""
    id: str
    group_id: str
    agent_id: str
    alias: str | None
    joined_at: datetime
    agent_name: str
    agent_role: str

    model_config = {"from_attributes": True}


class GroupFileResponse(BaseModel):
    """群共享文件响应"""
    name: str = Field(..., description="文件名")
    size: int = Field(..., description="文件大小（字节）")
    modified_at: float = Field(..., description="修改时间戳（秒）")

    model_config = {"from_attributes": True}
