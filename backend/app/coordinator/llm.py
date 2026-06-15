"""
群主 LLM 调用封装

基于 LangChain ChatOpenAI（兼容 OpenAI 协议的代理），提供：
- 意图分析（结构化输出）
- 任务拆解（结构化输出 DAG JSON）
- 结果汇总

支持通过环境变量切换 LLM 后端：
- ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL → ChatAnthropic
- OPENAI_API_KEY + OPENAI_BASE_URL → ChatOpenAI
- LLM_PROVIDER=auto (默认) 自动检测，也可设为 openai/anthropic
- LLM_MODEL 覆盖默认模型名
"""
import os

from langchain_core.language_models import BaseChatModel
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

# 默认模型名（顺序：ANTHROPIC_MODEL 环境变量 → LLM_MODEL 环境变量 → 硬编码）
_DEFAULT_OPENAI_MODEL = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("LLM_MODEL") or "glm-5.1"
_DEFAULT_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("LLM_MODEL") or "claude-sonnet-4-6-20250514"


def _detect_provider() -> str:
    """自动检测 LLM 提供者"""
    provider = os.environ.get("LLM_PROVIDER", settings.LLM_PROVIDER).lower()
    if provider in ("openai", "anthropic"):
        return provider

    # auto 检测：根据可用的 key 和 base_url 判断
    openai_base = settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_base = settings.ANTHROPIC_BASE_URL or os.environ.get("ANTHROPIC_BASE_URL", "")

    if openai_key or openai_base:
        return "openai"

    # 如果 ANTHROPIC_BASE_URL 指向非官方代理，默认用 openai 兼容
    if settings.ANTHROPIC_API_KEY and anthropic_base and "anthropic.com" not in anthropic_base:
        return "openai"

    if settings.ANTHROPIC_API_KEY:
        return "anthropic"

    return "openai"


def _get_llm(temperature: float = 0.0) -> BaseChatModel:
    """获取 LLM 实例（自动检测 provider）"""
    provider = _detect_provider()
    model = settings.LLM_MODEL or os.environ.get("LLM_MODEL", "")

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        base_url = settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL", "")
        api_key = os.environ.get("OPENAI_API_KEY", "")

        # 如果没有单独的 OpenAI 配置，尝试复用 Anthropic 的配置
        if not api_key and settings.ANTHROPIC_API_KEY:
            api_key = settings.ANTHROPIC_API_KEY
        if not base_url:
            anthropic_base = settings.ANTHROPIC_BASE_URL or os.environ.get("ANTHROPIC_BASE_URL", "")
            if anthropic_base:
                # Anthropic base_url 通常不带 /v1 后缀，OpenAI 需要 /v1
                base_url = anthropic_base.rstrip("/") + "/v1"

        return ChatOpenAI(
            model=model or _DEFAULT_OPENAI_MODEL,
            api_key=api_key,
            base_url=base_url or None,
            temperature=temperature,
            max_tokens=4096,
        )
    else:
        from langchain_anthropic import ChatAnthropic

        base_url = settings.ANTHROPIC_BASE_URL or os.environ.get("ANTHROPIC_BASE_URL", "")

        return ChatAnthropic(
            model=model or _DEFAULT_ANTHROPIC_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            base_url=base_url or None,
            temperature=temperature,
            max_tokens=4096,
        )


def get_intent_analyzer() -> BaseChatModel:
    """意图分析器：输出 IntentAnalysis 结构化结果"""
    return _get_llm(temperature=0.0).with_structured_output(IntentAnalysis)


def get_task_decomposer() -> BaseChatModel:
    """任务拆解器：输出 TaskDecomposition 结构化结果"""
    return _get_llm(temperature=0.0).with_structured_output(TaskDecomposition)


def get_summarizer() -> BaseChatModel:
    """结果汇总器：自由文本输出"""
    return _get_llm(temperature=0.3)
