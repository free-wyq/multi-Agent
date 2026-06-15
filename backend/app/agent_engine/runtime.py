"""
子智能体运行时（AgentEngine）

每个子智能体 = 一个常驻 asyncio Task
- 有独立 in-memory 状态
- 从 Redis Stream inbox 收消息
- 用轻量 LLM（大脑）判断：聊天 vs 执行
- 执行时直接用内置的 Claude Code Docker（不额外挂载）
- 流式日志回传 Redis + WebSocket
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from app.agent_engine.brain import get_brain, BRAIN_PROMPT
from app.agent_engine.capability_base import CapabilityResult
from app.bus.core import get_bus, CHANNEL_PREFIX
from app.core.database import async_session
from app.runtime.claude_code_runtime import ClaudeCodeRuntime
from app.runtime.instance_pool import ContainerInstancePool
from app.services import message_service

logger = logging.getLogger(__name__)

# 全局实例池（子智能体共享）
_pool = ContainerInstancePool()


class AgentEngine:
    """子智能体引擎

    常驻进程，处理分配给该智能体的所有消息。
    """

    def __init__(self, agent_definition, group_id: str):
        self.id = agent_definition.id          # 智能体定义 ID
        self.name = agent_definition.name
        self.role = agent_definition.role
        self.system_prompt = agent_definition.system_prompt or ""
        self.group_id = group_id

        self.status = "idle"                   # idle / thinking / executing / offline
        self.current_task_id: str | None = None
        self._inbox = asyncio.Queue()          # 内部消息队列
        self._task: asyncio.Task | None = None # 主循环 task
        self._shutdown = False

        # 大脑 LLM（轻量，用于判断和聊天回复）
        self._brain = get_brain()

        # 记忆：仅内存，重启清空（后续加 Redis/DB）
        self._memory: list[dict] = []          # [{role, content, ts}]

        # 运行时引用（execute 时从实例池获取）
        self._runtime: ClaudeCodeRuntime | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动常驻任务"""
        self._shutdown = False
        self.status = "idle"
        self._task = asyncio.create_task(self._main_loop(), name=f"agent-{self.id[:8]}")
        logger.info("AgentEngine 启动: %s (%s)", self.name, self.id[:8])

    async def stop(self) -> None:
        """停止常驻任务"""
        self._shutdown = True
        if self._runtime:
            await _pool.release(self._runtime, strategy="on_demand")
            self._runtime = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.status = "offline"
        logger.info("AgentEngine 停止: %s", self.name)

    async def push_message(self, message: dict) -> None:
        """外部推送消息到 inbox"""
        await self._inbox.put(message)

    # ── 主循环 ────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """主循环：收消息 → 大脑决策 → 执行/回复"""
        while not self._shutdown:
            try:
                msg = await asyncio.wait_for(self._inbox.get(), timeout=60.0)
            except asyncio.TimeoutError:
                continue

            if msg.get("type") == "task_dispatch":
                # 群主直接派活：跳过大脑，直接执行
                await self._do_execute(msg)
            else:
                # 普通消息：大脑判断
                await self._handle_message(msg)

    # ── 消息处理 ──────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """处理普通消息：先问大脑"""
        self.status = "thinking"
        content = msg.get("content", "")
        sender = msg.get("sender_id", "user")

        # 1. 加载上下文（最近 5 条记忆）
        context = self._build_context()

        # 2. 构建消息内容（如果是其他智能体发来的，标注来源）
        if sender not in ("user", "coordinator"):
            display_msg = f"[来自智能体 {sender}] {content}"
        else:
            display_msg = content

        # 3. 调用大脑决策
        prompt = BRAIN_PROMPT.format(
            role=self.role,
            name=self.name,
            context=context,
            message=display_msg,
        )

        try:
            decision = await self._brain.ainvoke(prompt)
        except Exception as exc:
            logger.error("大脑决策失败: %s", exc)
            await self._reply("抱歉，我这边有点卡壳，能再说一遍吗？", parent_msg=msg)
            self.status = "idle"
            return

        logger.info(
            "大脑决策: %s action=%s reasoning=%s",
            self.name, decision.action, decision.reasoning,
        )

        # 3. 记忆入库
        self._memory.append({"role": "user", "content": content, "ts": datetime.now(timezone.utc).isoformat()})

        # 4. 按 action 处理
        if decision.action == "chat":
            await self._reply(decision.content, parent_msg=msg)
            self._memory.append({"role": "assistant", "content": decision.content, "ts": datetime.now(timezone.utc).isoformat()})
            self.status = "idle"

        elif decision.action == "execute":
            # 大脑说：要干活了
            # 先回复用户"我去干"
            await self._reply(f"收到，我来 {decision.content[:30]}...", parent_msg=msg)
            # 生成任务
            task_msg = {
                "type": "task_dispatch",
                "task_id": f"task-{uuid.uuid4().hex[:8]}",
                "content": decision.content,
                "sender_id": sender,
                "parent_msg": msg,
            }
            await self._do_execute(task_msg)

        elif decision.action == "ask":
            await self._reply(decision.content, parent_msg=msg)
            self.status = "idle"

        else:
            await self._reply(decision.content, parent_msg=msg)
            self.status = "idle"

    # ── 执行能力（内置 Claude Code Docker）───────────────────────────────

    async def _do_execute(self, msg: dict) -> None:
        """真正干活：启动 Docker 跑 Claude Code"""
        self.status = "executing"
        task_id = msg.get("task_id", f"task-{uuid.uuid4().hex[:8]}")
        self.current_task_id = task_id
        task_content = msg.get("content", "")

        logger.info("[%s] 开始执行: %s", self.name, task_content[:50])

        # 1. 广播"开始执行"日志
        await self._publish_log(task_id, f"▶ [{self.name}] 开始执行任务...")

        from app.models.agent_definition import AgentDefinition
        async with async_session() as db:
            agent_def = await db.get(AgentDefinition, self.id)
        if not agent_def:
            await self._publish_log(task_id, "❌ 找不到智能体定义")
            self.status = "idle"
            return

        # 2. 从实例池获取/创建 Docker 运行时
        try:
            self._runtime = await _pool.acquire(agent_def, self.group_id)
            await self._publish_log(task_id, f"🐳 容器 {self._runtime.container_name or 'new'} 就绪")
        except Exception as exc:
            logger.error("获取容器失败: %s", exc)
            await self._publish_log(task_id, f"❌ 启动容器失败: {exc}")
            self.status = "idle"
            return

        # 3. 执行（阻塞，但日志通过轮询或后续改造流式）
        result = None
        try:
            # 注意：当前 runtime.execute 是阻塞返回 AgentResult
            # 先简单处理：等完成后发日志
            result = await self._runtime.execute(task_content, task_id=task_id)

            # 4. 日志和结果回传
            if result.output:
                for line in result.output.splitlines()[:50]:  # 限制行数
                    await self._publish_log(task_id, line)

            if result.success:
                await self._publish_log(task_id, f"✅ 任务完成 (exit={result.exit_code})")
                # 回复群聊
                summary = result.output[:200] if result.output else "已完成"
                await self._reply(f"任务完成 🎉\n{summary}", task_id=task_id)
            else:
                await self._publish_log(task_id, f"❌ 执行失败 (exit={result.exit_code})")
                await self._reply(f"执行出错了: {result.output or '未知错误'}", task_id=task_id)

        except Exception as exc:
            logger.error("执行异常: %s", exc)
            await self._publish_log(task_id, f"❌ 执行异常: {exc}")
            await self._reply(f"执行出错了: {exc}", task_id=task_id)

        finally:
            # 5. 释放容器回实例池
            strategy = getattr(agent_def, "startup_strategy", "on_demand")
            if self._runtime:
                await _pool.release(self._runtime, strategy=strategy)
                self._runtime = None

            self.current_task_id = None
            self.status = "idle"

    # ── 辅助方法 ──────────────────────────────────────────────────────

    async def _reply(self, content: str, *, parent_msg: dict | None = None, task_id: str | None = None) -> None:
        """在群聊中回复用户"""
        await self._save_and_publish(
            content=content,
            msg_type="agent_reply",
            task_id=task_id,
            parent_msg=parent_msg,
        )

    async def _publish_log(self, task_id: str, line: str) -> None:
        """发布执行日志到 Redis（前端 WS 会推送）"""
        try:
            bus = get_bus()
            channel = f"{CHANNEL_PREFIX}{self.group_id}"
            await bus.publish(channel, {
                "id": str(uuid.uuid4()),
                "group_id": self.group_id,
                "task_id": task_id,
                "sender_id": self.id,
                "receiver_id": "broadcast",
                "type": "task_log",
                "content": line,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.warning("发布日志失败: %s", exc)

    async def _save_and_publish(
        self,
        content: str,
        msg_type: str,
        task_id: str | None = None,
        parent_msg: dict | None = None,
    ) -> None:
        """保存消息到 DB + 发布到 Redis + 检查 @mention 路由到其他智能体"""
        msg_data = {
            "group_id": self.group_id,
            "sender_id": self.id,
            "receiver_id": "broadcast",
            "type": msg_type,
            "content": content,
            "task_id": task_id,
        }

        # 存 DB
        try:
            async with async_session() as db:
                try:
                    await message_service.create_message(db, msg_data)
                    await db.commit()
                except Exception:
                    await db.rollback()
        except Exception as exc:
            logger.warning("保存消息失败: %s", exc)

        # 发 Redis
        try:
            bus = get_bus()
            channel = f"{CHANNEL_PREFIX}{self.group_id}"
            await bus.publish(channel, {
                "id": str(uuid.uuid4()),
                "group_id": self.group_id,
                "task_id": task_id,
                "sender_id": self.id,
                "receiver_id": "broadcast",
                "type": msg_type,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.warning("发布消息失败: %s", exc)

        # 检查 @mention 并路由到其他智能体
        await self._route_mentions(content)

    def _build_context(self) -> str:
        """构建最近对话上下文"""
        recent = self._memory[-5:]
        lines = []
        for m in recent:
            who = "用户" if m["role"] == "user" else self.name
            lines.append(f"{who}: {m['content']}")
        return "\n".join(lines) if lines else "（无历史对话）"

    async def _route_mentions(self, content: str) -> None:
        """检查回复中的 @mention，将消息路由到被 @ 的智能体"""
        import re
        import sqlalchemy as sa
        from app.models.agent_definition import AgentDefinition
        from app.models.group_member import GroupMember

        mentions = re.findall(r"@(\S+)", content)
        if not mentions:
            return

        # 防循环：记录最近 30s 内已路由过的 (target_id) ，同一目标不重复路由
        now = asyncio.get_event_loop().time()
        if not hasattr(self, '_recent_routes'):
            self._recent_routes = {}  # target_id -> timestamp
        # 清理过期的
        self._recent_routes = {k: v for k, v in self._recent_routes.items() if now - v < 30}

        try:
            async with async_session() as db:
                # 获取群成员
                result = await db.execute(
                    sa.select(GroupMember.agent_id, GroupMember.alias)
                    .where(GroupMember.group_id == self.group_id)
                )
                members = {row[0]: row[1] for row in result.all()}
                if not members:
                    return

                # 获取成员名字
                result = await db.execute(
                    sa.select(AgentDefinition.id, AgentDefinition.name)
                    .where(AgentDefinition.id.in_(list(members.keys())))
                )
                name_map = {row[1]: row[0] for row in result.all()}

                from app.agent_engine import get_registry
                registry = get_registry()

                for mention in mentions:
                    # 不路由给自己
                    if mention == self.id or mention == self.name:
                        continue

                    target_id = None
                    if mention in members:
                        target_id = mention
                    elif mention in name_map:
                        target_id = name_map[mention]
                    else:
                        # 模糊匹配 alias
                        for aid, alias in members.items():
                            if alias and mention in alias:
                                target_id = aid
                                break

                    if not target_id or target_id == self.id:
                        continue

                    # 防循环：30s 内不重复路由同一目标
                    if target_id in self._recent_routes:
                        logger.info("防循环: 跳过重复路由 %s (30s内)", target_id[:8])
                        continue
                    self._recent_routes[target_id] = now

                    logger.info("智能体 @mention 路由: %s -> %s", self.name, target_id[:8])
                    await registry.route_message(target_id, {
                        "type": "chat",
                        "content": content,
                        "sender_id": self.id,
                        "group_id": self.group_id,
                    }, group_id=self.group_id)
        except Exception as exc:
            logger.warning("@mention 路由失败: %s", exc)
