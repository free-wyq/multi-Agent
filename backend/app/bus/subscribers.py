"""
Bus → WebSocket 桥接订阅

当首个 WebSocket 客户端连接某 group 时，自动订阅该 group 的 bus channel；
当最后一个客户端断开时，自动取消订阅。
"""
import logging

from app.bus.core import get_bus, CHANNEL_PREFIX
from app.ws.manager import get_ws_manager

logger = logging.getLogger(__name__)

# 已订阅 bus → ws 的 group 集合
_subscribed_groups: set[str] = set()


async def ensure_group_subscription(group_id: str) -> None:
    """确保 WebSocket 管理器已订阅该 group 的 bus channel

    幂等操作：已订阅则跳过。
    """
    if group_id in _subscribed_groups:
        return
    bus = get_bus()
    ws_manager = get_ws_manager()
    channel = f"{CHANNEL_PREFIX}{group_id}"
    await bus.subscribe(channel, ws_manager.handle_bus_message)
    _subscribed_groups.add(group_id)
    logger.info("Subscribed WS manager to bus channel: %s", channel)


async def remove_group_subscription(group_id: str) -> None:
    """移除 WebSocket 管理器对该 group 的 bus 订阅"""
    if group_id not in _subscribed_groups:
        return
    bus = get_bus()
    ws_manager = get_ws_manager()
    channel = f"{CHANNEL_PREFIX}{group_id}"
    await bus.unsubscribe(channel, ws_manager.handle_bus_message)
    _subscribed_groups.discard(group_id)
    logger.info("Unsubscribed WS manager from bus channel: %s", channel)
