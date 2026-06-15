"""
消息总线核心

基于 Redis Pub/Sub 的异步消息总线，提供：
- publish / subscribe / unsubscribe 基础能力
- publish_and_persist 便捷方法（发布 + 持久化到 Message 表）
- 后台 listener task 自动分发消息到注册的 handler
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

# 消息处理回调类型
BusHandler = Callable[[dict], Awaitable[None]]

# Channel 前缀
CHANNEL_PREFIX = "agenticx:group:"


class MessageBus:
    """Redis Pub/Sub 消息总线"""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.PubSub | None = None
        # channel → [handler, ...]
        self._subscriptions: dict[str, list[BusHandler]] = {}
        self._listener_task: asyncio.Task | None = None

    # ── 生命周期 ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """初始化 Redis 连接（FastAPI lifespan 启动时调用）"""
        self._redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
        self._pubsub = self._redis.pubsub()
        logger.info("MessageBus connected to Redis: %s", settings.REDIS_URL)

    async def disconnect(self) -> None:
        """关闭 Redis 连接（FastAPI lifespan 关闭时调用）"""
        # 停止 listener
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
            self._pubsub = None

        if self._redis:
            await self._redis.close()
            self._redis = None

        self._subscriptions.clear()
        logger.info("MessageBus disconnected from Redis")

    # ── 发布 ──────────────────────────────────────────────────────

    async def publish(self, channel: str, message: dict) -> None:
        """发布消息到 Redis channel"""
        if not self._redis:
            raise RuntimeError("MessageBus not connected — call connect() first")
        payload = json.dumps(message, ensure_ascii=False, default=str)
        await self._redis.publish(channel, payload)
        logger.debug("Published to %s: type=%s", channel, message.get("type"))

    async def publish_and_persist(
        self,
        channel: str,
        *,
        group_id: str,
        task_id: str | None = None,
        sender_id: str,
        receiver_id: str,
        type: str,
        content: str | None = None,
        data: dict | None = None,
    ) -> dict:
        """发布消息到 bus 并持久化到 Message 表

        返回完整的消息 dict（含自动生成的 id 和 timestamp）。
        即使 DB 写入失败，消息仍会发布到 Redis。
        """
        message = {
            "id": str(uuid.uuid4()),
            "group_id": group_id,
            "task_id": task_id,
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "type": type,
            "content": content,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 持久化到 DB
        try:
            from app.core.database import async_session
            from app.services import message_service

            async with async_session() as db:
                try:
                    await message_service.create_message(db, {
                        "id": message["id"],
                        "group_id": group_id,
                        "task_id": task_id,
                        "sender_id": sender_id,
                        "receiver_id": receiver_id,
                        "type": type,
                        "content": content,
                        "data": data,
                    })
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception as exc:
            logger.warning("Failed to persist message to DB: %s", exc)
            # 仍然继续发布

        # 发布到 Redis
        await self.publish(channel, message)
        return message

    # ── 订阅 ──────────────────────────────────────────────────────

    async def subscribe(self, channel: str, handler: BusHandler) -> None:
        """订阅 channel 并注册处理回调

        同一 handler 在同一 channel 上只注册一次（幂等）。
        首次订阅某 channel 时会在 Redis 层执行 subscribe。
        """
        if channel not in self._subscriptions:
            self._subscriptions[channel] = []

        # 幂等：相同 handler 不重复注册
        if handler in self._subscriptions[channel]:
            return

        is_new_channel = len(self._subscriptions[channel]) == 0
        self._subscriptions[channel].append(handler)

        # Redis 层 subscribe（仅首次）
        if is_new_channel and self._pubsub:
            await self._pubsub.subscribe(channel)
            logger.info("Subscribed to Redis channel: %s", channel)

        # 确保 listener 在运行
        self._ensure_listener()

    async def unsubscribe(self, channel: str, handler: BusHandler | None = None) -> None:
        """取消订阅

        - 传入 handler：仅移除该 handler
        - 不传 handler：移除该 channel 的所有 handler
        """
        if channel not in self._subscriptions:
            return

        if handler:
            self._subscriptions[channel] = [
                h for h in self._subscriptions[channel] if h != handler
            ]
        else:
            self._subscriptions[channel] = []

        # 无 handler 时，Redis 层 unsubscribe
        if not self._subscriptions[channel]:
            del self._subscriptions[channel]
            if self._pubsub:
                await self._pubsub.unsubscribe(channel)
                logger.info("Unsubscribed from Redis channel: %s", channel)

    # ── 内部 ──────────────────────────────────────────────────────

    def _ensure_listener(self) -> None:
        """确保后台 listener task 在运行"""
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """后台 task：从 Redis pub/sub 读取消息并分发到 handlers"""
        if not self._pubsub:
            return
        try:
            async for raw in self._pubsub.listen():
                if raw["type"] != "message":
                    continue

                channel = raw["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")

                try:
                    message = json.loads(raw["data"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Invalid JSON on channel %s", channel)
                    continue

                handlers = self._subscriptions.get(channel, [])
                for handler in handlers:
                    try:
                        await handler(message)
                    except Exception as exc:
                        logger.error(
                            "Handler error on channel %s: %s", channel, exc
                        )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("MessageBus listener error: %s", exc)


# ── 全局单例 ──────────────────────────────────────────────────────

_bus: MessageBus | None = None


def get_bus() -> MessageBus:
    """获取全局 MessageBus 实例"""
    global _bus
    if _bus is None:
        _bus = MessageBus()
    return _bus
