"""
AgentRuntime 抽象基类

定义子智能体运行时的统一接口。
当前唯一实现：ClaudeCodeRuntime（基于 Docker + Claude Code CLI）
未来可扩展：OpenAIRuntime（基于 OpenAI Agent SDK）等。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class InstanceStatus(str, Enum):
    """智能体实例状态"""

    IDLE = "idle"         # 空闲，等待任务
    RUNNING = "running"   # 正在执行任务
    ERROR = "error"       # 启动或执行出错
    STOPPED = "stopped"   # 已停止（容器已销毁）
    POOLED = "pooled"     # 在实例池中待命（仅 pooled 模式）


@dataclass
class AgentResult:
    """智能体执行结果"""

    success: bool
    exit_code: int
    output: str                    # stdout/stderr 文本
    artifact_paths: list[str]      # 产出物路径（相对于 /workspace）
    task_id: str | None = None
    agent_id: str | None = None
    error_message: str | None = None


class AgentRuntime(ABC):
    """智能体运行时抽象

    每个子智能体一个运行时实例，封装容器管理和任务执行。

    Usage:
        runtime = ClaudeCodeRuntime(definition_id, group_id, ...)
        await runtime.start()
        result = await runtime.execute("实现登录功能")
        await runtime.stop()
    """

    def __init__(
        self,
        definition_id: str,
        definition_name: str,
        group_id: str,
        *,
        instance_id: str | None = None,
        role: str = "executor",
    ) -> None:
        self.definition_id = definition_id
        self.definition_name = definition_name
        self.group_id = group_id
        self.instance_id = instance_id
        self.role = role

        self.status = InstanceStatus.IDLE
        self.current_task_id: str | None = None
        self.container_id: str | None = None
        self.container_name: str | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    @abstractmethod
    async def start(self) -> None:
        """启动运行时（创建并启动容器）

        执行完成后，self.container_id 必须被设置。
        """
        ...

    @abstractmethod
    async def stop(self, *, remove_container: bool = True) -> None:
        """停止运行时（停止并可选销毁容器）"""
        ...

    @abstractmethod
    async def restart(self) -> None:
        """重启运行时（用于 error 恢复）"""
        ...

    # ── 任务执行 ──────────────────────────────────────────────────────

    @abstractmethod
    async def execute(self, task: str, *, task_id: str | None = None) -> AgentResult:
        """下发任务并等待（阻塞）执行结果

        Args:
            task: 任务指令文本，发给 Claude Code
            task_id: 可选的任务 ID，用于结果关联

        Returns:
            AgentResult 执行结果
        """
        ...

    # ── 状态与日志 ────────────────────────────────────────────────────

    @abstractmethod
    async def get_logs(self, tail: int = 100) -> str:
        """获取最近日志"""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        """检查运行时健康状态"""
        ...

    # ── 元数据 ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """序列化运行时为字典（用于 API 返回 / 数据库存储）"""
        return {
            "instance_id": self.instance_id,
            "definition_id": self.definition_id,
            "definition_name": self.definition_name,
            "group_id": self.group_id,
            "role": self.role,
            "status": self.status.value,
            "current_task_id": self.current_task_id,
            "container_id": self.container_id,
            "container_name": self.container_name,
        }
