"""
群主 LLM 调用封装

基于 LangChain ChatAnthropic，提供：
- 意图分析（结构化输出）
- 任务拆解（结构化输出 DAG JSON）
- 结果汇总
"""
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from app.core.config import settings


# ── 结构化输出 Schema ──────────────────────────────────────────────


class IntentAnalysis(BaseModel):
    """意图分析结果"""
    analysis: str = Field(description="对用户需求的理解和分析")
    involved_roles: list[str] = Field(description="涉及的角色标识列表，如 ['frontend-engineer', 'backend-engineer']")


class SubTaskDef(BaseModel):
    """子任务定义"""
    title: str = Field(description="任务标题，如'实现登录页面'")
    description: str = Field(description="任务详细描述，包含具体要求和约束")
    assigned_role: str = Field(description="执行角色标识，如 'frontend-engineer'")
    depends_on: list[int] = Field(
        default_factory=list,
        description="依赖的前置子任务序号（0-based），空列表表示无依赖",
    )


class TaskDecomposition(BaseModel):
    """任务拆解结果（DAG JSON）"""
    subtasks: list[SubTaskDef] = Field(description="拆解后的子任务列表")
    reasoning: str = Field(description="拆解理由和调度策略说明")


# ── LLM 工厂 ────────────────────────────────────────────────────────


def _get_llm(temperature: float = 0.0) -> ChatAnthropic:
    """获取 ChatAnthropic 实例"""
    return ChatAnthropic(
        model="claude-sonnet-4-6-20250514",
        api_key=settings.ANTHROPIC_API_KEY,
        temperature=temperature,
        max_tokens=4096,
    )


def get_intent_analyzer() -> ChatAnthropic:
    """意图分析器：输出 IntentAnalysis 结构化结果"""
    return _get_llm(temperature=0.0).with_structured_output(IntentAnalysis)


def get_task_decomposer() -> ChatAnthropic:
    """任务拆解器：输出 TaskDecomposition 结构化结果"""
    return _get_llm(temperature=0.0).with_structured_output(TaskDecomposition)


def get_summarizer() -> ChatAnthropic:
    """结果汇总器：自由文本输出"""
    return _get_llm(temperature=0.3)
