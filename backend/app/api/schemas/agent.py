"""
智能体相关 Pydantic Schema
"""
from datetime import datetime

from pydantic import BaseModel, Field


# ── AgentDefinition ──────────────────────────────────────────────

class AgentDefinitionCreate(BaseModel):
    """创建智能体定义"""
    name: str = Field(..., min_length=1, max_length=100, description="智能体名称")
    role: str = Field(..., min_length=1, max_length=50, description="角色标识")
    system_prompt: str = Field(..., min_length=1, description="角色系统提示词")
    skills: list[str] = Field(default_factory=list, description="技能列表（角色模板自动映射）")
    extra_skills: list[str] = Field(default_factory=list, description="技能市场额外挂载")
    base_image: str = Field(default="agent-base:latest", description="基础镜像")
    allowed_tools: list[str] = Field(default_factory=list, description="允许使用的工具")
    denied_tools: list[str] = Field(default_factory=list, description="禁止使用的工具")
    startup_strategy: str = Field(default="on_demand", description="启动策略")
    model: str = Field(default="claude-sonnet-4-6-20250514", description="LLM 模型")
    max_turns: int = Field(default=50, description="最大对话轮数")
    description: str | None = Field(None, description="智能体描述")
    metadata_: dict | None = Field(None, alias="metadata", description="扩展元数据")

    model_config = {"populate_by_name": True}


class AgentDefinitionUpdate(BaseModel):
    """更新智能体定义（全部可选）"""
    name: str | None = None
    role: str | None = None
    system_prompt: str | None = None
    skills: list[str] | None = None
    extra_skills: list[str] | None = None
    base_image: str | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    startup_strategy: str | None = None
    model: str | None = None
    max_turns: int | None = None
    description: str | None = None
    metadata_: dict | None = Field(None, alias="metadata")

    model_config = {"populate_by_name": True}


class AgentDefinitionResponse(BaseModel):
    """智能体定义响应"""
    id: str
    name: str
    role: str
    system_prompt: str
    skills: list[str]
    extra_skills: list[str]
    base_image: str
    allowed_tools: list[str]
    denied_tools: list[str]
    startup_strategy: str
    model: str
    max_turns: int
    description: str | None
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


# ── AgentInstance ────────────────────────────────────────────────

class AgentInstanceResponse(BaseModel):
    """智能体实例响应"""
    id: str
    definition_id: str
    container_id: str | None
    container_name: str | None
    session_id: str | None
    status: str
    current_task_id: str | None
    work_dir: str | None
    metadata_: dict | None = Field(None, alias="metadata")
    created_at: datetime
    stopped_at: datetime | None

    model_config = {"from_attributes": True, "populate_by_name": True}
