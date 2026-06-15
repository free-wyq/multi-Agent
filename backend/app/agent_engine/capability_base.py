"""
能力插件抽象基类

每个子智能体挂载一组能力（如 claude_code、shell）。
能力 = 外部工具，不是智能体本体。智能体用不用，由大脑决定。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class CapabilityEvent:
    """能力执行事件（流式输出）"""
    type: str  # "log" | "file" | "result" | "error"
    text: str = ""  # 日志/结果文本
    path: str | None = None  # type=file 时


@dataclass
class CapabilityResult:
    """能力执行结果"""
    success: bool
    summary: str  # 一句话总结
    logs: list[str]  # 完整日志
    artifacts: list[str]  # 产出物路径
    exit_code: int = 0


class Capability(ABC):
    """能力插件抽象

    Usage:
        cap = ClaudeCodeCapability(agent_def)
        async for event in cap.invoke("实现登录API"):
            yield event  # 实时日志推送给用户看
        result = await cap.finalize()  # 执行完毕后的结果汇总
    """

    name: str = ""           # 能力标识，如 "claude_code"
    requires_runtime: bool = False  # 是否需要启动外部进程
    description: str = ""    # UI 展示用

    @abstractmethod
    async def invoke(self, task: str, **kwargs) -> AsyncIterator[CapabilityEvent]:
        """执行任务，流式返回事件

        Args:
            task: 任务指令（大脑拆解后的明确指令）
            **kwargs: 额外上下文（如 group_id、project_path）
        """
        ...

    @abstractmethod
    async def finalize(self) -> CapabilityResult:
        """执行完成后调用，返回结构化结果"""
        ...

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "requires_runtime": self.requires_runtime,
        }
