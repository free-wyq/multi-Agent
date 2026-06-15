"""
运行时服务层

协调 AgentInstance 数据库操作、ClaudeCodeRuntime 执行、实例池管理。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_definition import AgentDefinition
from app.models.agent_instance import AgentInstance
from app.runtime.claude_code_runtime import ClaudeCodeRuntime
from app.runtime.base_runtime import AgentResult, InstanceStatus
from app.runtime.instance_pool import ContainerInstancePool
from app.services import agent_service
from app.services import agent_service_instance

logger = logging.getLogger(__name__)

# 内存中的运行时实例表（非持久）
# {instance_id: ClaudeCodeRuntime}
_active_runtimes: dict[str, ClaudeCodeRuntime] = {}


# ── 启动/停止 ──────────────────────────────────────────────────────


async def start_agent(
    db: AsyncSession,
    *,
    definition_id: str,
    group_id: str,
    pool: ContainerInstancePool,
) -> dict:
    """启动智能体运行时"""
    # 1. 获取定义
    agent_def = await agent_service.get_definition(db, definition_id)
    if not agent_def:
        raise ValueError(f"智能体定义不存在: {definition_id}")

    # 2. 从池中获取（如果是 pooled 策略）或新建
    runtime = await pool.acquire(agent_def, group_id)

    # 3. 创建/更新 AgentInstance 记录
    instance = await agent_service_instance.create_instance(db, {
        "id": runtime.instance_id,
        "definition_id": definition_id,
        "container_id": runtime.container_id,
        "container_name": runtime.container_name,
        "status": runtime.status.value,
        "work_dir": "/workspace",
    })

    # 4. 注册到活跃表
    _active_runtimes[runtime.instance_id] = runtime

    return {
        "instance_id": runtime.instance_id,
        "container_id": runtime.container_id,
        "container_name": runtime.container_name,
        "status": runtime.status.value,
        "message": "启动成功",
    }


async def stop_instance(instance_id: str, *, remove_container: bool = True) -> None:
    """停止实例"""
    runtime = _active_runtimes.get(instance_id)
    if runtime:
        await runtime.stop(remove_container=remove_container)
        del _active_runtimes[instance_id]
    else:
        # 仍然尝试用 DockerManager 停止
        from app.runtime.docker_manager import DockerContainerManager
        dm = DockerContainerManager()
        # 需要 container_id，查询 DB ...简化：直接报找不到
        raise ValueError(f"未找到活跃实例: {instance_id}")


async def restart_instance(instance_id: str) -> dict:
    """重启实例"""
    runtime = _active_runtimes.get(instance_id)
    if not runtime:
        raise ValueError(f"未找到活跃实例: {instance_id}")

    await runtime.restart()
    return {
        "instance_id": runtime.instance_id,
        "container_id": runtime.container_id,
        "container_name": runtime.container_name,
        "status": runtime.status.value,
        "message": "重启成功",
    }


# ── 任务执行 ───────────────────────────────────────────────────────


async def execute_task(
    instance_id: str,
    task: str,
    *,
    task_id: str | None = None,
    timeout: float = 600.0,
    pool: ContainerInstancePool | None = None,
) -> AgentResult:
    """下发任务到指定实例"""
    runtime = _active_runtimes.get(instance_id)
    if not runtime:
        raise ValueError(f"实例未运行: {instance_id}")

    logger.info("下发任务: instance=%s task=%s", instance_id[:8], task[:40])
    result = await runtime.execute(task, task_id=task_id, timeout=timeout)

    # 根据策略释放（MVP 默认 on_demand 直接销毁）
    if pool and runtime._agent_def:
        await pool.release(runtime, strategy=runtime._agent_def.startup_strategy)
        if runtime.instance_id in _active_runtimes:
            del _active_runtimes[runtime.instance_id]

    return result


# ── 查询 ───────────────────────────────────────────────────────────


async def get_instance(db: AsyncSession, instance_id: str) -> AgentInstance | None:
    """获取实例 DB 记录"""
    return await agent_service_instance.get_instance(db, instance_id)


async def list_instances_by_group(db: AsyncSession, group_id: str) -> list[AgentInstance]:
    """按群组列出实例"""
    # 目前没有 group_id 字段，简化：返回所有 running 实例
    # 实际应查询 DB 中 container_name 包含 group_id 的……或者扩展模型加 group_id
    from sqlalchemy import select
    stmt = select(AgentInstance).where(AgentInstance.status.in_(["running", "idle", "pooled"]))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_logs(instance_id: str, tail: int = 100) -> str:
    """获取实例日志"""
    runtime = _active_runtimes.get(instance_id)
    if not runtime:
        raise ValueError(f"实例未运行: {instance_id}")
    return await runtime.get_logs(tail=tail)
