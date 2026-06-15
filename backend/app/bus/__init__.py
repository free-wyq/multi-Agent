"""
消息总线模块

基于 Redis Pub/Sub 实现的进程内消息总线，用于：
- 群主协调器 → 子智能体：派发任务
- 子智能体 → 群主协调器：回报结果
- 后端 → 前端：WebSocket 实时推送

Channel 命名：agenticx:group:{group_id}
"""
from app.bus.core import MessageBus, get_bus

__all__ = ["MessageBus", "get_bus"]
