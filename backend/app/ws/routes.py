"""
WebSocket 路由

提供实时 WebSocket 端点，客户端连接 ws://localhost:8000/ws/{group_id}
即可接收该群组的消息总线事件。
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws.manager import get_ws_manager
from app.bus.subscribers import ensure_group_subscription, remove_group_subscription

router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/{group_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str):
    """WebSocket 端点：按群组订阅实时消息"""
    manager = get_ws_manager()

    # 确保 bus → ws 订阅
    await ensure_group_subscription(group_id)

    await manager.connect(websocket, group_id)
    try:
        while True:
            # 保持连接；客户端可发送心跳或 user_input 消息
            data = await websocket.receive_text()
            # 未来可扩展：解析客户端消息，发布 user_input 事件到 bus
    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id)
        # 无更多连接时取消 bus 订阅
        if not manager.has_connections(group_id):
            await remove_group_subscription(group_id)
