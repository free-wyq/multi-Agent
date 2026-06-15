"""
容器实例池（ContainerInstancePool）

管理空闲运行的 AgentInstance，支持复用策略减少冷启动时间。

策略映射：
- on_demand（默认）：任务完成后销毁容器
- pooled：任务完成后回到池中待命，复用
- always_on：由外部管理，不归池管理
"""
import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

from app.runtime.claude_code_runtime import ClaudeCodeRuntime
from app.runtime.base_runtime import InstanceStatus

if TYPE_CHECKING:
    from app.models.agent_definition import AgentDefinition

logger = logging.getLogger(__name__)

# 每个定义最多保留的空闲实例数
MAX_POOL_SIZE_PER_DEF = 3


class ContainerInstancePool:
    """智能体实例池

    按 definition_id 分组维护空闲实例队列：
        {def_id: deque[ClaudeCodeRuntime]}

    用法:
        pool = ContainerInstancePool()
        runtime = await pool.acquire(agent_definition, group_id)
        result = await runtime.execute("实现登录")
        await pool.release(runtime, strategy="on_demand")  # 或 "pooled"
    """

    def __init__(self) -> None:
        # definition_id → 空闲实例队列
        self._pools: dict[str, deque[ClaudeCodeRuntime]] = {}
        self._lock = asyncio.Lock()

    # ── 获取/释放 ──────────────────────────────────────────────────────

    async def acquire(
        self,
        agent_def: "AgentDefinition",
        group_id: str,
    ) -> ClaudeCodeRuntime:
        """获取一个可用的运行时实例

        优先复用空闲池中的实例，无空闲则新建容器。
        """
        def_id = agent_def.id

        async with self._lock:
            # 1) 尝试复用空闲实例
            pool = self._pools.get(def_id)
            if pool:
                while pool:
                    candidate = pool.popleft()
                    if await candidate.is_healthy():
                        candidate.current_task_id = None
                        candidate.status = InstanceStatus.RUNNING
                        logger.info(
                            "实例池复用: def=%s instance=%s",
                            def_id[:8],
                            candidate.instance_id[:8],
                        )
                        return candidate
                    else:
                        # 不健康，销毁
                        await candidate.stop(remove_container=True)
                        logger.warning(
                            "实例池丢弃不健康实例: def=%s",
                            def_id[:8],
                        )

        # 2) 池中没有可用，新建容器
        runtime = ClaudeCodeRuntime(
            definition_id=agent_def.id,
            definition_name=agent_def.name,
            group_id=group_id,
            role=agent_def.role,
            agent_def=agent_def,
        )
        await runtime.start()
        runtime.status = InstanceStatus.RUNNING
        logger.info(
            "实例池新建: def=%s instance=%s container=%s",
            def_id[:8],
            runtime.instance_id[:8],
            runtime.container_id[:12] if runtime.container_id else "N/A",
        )
        return runtime

    async def release(
        self,
        runtime: ClaudeCodeRuntime,
        *,
        strategy: str = "on_demand",
    ) -> None:
        """释放运行时实例

        Args:
            strategy: on_demand（销毁）/ pooled（回收到池）/ always_on（忽略）
        """
        if strategy == "always_on":
            # 由外部管理，什么都不做
            runtime.status = InstanceStatus.IDLE
            return

        if strategy == "pooled":
            async with self._lock:
                pool = self._pools.setdefault(runtime.definition_id, deque())
                if len(pool) < MAX_POOL_SIZE_PER_DEF:
                    runtime.status = InstanceStatus.POOLED
                    pool.append(runtime)
                    logger.info(
                        "实例池回收: def=%s instance=%s (池大小=%d)",
                        runtime.definition_id[:8],
                        runtime.instance_id[:8],
                        len(pool),
                    )
                    return
                else:
                    # 池已满，销毁
                    logger.info("实例池已满，销毁实例: %s", runtime.instance_id[:8])

        # 默认 on_demand：销毁容器
        await runtime.stop(remove_container=True)
        logger.info("实例已销毁: %s", runtime.instance_id[:8])

    # ── 池管理 ────────────────────────────────────────────────────────

    async def warmup(
        self,
        agent_def: "AgentDefinition",
        group_id: str,
        count: int = 1,
    ) -> list[ClaudeCodeRuntime]:
        """预热：预先创建 N 个实例放入池中"""
        instances: list[ClaudeCodeRuntime] = []
        for _ in range(count):
            runtime = ClaudeCodeRuntime(
                definition_id=agent_def.id,
                definition_name=agent_def.name,
                group_id=group_id,
                role=agent_def.role,
                agent_def=agent_def,
            )
            await runtime.start()
            runtime.status = InstanceStatus.POOLED
            async with self._lock:
                pool = self._pools.setdefault(agent_def.id, deque())
                pool.append(runtime)
            instances.append(runtime)
        logger.info(
            "预热完成: def=%s count=%d",
            agent_def.id[:8],
            count,
        )
        return instances

    async def drain(self, definition_id: str | None = None) -> None:
        """清空池中所有实例（或指定 definition_id 的实例）"""
        async with self._lock:
            if definition_id:
                pool = self._pools.pop(definition_id, deque())
                to_stop = list(pool)
            else:
                to_stop = []
                for pool in self._pools.values():
                    to_stop.extend(pool)
                self._pools.clear()

        for runtime in to_stop:
            await runtime.stop(remove_container=True)

        logger.info("实例池已清空%s", f" def={definition_id[:8]}" if definition_id else "")

    def stats(self) -> dict[str, int]:
        """返回各 definition 的池大小"""
        return {k: len(v) for k, v in self._pools.items()}
