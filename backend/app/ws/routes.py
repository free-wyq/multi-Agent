"""
WebSocket 路由

通讯模式（类微信）：
- WS 连接后，直接订阅 Redis 群组 channel
- 后端任何地方 publish 到该 channel，WS 客户端实时收到
- 不依赖 bus listener task（避免其 crash 导致推送失败）
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

CHANNEL_PREFIX = "agenticx:group:"


@router.websocket("/ws/{group_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str):
    """WebSocket 端点：直接订阅 Redis channel，实时推送消息到浏览器"""
    import redis.asyncio as aioredis

    await websocket.accept()
    logger.info("WebSocket connected: group=%s", group_id)

    r = aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=None,      # 无超时，长连接
        socket_connect_timeout=5,
    )
    pubsub = r.pubsub()
    channel = f"{CHANNEL_PREFIX}{group_id}"

    try:
        await pubsub.subscribe(channel)
    except Exception as e:
        logger.error("Failed to subscribe Redis channel %s: %s", channel, e)
        await websocket.close()
        return

    # 两个并发的协程：
    # 1. 从 Redis pub/sub 读消息 → 推到 WS
    # 2. 从 WS 读消息（保活 / 未来扩展）
    async def redis_to_ws():
        """Redis pub/sub → WebSocket"""
        try:
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                    # 包装成 WS 协议格式
                    ws_msg = {
                        "type": data.get("type", "log"),
                        "data": data,
                    }
                    await websocket.send_json(ws_msg)
                except (json.JSONDecodeError, TypeError):
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("redis_to_ws error: %s", e)

    async def ws_keepalive():
        """WebSocket 保活（读取客户端消息，防止连接超时）"""
        try:
            while True:
                data = await websocket.receive_text()
                # 未来可扩展：解析客户端消息
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    redis_task = asyncio.create_task(redis_to_ws())
    ws_task = asyncio.create_task(ws_keepalive())

    try:
        # 等任一协程结束
        done, pending = await asyncio.wait(
            [redis_task, ws_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # 取消剩余的
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await r.aclose()
        logger.info("WebSocket disconnected: group=%s", group_id)
