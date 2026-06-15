"""
AgentEngine 子智能体运行时模块

每个子智能体 = 常驻 asyncio Task
- 轻量 LLM 大脑（闲聊/讨论秒回）
- 执行时从实例池获取 Docker 跑 Claude Code（内置能力）
- 流式日志回传
"""
from app.agent_engine.runtime import AgentEngine
from app.agent_engine.registry import AgentRegistry, get_registry

__all__ = ["AgentEngine", "AgentRegistry", "get_registry"]
