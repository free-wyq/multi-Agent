"""
WebSocket 连接管理器

管理按 group_id 分组的 WebSocket 连接，将消息总线事件转发到浏览器。
"""
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """WebSocket 连接管理器"""

    def __init__(self) -> None:
        # group_id → set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, group_id: str) -> None:
        """接受 WebSocket 连接并注册到对应 group"""
        await websocket.accept()
        if group_id not in self._connections:
            self._connections[group_id] = set()
        self._connections[group_id].add(websocket)
        logger.info(
            "WebSocket connected: group=%s (total=%d)",
            group_id, len(self._connections[group_id]),
        )

    def disconnect(self, websocket: WebSocket, group_id: str) -> None:
        """移除 WebSocket 连接"""
        if group_id in self._connections:
            self._connections[group_id].discard(websocket)
            if not self._connections[group_id]:
                del self._connections[group_id]
        logger.info("WebSocket disconnected: group=%s", group_id)

    async def broadcast_to_group(self, group_id: str, message: dict) -> None:
        """向 group 内所有 WebSocket 客户端推送消息"""
        connections = self._connections.get(group_id, set())
        disconnected: set[WebSocket] = set()
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.add(ws)
        # 清理断开的连接
        for ws in disconnected:
            self._connections.get(group_id, set()).discard(ws)

    def has_connections(self, group_id: str) -> bool:
        """检查 group 是否还有活跃的 WebSocket 连接"""
        return bool(self._connections.get(group_id))

    async def handle_bus_message(self, message: dict) -> None:
        """Bus handler 回调：将消息总线事件转发到 WebSocket 客户端"""
        group_id = message.get("group_id")
        if not group_id:
            return
        # 构建 WebSocket 协议消息
        ws_message = {
            "type": message.get("type", "log"),
            "data": message,
        }
        await self.broadcast_to_group(group_id, ws_message)


# ── 全局单例 ──────────────────────────────────────────────────────

_ws_manager: WebSocketManager | None = None


def get_ws_manager() -> WebSocketManager:
    """获取全局 WebSocketManager 实例"""
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = WebSocketManager()
    return _ws_manager
