"""AgentEngine + AgentRegistry (Rust engine.rs).

``AgentEngine`` is a resident asyncio.Task that owns an asyncio.Queue inbox
channel and, per incoming item, invokes the LangGraph graph once. Cross-invoke
state (memory, dispatch_plan, recent_routes) is held on the engine instance
and injected into each ``ainvoke``; the MemorySaver checkpointer + thread_id
preserve graph-internal state across invocations.

``AgentRegistry`` manages the engine instances keyed by group_id -> agent_id
and provides ``load_from_store`` (startup) and ``shutdown_all`` (shutdown).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from engine import coordinator as coord_mod
from engine import worker as worker_mod
from engine.coordinator import build_coordinator_graph
from engine.inbox import (
    complete_task,
    get_inbox,
    push_notify,
    register_inbox,
    unregister_inbox,
)
from engine.cli_executor import execute_claude_cli
from engine.mention import route_mentions
from engine.worker import build_worker_graph
from events import emit_message_added, emit_task_completed, emit_task_log
from store import crud

logger = logging.getLogger("multi-agent.registry")


class AgentEngine:
    """Resident engine for one agent in one group.

    Owns the asyncio.Queue inbox channel, the compiled LangGraph graph, and the
    cross-invoke state (memory, dispatch_plan, recent_routes, pending_tasks).
    The run loop blocks on ``inbox.get`` with a 1s timeout so shutdown can
    interrupt it.
    """

    def __init__(
        self,
        agent_def: dict[str, Any],
        group_id: str,
        coordinator_id: str = "",
    ) -> None:
        self.agent_id: str = agent_def["id"]
        self.name: str = agent_def["name"]
        self.role: str = agent_def.get("role", "")
        self.group_id: str = group_id
        # 判定群主：谁被设为该群的 coordinator_id，谁就是协调者——
        # 不按 role 字符串判定（role 是创建时设的数据，群主身份是群组级配置）。
        self.is_coordinator: bool = self.agent_id == coordinator_id
        self.coordinator_id: str = coordinator_id
        self.status: str = "idle"  # idle | executing | offline
        self.current_task_id: str | None = None
        self._shutdown: bool = False
        self._task: asyncio.Task | None = None
        self._memory: list[dict[str, str]] = []
        self._dispatch_plan: list[dict[str, Any]] = []
        self._recent_routes: dict[str, float] = {}
        self._pending_tasks: list[dict[str, Any]] = []  # backlog while executing

        if self.is_coordinator:
            self.graph = build_coordinator_graph()
        else:
            self.graph = build_worker_graph()
        self.thread_id = f"{group_id}:{self.agent_id}"

    async def start(self) -> None:
        register_inbox(self.group_id, self.agent_id)
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "[engine] %s (role=%s) started in group %s",
            self.name, self.role, self.group_id,
        )

    async def stop(self) -> None:
        self._shutdown = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        unregister_inbox(self.group_id, self.agent_id)
        self.status = "offline"
        logger.info("[engine] %s stopped", self.name)

    async def _run_loop(self) -> None:
        """Consume the inbox queue until shutdown. 1s timeout for interruptibility."""
        inbox = get_inbox(self.group_id, self.agent_id)
        while not self._shutdown:
            try:
                item = await asyncio.wait_for(inbox.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle_inbox_item(item)
            except Exception:
                logger.exception(
                    "[engine %s] error handling inbox item", self.name
                )

    async def _handle_inbox_item(self, item: dict[str, Any]) -> None:
        if item["kind"] == "task":
            await self._handle_task(item["item"])
        elif item["kind"] == "notify":
            await self._handle_notify(item["item"])

    # ── task handling ────────────────────────────────────────────────

    async def _handle_task(self, task: dict[str, Any]) -> None:
        """Process a task item. Busy -> backlog; otherwise execute (Rust handle_inbox task branch)."""
        if self.status == "executing":
            self._pending_tasks.append(task)
            return

        self.status = "executing"
        self.current_task_id = task["id"]
        preview = (task.get("content") or "")[:50]
        await self._publish_log(task["id"], f"▶ [{self.name}] 开始执行任务: {preview}...")

        if self.is_coordinator:
            # coordinator does not run the CLI; treat the task as a user demand
            # and trigger the coordinator graph via a synthetic notify.
            await complete_task(task["id"], True, "协调者已接收需求，开始调度。")
            notify = {
                "id": task["id"],
                "group_id": self.group_id,
                "type": "coordinator_task",
                "sender_id": "user",
                "receiver_id": self.agent_id,
                "content": task.get("content") or "",
                "data": task.get("data"),
                "created_at": task.get("created_at", ""),
            }
            await self._handle_notify(notify)
        else:
            await self._run_worker_task(task)

        self._reset_idle(task["id"])
        await self._drain_pending()

    async def _run_worker_task(self, task: dict[str, Any]) -> None:
        """Worker task execution. M3 uses the mock CLI executor; M5 swaps in real Claude Code CLI."""
        agent_def = await crud.get_agent(self.agent_id)
        if not agent_def:
            await complete_task(task["id"], False, "找不到智能体定义")
            await self._publish_log(task["id"], "❌ 找不到智能体定义")
            return

        agent_dict = agent_def.model_dump()
        group_id = self.group_id
        agent_id = self.agent_id
        task_id = task["id"]
        task_content = task.get("content") or ""

        async def on_log(line: str) -> None:
            await emit_task_log(group_id, task_id, agent_id, line)

        result = await execute_claude_cli(
            group_id, agent_dict, task_content, task_id, on_log
        )

        snippet = (result.get("output") or "")[:200]
        success = bool(result.get("success"))
        exit_code = result.get("exit_code")

        await emit_task_completed(
            group_id,
            task_id,
            agent_id,
            success,
            (result.get("output") or "")[:500],
            exit_code,
        )

        await complete_task(
            task_id,
            success,
            (result.get("output") or "")[:500],
            {"exit_code": exit_code},
        )

        if success:
            reply = (
                "任务完成 🎉"
                if not snippet
                else f"任务完成 🎉\n{snippet}"
            )
        else:
            reply = f"执行出错了: {result.get('output') or ''}"
        await self._reply(reply)

        # single agent_reply notify to coordinator (Rust 318-339)
        # 用 engine 启动时缓存的 coordinator_id，不再二次查库
        if self.coordinator_id and self.coordinator_id != agent_id:
            await push_notify(
                group_id,
                "agent_reply",
                agent_id,
                self.coordinator_id,
                f"步骤完成：{task_content}\n\n结果：{snippet or '已完成'}",
                {"task_id": task_id, "success": success},
            )

    # ── notify handling ──────────────────────────────────────────────

    async def _handle_notify(self, notify: dict[str, Any]) -> None:
        """Process a notify item via the LangGraph graph."""
        if notify.get("sender_id") == self.agent_id:
            return

        async def reply_cb(content: str) -> None:
            """Engine-side mention routing callback for graph nodes."""
            await route_mentions(
                self.group_id,
                self.agent_id,
                self.name,
                content,
                self._recent_routes,
            )

        if self.is_coordinator:
            coord_mod.set_reply_callback(reply_cb)
            try:
                result = await self.graph.ainvoke(
                    {
                        "group_id": self.group_id,
                        "agent_id": self.agent_id,
                        "agent_name": self.name,
                        "incoming_message": notify.get("content") or "",
                        "incoming_sender": notify.get("sender_id") or "",
                        "incoming_kind": notify.get("type") or "",
                        "incoming_data": notify.get("data"),
                        "memory": self._memory,
                        "dispatch_plan": self._dispatch_plan,
                        "recent_routes": self._recent_routes,
                    },
                    config={"configurable": {"thread_id": self.thread_id}},
                )
            finally:
                coord_mod.set_reply_callback(None)
            # sync dispatch_plan back from graph result (nodes mutate it)
            if result and isinstance(result, dict):
                updated_plan = result.get("dispatch_plan")
                if updated_plan is not None:
                    self._dispatch_plan = list(updated_plan)
            # record memory (user side)
            self._memory.append(
                {
                    "role": "user",
                    "content": f"[{notify.get('sender_id')}] {notify.get('content')}",
                }
            )
        else:
            worker_mod.set_reply_callback(reply_cb)
            try:
                result = await self.graph.ainvoke(
                    {
                        "group_id": self.group_id,
                        "agent_id": self.agent_id,
                        "agent_name": self.name,
                        "agent_role": self.role,
                        "incoming_message": notify.get("content") or "",
                        "incoming_sender": notify.get("sender_id") or "",
                        "memory": self._memory,
                    },
                    config={"configurable": {"thread_id": self.thread_id}},
                )
            finally:
                worker_mod.set_reply_callback(None)
            self._memory.append(
                {"role": "user", "content": notify.get("content") or ""}
            )
            # if the brain decided chat/ask, record the assistant reply
            decision = (result or {}).get("decision") or {}
            if decision.get("action") in ("chat", "ask"):
                self._memory.append(
                    {"role": "assistant", "content": decision.get("content", "")}
                )

    # ── unified reply / publish (Rust engine.reply / publish_log) ────

    async def _reply(self, content: str) -> None:
        """Persist an agent_reply message + emit + mention route (Rust engine.reply)."""
        msg = await crud.create_message(
            {
                "group_id": self.group_id,
                "task_id": None,
                "sender_id": self.agent_id,
                "receiver_id": "broadcast",
                "type": "agent_reply",
                "content": content,
                "data": None,
            }
        )
        await emit_message_added(msg.model_dump())
        await route_mentions(
            self.group_id,
            self.agent_id,
            self.name,
            content,
            self._recent_routes,
        )

    async def _publish_log(self, task_id: str | None, line: str) -> None:
        """Persist a task_log message + emit (Rust engine.publish_log)."""
        msg = await crud.create_message(
            {
                "group_id": self.group_id,
                "task_id": task_id,
                "sender_id": self.agent_id,
                "receiver_id": "broadcast",
                "type": "task_log",
                "content": line,
                "data": None,
            }
        )
        await emit_message_added(msg.model_dump())

    def _reset_idle(self, task_id: str) -> None:
        self.status = "idle"
        self.current_task_id = None

    async def _drain_pending(self) -> None:
        """After finishing a task, process the backlog (Rust drain_pending)."""
        if self.status != "idle" or not self._pending_tasks:
            return
        next_task = self._pending_tasks.pop(0)
        await self._handle_task(next_task)


class AgentRegistry:
    """group_id -> agent_id -> AgentEngine."""

    def __init__(self) -> None:
        self._engines: dict[str, dict[str, AgentEngine]] = {}

    async def add_engine(
        self,
        group_id: str,
        agent_def: dict[str, Any],
        coordinator_id: str = "",
    ) -> AgentEngine:
        if group_id not in self._engines:
            self._engines[group_id] = {}
        if agent_def["id"] in self._engines[group_id]:
            return self._engines[group_id][agent_def["id"]]
        engine = AgentEngine(agent_def, group_id, coordinator_id)
        await engine.start()
        self._engines[group_id][agent_def["id"]] = engine
        return engine

    async def remove_engine(self, group_id: str, agent_id: str) -> None:
        group = self._engines.get(group_id)
        if not group or agent_id not in group:
            return
        await group[agent_id].stop()
        del group[agent_id]
        if not group:
            del self._engines[group_id]

    def get_engine(self, group_id: str, agent_id: str) -> AgentEngine | None:
        return self._engines.get(group_id, {}).get(agent_id)

    async def load_from_store(self) -> None:
        """Spin up an engine for every coordinator + member across all groups.

        同一群组内所有引擎共享同一个 coordinator_id，引擎据此判定谁是群主
        （谁 == coordinator_id 谁就是协调者），不依赖 role 字符串。
        """
        groups = await crud.list_groups()
        for g in groups:
            coord_id = g.coordinator_id or ""
            if coord_id:
                coord = await crud.get_agent(coord_id)
                if coord:
                    await self.add_engine(g.id, coord.model_dump(), coord_id)
            members = await crud.list_group_members_with_agent(g.id)
            for m in members:
                agent = await crud.get_agent(m.agent_id)
                if agent:
                    await self.add_engine(g.id, agent.model_dump(), coord_id)
        logger.info(
            "[registry] loaded %d group(s) with engines",
            len(self._engines),
        )

    async def shutdown_all(self) -> None:
        for group in list(self._engines.values()):
            for engine in list(group.values()):
                await engine.stop()
        self._engines.clear()

    def list_group_status(self, group_id: str) -> list[dict[str, Any]]:
        """Return agent statuses for a group (used by GET /api/status/{groupId})."""
        out: list[dict[str, Any]] = []
        for aid, eng in self._engines.get(group_id, {}).items():
            out.append(
                {
                    "id": aid,
                    "name": eng.name,
                    "role": eng.role,
                    "status": eng.status,
                    "current_task_id": eng.current_task_id,
                }
            )
        return out


registry = AgentRegistry()
