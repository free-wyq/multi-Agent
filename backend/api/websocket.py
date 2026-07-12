"""WebSocket endpoint: per-group bus channel.

Frontend `onBusEvent(groupId)` opens `ws://localhost:8000/ws/bus/{groupId}` and
receives BusEventData dicts pushed by `events.bus`. The socket stays open; inbound
text is ignored in M1.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from events import bus_manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/bus/{group_id}")
async def ws_bus(websocket: WebSocket, group_id: str) -> None:
    await websocket.accept()
    bus_manager.subscribe(group_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # Expected exit: client closed the socket. `pass` is correct — the
        # finally block below unsubscribes, so the disconnect is handled, not
        # swallowed. No log: a disconnect per page close is normal traffic,
        # logging it would flood (B31 错误处理重巡航——已注释说明有意吞没).
        pass
    finally:
        bus_manager.unsubscribe(group_id, websocket)
