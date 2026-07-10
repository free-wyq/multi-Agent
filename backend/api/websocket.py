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
        pass
    finally:
        bus_manager.unsubscribe(group_id, websocket)
