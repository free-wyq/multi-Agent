"""
AgentEngine 注册表

全局管理所有子智能体引擎：
- FastAPI 启动时：根据 DB 中的 AgentDefinition，为每个活跃群组创建常驻协程
- 运行时：按 agent_id 路由消息到对应引擎
- 关闭时：优雅停止所有引擎
"""
import asyncio
import logging

from app.core.database import async_session
from app.models.agent_definition import AgentDefinition
from app.models.group_member import GroupMember
from app.agent_engine.runtime import AgentEngine

logger = logging.getLogger(__name__)


class AgentRegistry:
    """子智能体注册表

    {group_id: {agent_id: AgentEngine}}
    """

    def __init__(self) -> None:
        self._engines: dict[str, dict[str, AgentEngine]] = {}
        self._lock = asyncio.Lock()

    async def load_from_db(self) -> None:
        """从数据库加载：为有群组的智能体创建常驻引擎"""
        async with async_session() as db:
            # 查询所有有群组成员关系的智能体
            result = await db.execute(
                __import__('sqlalchemy').select(
                    GroupMember.group_id, GroupMember.agent_id
                )
            )
            members = result.all()

            # 批量查询智能体定义
            agent_ids = list({m[1] for m in members})
            if not agent_ids:
                logger.info("AgentRegistry: 没有需要启动的智能体")
                return

            result = await db.execute(
                __import__('sqlalchemy').select(AgentDefinition)
                .where(AgentDefinition.id.in_(agent_ids))
            )
            defs = {d.id: d for d in result.scalars().all()}

        # 创建引擎
        created = 0
        for group_id, agent_id in members:
            agent_def = defs.get(agent_id)
            if not agent_def:
                continue

            # 不重复创建
            if group_id in self._engines and agent_id in self._engines[group_id]:
                continue

            engine = AgentEngine(agent_def, group_id=group_id)
            await engine.start()

            async with self._lock:
                self._engines.setdefault(group_id, {})[agent_id] = engine
            created += 1

        logger.info("AgentRegistry: 已启动 %d 个子智能体引擎", created)

    async def add_engine(self, agent_def: AgentDefinition, group_id: str) -> AgentEngine:
        """新增一个引擎（如用户新拉群员进群）"""
        engine = AgentEngine(agent_def, group_id=group_id)
        await engine.start()

        async with self._lock:
            self._engines.setdefault(group_id, {})[agent_def.id] = engine

        logger.info("AgentRegistry: 新增引擎 %s in group %s", agent_def.name, group_id[:8])
        return engine

    async def remove_engine(self, agent_id: str, group_id: str) -> None:
        """移除一个引擎（用户把群员踢出群）"""
        async with self._lock:
            group_engines = self._engines.get(group_id, {})
            engine = group_engines.pop(agent_id, None)
            if not group_engines:
                self._engines.pop(group_id, None)

        if engine:
            await engine.stop()
            logger.info("AgentRegistry: 移除引擎 %s", agent_id[:8])

    def get_engine(self, agent_id: str, group_id: str | None = None) -> AgentEngine | None:
        """获取引擎

        Args:
            agent_id: 智能体定义 ID
            group_id: 如果指定，只在指定群找；否则全局搜索
        """
        if group_id:
            return self._engines.get(group_id, {}).get(agent_id)
        for group_engines in self._engines.values():
            if agent_id in group_engines:
                return group_engines[agent_id]
        return None

    async def route_message(self, agent_id: str, message: dict, group_id: str | None = None) -> bool:
        """路由消息到指定引擎

        Returns:
            True: 成功路由
            False: 引擎不存在
        """
        engine = self.get_engine(agent_id, group_id=group_id)
        if not engine:
            logger.warning("AgentRegistry: 找不到引擎 %s", agent_id[:8])
            return False

        await engine.push_message(message)
        return True

    async def shutdown_all(self) -> None:
        """停止所有引擎"""
        all_engines: list[AgentEngine] = []
        async with self._lock:
            for group_engines in self._engines.values():
                all_engines.extend(group_engines.values())
            self._engines.clear()

        await asyncio.gather(*[e.stop() for e in all_engines], return_exceptions=True)
        logger.info("AgentRegistry: 已停止 %d 个引擎", len(all_engines))


# 全局单例
_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    """获取全局注册表"""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
