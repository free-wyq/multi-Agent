"""
任务相关 Pydantic Schema
"""
from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    """创建任务（用户提交需求）"""
    group_id: str = Field(..., description="所属群组")
    title: str = Field(..., min_length=1, max_length=200, description="任务标题")
    description: str | None = None
    parent_task_id: str | None = None
    assigned_agent_id: str | None = None
    dependencies: list[str] = Field(default_factory=list, description="前置任务 ID 列表")
    dag_order: int | None = None


class TaskUpdate(BaseModel):
    """更新任务"""
    status: str | None = None
    assigned_agent_id: str | None = None
    instance_id: str | None = None
    artifact_path: str | None = None
    artifact: dict | None = None
    exit_code: int | None = None
    error_message: str | None = None
    result_summary: str | None = None


class TaskResponse(BaseModel):
    """任务响应"""
    id: str
    group_id: str
    parent_task_id: str | None
    title: str
    description: str | None
    status: str
    assigned_agent_id: str | None
    instance_id: str | None
    dependencies: list[str]
    artifact_path: str | None
    artifact: dict | None
    exit_code: int | None
    error_message: str | None
    result_summary: str | None
    dag_order: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}
