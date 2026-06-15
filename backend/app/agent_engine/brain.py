"""
子智能体大脑（轻量 LLM）

负责判断用户消息是「闲聊/讨论」还是「需要执行」，
闲聊直接回复，执行就生成任务指令。

使用轻量模型（Haiku），够快够便宜。
"""
import logging
import os

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)


class BrainDecision(BaseModel):
    """大脑决策结果"""
    action: str = Field(description="决策：chat（纯聊天） / execute（需要执行） / ask（需要澄清）")
    content: str = Field(description="给用户的回复内容（action=chat）或给执行器的任务指令（action=execute）")
    reasoning: str = Field(description="决策理由")


_DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("LLM_MODEL") or "claude-haiku-4-5-20251001"


def _get_llm(temperature: float = 0.3) -> BaseChatModel:
    model = settings.LLM_MODEL or os.environ.get("LLM_MODEL", "")
    if "sonnet" in model or "opus" in model:
        pass
    else:
        model = model or _DEFAULT_MODEL

    provider = os.environ.get("LLM_PROVIDER", settings.LLM_PROVIDER).lower()
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        base_url = settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL", "")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key and settings.ANTHROPIC_API_KEY:
            api_key = settings.ANTHROPIC_API_KEY
        if not base_url:
            anthropic_base = settings.ANTHROPIC_BASE_URL or os.environ.get("ANTHROPIC_BASE_URL", "")
            if anthropic_base:
                base_url = anthropic_base.rstrip("/") + "/v1"

        return ChatOpenAI(
            model=model, api_key=api_key, base_url=base_url or None,
            temperature=temperature, max_tokens=4096,
        )
    else:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model or _DEFAULT_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            temperature=temperature,
            max_tokens=4096,
        )


def get_brain() -> BaseChatModel:
    """获取带结构化输出的大脑 LLM"""
    return _get_llm(temperature=0.3).with_structured_output(BrainDecision)


BRAIN_PROMPT = """你是一名专业的 {role}，名字叫 {name}。

当前对话上下文：
{context}

用户发来消息：{message}

请判断：
- chat：如果只是讨论、咨询、确认方案 → 直接回复用户
- execute：如果用户明确要求你动手干活（写代码、改配置、运行命令） → 输出给执行器的任务指令
- ask：如果意图不清/缺少必要信息 → 向用户提问

执行任务时的要求：
1. 把任务拆解为清晰的执行指令（一句话说明要做什么）
2. 指定必须遵守的约束（如"用 FastAPI"、"不要改现有路由"）
3. 如果需要先和用户确认方案，用 ask 模式

重要：如果你需要请求其他团队成员协助，在回复中用 @对方名字 的方式提及对方，系统会自动将消息路由给他们。
例如：@后端工程师 请提供登录API接口

你的回答：
"""
