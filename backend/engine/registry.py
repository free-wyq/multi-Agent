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
from engine.group_runtime import GroupRuntime
from engine.inbox import (
    complete_task,
    get_inbox,
    push_notify,
    register_inbox,
    unregister_inbox,
)
from engine.agent_executor import execute_agent_task
from engine.mention import clear_group_routes, route_mentions
from engine.reply import persist_agent_reply
from engine.worker import build_worker_graph
from engine.workspace import scan_workspace_artifacts
from langgraph.graph import END
from langgraph.types import Command
from events import (
    emit_agent_status,
    emit_message_added,
    emit_task_completed,
    emit_task_log,
    emit_task_think,
    emit_task_token,
    emit_task_tool,
)
from config import WORKER_TASK_TIMEOUT
from llm import TEAM_INTERACTION_SUFFIX
from models import get_leader_strategy
from store import crud

logger = logging.getLogger("multi-agent.registry")


class AgentEngine:
    """Resident engine for one agent in one group.

    Owns the asyncio.Queue inbox channel, the compiled LangGraph graph, and the
    cross-invoke state (memory, dispatch_plan, recent_routes, pending_tasks).
    The run loop blocks on ``inbox.get`` with a 1s timeout so shutdown can
    interrupt it.

    时效口径契约（B11 文档化「群主运行期不可变」）—引擎字段分两层时效，勿混：

    身份层（startup-baked，``__init__`` 落定，引擎生命周期内不再变）：
    ``agent_id`` / ``group_id`` / ``coordinator_id`` / ``is_coordinator`` /
    ``graph_kind`` / ``single_chat`` / ``system_prompt``。``is_coordinator`` 派生
    ``graph_kind`` → 决定编译哪张 LangGraph 图（coordinator 图 vs worker 图），这是
    引擎*身份*非消息级配置。每 notify 刷新 ``coordinator_id`` 会要求重建图 + 作废
    MemorySaver checkpointer 线程（coordinator 线程的 dispatch_plan/interrupt 状态
    worker 图无法解释），代价高且状态腐蚀风险大，故启动缓存不再二次查库（见
    ``_run_worker_task`` 的 report-back notify 用 ``self.coordinator_id``）。

    配置层（per-invoke，``_handle_notify`` 每次 ``crud.get_group`` 现读）：
    ``auto_confirm`` / ``leader_strategy``。消息级行为旋钮（群设置 Modal / plan-direct
    API 可随时改），现读代价一次 DB 读（vs 下游 LLM 调用可忽略），缓存会冻结「等待确认
    vs 直接干」模式与指挥策略直到重启。

    后果——换群主是 pending-restart：``PUT /api/groups/{id}`` 改 ``coordinator_id``
    只落 DB 行，不重建驻留引擎；新群主的引擎仍跑启动烘焙的图（建群时是成员 → worker
    图），老群主的 coordinator 图被 ``route_user_message`` 等现读路由旁路闲置。换群主
    仅在进程重启或解散重建后生效。这是有意分层（图身份 ≠ 消息级配置），非 bug；要支持
    运行期换群主须实现引擎重建（高风险，未做）。
    """

    def __init__(
        self,
        agent_def: dict[str, Any],
        group_id: str,
        coordinator_id: str = "",
        single_chat: bool = False,
    ) -> None:
        self.agent_id: str = agent_def["id"]
        self.name: str = agent_def["name"]
        self.role: str = agent_def.get("role", "")
        self.group_id: str = group_id
        # B11 时效契约·身份层（startup-baked）：谁被设为该群的 coordinator_id，谁
        # 就是协调者——不按 role 字符串判定（role 是创建时设的数据，群主身份是群组级
        # 配置）。coordinator_id / is_coordinator / graph_kind 在 __init__ 落定，引擎
        # 生命周期内不再变（启动缓存「不再二次查库」是有意为之——换群主须重建图 +
        # 作废 checkpointer 线程，见类 docstring 时效口径契约）。auto_confirm /
        # leader_strategy 属配置层，在 _handle_notify 每 notify 现读（见下）。
        self.is_coordinator: bool = self.agent_id == coordinator_id
        self.coordinator_id: str = coordinator_id
        # agent 基础 system_prompt 缓存到引擎，供 coordinator 图（拼接 COORDINATOR_SYSTEM）
        # 与 worker 图（brain 注入 system 消息）在 _handle_notify 时读 state.system_prompt 用，
        # 避免 notify 每次回查 agent。
        self.system_prompt: str = agent_def.get("system_prompt", "") or ""
        # single_chat 标记：单聊群里唯一的 agent 虽被设为 coordinator_id（承接无 @mention 的
        # 消息），但行为应是「个体」而非「调度者」——按业内共识单 agent = 退化多 agent，
        # supervisor 只在多 agent 里存在，故单聊编译成 worker 图，不拼 COORDINATOR_SYSTEM。
        self.single_chat: bool = bool(single_chat)
        self.status: str = "idle"  # idle | executing | offline
        self.current_task_id: str | None = None
        self._shutdown: bool = False
        self._task: asyncio.Task | None = None
        self._memory: list[dict[str, str]] = []
        self._dispatch_plan: list[dict[str, Any]] = []
        self._recent_routes: dict[str, float] = {}
        self._pending_tasks: list[dict[str, Any]] = []  # backlog while executing
        self._worker_task: asyncio.Task | None = None  # PL-11: cancellable execution body
        self._cancel_requested: bool = False  # PL-11: set by a stop request
        self._timeout_fired: bool = False  # MT-17: set by the watchdog when a task times out

        # 选图：群聊 Leader（is_coordinator 且非单聊）→ coordinator 图；其余（单聊的
        # 唯一 agent、普通成员）→ worker 图。graph_kind 作为后续 is_coordinator 分支的
        # 判定基准（_handle_task 看门狗、_execute_body 分流），单聊 worker 也能挂看门狗、
        # 走 _run_worker_task（不再合成 coordinator_task notify 死循环）。
        #
        # 命名口径（见 docs/naming-conventions.md §1）：single_chat 是「输入」（群级配置
        # 标志），graph_kind 是「派生」（编译哪张图）——非两套平行分类，是同一条身份派生
        # 链的输入→输出。single_chat=True 把 is_coordinator=True 的 agent 降级成 worker 图
        # （单聊=退化的多智能体，supervisor 只在多 agent 里存在）。两轴各有读处，勿混。
        if self.is_coordinator and not self.single_chat:
            self.graph = build_coordinator_graph()
            self.graph_kind: str = "coordinator"
        else:
            self.graph = build_worker_graph()
            self.graph_kind = "worker"
        # 命名口径（见 docs/naming-conventions.md §2.3）：thread_id 是 LangGraph MemorySaver
        # 检查点键。驻留引擎图用稳定键 {group}:{agent}（跨 invoke 持久化图状态）；
        # create_react_agent（agent_loop.py:257）另用 task_id-or-uuid 的 per-exec 键。
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
        await emit_agent_status(
            self.group_id, self.agent_id, self.name, "offline", None
        )
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
        # PL-11: a task marked ``cancelled`` via ``inbox.cancel_task`` while it sat
        # in the queue or the ``_pending_tasks`` backlog is skipped without
        # execution. The dequeued item is the same dict ``push_task`` appended to
        # ``_task_queues`` and ``put`` onto the channel, so the marker set by
        # ``cancel_task`` is visible here without a re-query. Covers both the
        # fresh-dequeue path (this call from ``_handle_inbox_item``) and the
        # backlog-drain path (``_drain_pending`` → ``_handle_task``).
        if task.get("status") == "cancelled":
            logger.info(
                "[engine %s] skipping cancelled task %s", self.name, task["id"]
            )
            await self._publish_log(task["id"], "⏹ 任务已取消，跳过执行")
            return

        if self.status == "executing":
            self._pending_tasks.append(task)
            return

        self.status = "executing"
        self.current_task_id = task["id"]
        self._cancel_requested = False  # reset per task (no stale cancel carryover)
        await emit_agent_status(
            self.group_id, self.agent_id, self.name, "executing", task["id"]
        )
        preview = (task.get("content") or "")[:50]
        await self._publish_log(task["id"], f"▶ [{self.name}] 开始执行任务: {preview}...")

        # PL-11: run the execution body as a child asyncio.Task so a cancel
        # request can interrupt it. ``self._worker_task`` holds the handle so
        # ``request_cancel`` can cancel it; cleared in ``finally`` so the next
        # task (drained below) starts with a clean slot.
        self._worker_task = asyncio.create_task(self._execute_body(task))
        # MT-17: arm a watchdog that cancels the body if it produces no result
        # within ``worker_timeout`` (per-group override > WORKER_TASK_TIMEOUT).
        # Only armed for *worker* engines — the coordinator's dispatch is
        # non-blocking w.r.t. workers (it fans out then ENDS), so it won't hang
        # on worker execution, and killing the coordinator graph mid-invoke
        # risks leaving ``_dispatch_plan`` inconsistent (a coordinator-LLM hang
        # is better bounded at the httpx client, not here). Scoped to workers
        # matches the MT-17 "Worker 长时间无响应超时降级" spec precisely.
        self._timeout_fired = False
        watchdog = None
        if self.graph_kind == "worker":
            watchdog = await self._arm_timeout_watchdog(
                task, self._resolve_worker_timeout()
            )
        try:
            await self._worker_task
        except asyncio.CancelledError:
            # MT-17: a timeout-driven cancel is indistinguishable from a
            # user/PL-11 cancel at the asyncio layer — both cancel the child
            # Task. ``_timeout_fired`` (set by the watchdog before cancelling)
            # disambiguates: if the watchdog fired, this is a hung-worker
            # degradation, so we run the timeout-cleanup path instead of the
            # PL-11 user-stop path.
            if self._timeout_fired:
                self._timeout_fired = False
                logger.warning(
                    "[engine %s] task %s timed out (no result within watchdog window)",
                    self.name, task["id"],
                )
                await self._on_task_timed_out(task)
            elif self._cancel_requested:
                # PL-11: this cancel was requested by ``request_cancel`` (it set
                # the flag). Absorb it — the engine loop must survive a task
                # cancel so the agent returns to idle and the backlog drains.
                # (CancelledError is BaseException in Py3.8+, so ``_run_loop``'s
                # ``except Exception`` would NOT catch a re-raise — the loop
                # would die. We must swallow task-cancels here.)
                self._cancel_requested = False
                logger.info(
                    "[engine %s] task %s cancelled by request", self.name, task["id"]
                )
                await self._on_task_cancelled(task)
            else:
                # Not our cancel → the engine is shutting down (``stop()``
                # cancelled the loop mid-task). Let it propagate so the loop
                # exits cleanly.
                raise
        finally:
            self._worker_task = None
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()

        await self._reset_idle(task["id"])
        await self._drain_pending()

    async def _execute_body(self, task: dict[str, Any]) -> None:
        """The coordinator-or-worker execution body (PL-11: cancellable wrapper).

        Wraps the old ``_handle_task`` post-setup logic so it runs as a child
        Task. Split out from ``_handle_task`` to enable cancellation without
        touching the queue loop: cancelling ``self._worker_task`` cancels
        ``self._execute_body`` (and everything it awaits, incl. the LLM call),
        while ``_handle_task``'s post-run ``_reset_idle``/``_drain_pending``
        still execute to leave the engine in a clean idle state.
        """
        if self.graph_kind == "coordinator":
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

    def request_cancel(self, task_id: str | None = None) -> bool:
        """PL-11: cancel the current executing task. Returns whether a cancel was issued.

        Sets ``_cancel_requested`` and cancels the child ``_worker_task``. The
        next ``await`` in the execution body raises ``CancelledError``, the
        body unwinds, and ``_handle_task`` catches it, runs ``_reset_idle``,
        and the engine returns to idle (backlog drained next).

        ``task_id`` is optional: if provided, the cancel is only issued when it
        matches the engine's ``current_task_id`` (race-guard against stopping
        the wrong task after a status change). If omitted, cancel whatever is
        running.
        """
        if self.status != "executing" or self._worker_task is None:
            return False
        if task_id is not None and task_id != self.current_task_id:
            return False
        self._cancel_requested = True
        self._worker_task.cancel()
        return True

    async def _run_worker_task(self, task: dict[str, Any]) -> None:
        """Worker task execution via the agentic loop (M5: LLM + bind_tools)."""
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

        async def on_log(kind: str, content: str, data: dict | None = None) -> None:
            if kind in ("tool_start", "tool_end"):
                phase = "start" if kind == "tool_start" else "end"
                await emit_task_tool(
                    group_id,
                    task_id,
                    agent_id,
                    phase,
                    (data or {}).get("name", ""),
                    content,
                    data,
                )
            elif kind == "token":
                # PL-08: per-token streaming delta → task_token WS event
                await emit_task_token(
                    group_id,
                    task_id,
                    agent_id,
                    (data or {}).get("phase", "streaming"),
                    content,
                )
            elif kind in ("think", "answer"):
                await emit_task_think(
                    group_id,
                    task_id,
                    agent_id,
                    "thinking" if kind == "think" else "final",
                    content,
                )
            else:
                await emit_task_log(group_id, task_id, agent_id, content)

        result = await execute_agent_task(
            group_id, agent_dict, task_content, task_id, on_log
        )

        snippet = (result.get("output") or "")[:200]
        success = bool(result.get("success"))
        exit_code = result.get("exit_code")

        # PL-12: scan the group workspace for file artifacts produced during
        # this task, then persist them onto the task row so the task card /
        # download entry can surface them. Only scan on success — a failed or
        # cancelled task typically leaves no useful artifacts, and the scan
        # is wasted work. The scan is shallow + bounded (see
        # ``scan_workspace_artifacts``) so even a workspace that contains a
        # large generated dependency tree stays cheap. ``set_task_artifact``
        # is a no-op (returns None) if the task_id isn't persisted (it never
        # throws) — e.g. coordinator-only synthetic tasks that aren't rows.
        artifact_path: str | None = None
        artifact: dict | None = None
        if success:
            try:
                manifest = scan_workspace_artifacts(group_id)
                files = manifest.get("files") or []
                if files:
                    artifact_path = files[0]["path"]
                    artifact = manifest
            except Exception:
                logger.exception(
                    "[engine %s] artifact scan failed for task %s",
                    self.name, task_id,
                )
            if artifact_path:
                try:
                    await crud.set_task_artifact(task_id, artifact_path, artifact)
                except Exception:
                    logger.exception(
                        "[engine %s] failed to persist artifact for task %s",
                        self.name, task_id,
                    )
                await self._publish_log(
                    task_id, f"📦 产物已记录：{artifact_path}"
                    + (f"（共 {len((artifact or {}).get('files', []))} 个文件）"
                       if artifact and len(artifact.get("files", [])) > 1 else "")
                )

        await emit_task_completed(
            group_id,
            task_id,
            agent_id,
            success,
            (result.get("output") or "")[:500],
            exit_code,
            artifact,
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
        # B22：透传 task_id，让前端 finalizedBubbles 按 task_id 精确退场（非 sender+时间戳）。
        await self._reply(reply, task_id)

        # execute report-back → the per-group GroupRuntime (task-19④).
        #
        # Split-brain fix: the group graph's ``node_dispatch_next_group`` fans
        # the plan out via LangGraph ``Send``, marking steps ``dispatched`` +
        # ``task_id`` synced onto ``rt._dispatch_plan``. The worker's report-back
        # MUST return to the SAME runtime so the centralized path's
        # ``node_classify_incoming`` (task_id match against ``rt._dispatch_plan``)
        # hits and routes to ``handle_reply_group`` (MT-15 recovery / MT-14 step
        # adjustment). The old ``push_notify → resident coordinator engine``
        # path injected a STALE ``coordinator_engine._dispatch_plan`` that the
        # Send fan-out never touched → task_id mismatch → misroute to
        # llm_decide → plan deadlock. ``invoke_turn(incoming_kind="agent_reply",
        # incoming_data={"task_id":...})`` flows through ``route_entry``'s
        # report-back fork (group_graph.py ``_is_report_back``) → classify →
        # handle_reply_group, the live plan.
        #
        # 用 engine 启动时缓存的 coordinator_id（身份层·startup-baked，B11 时效契约
        # 见类 docstring）。不每任务查库：coordinator_id 是引擎身份非消息级配置，
        # 运行期不变；换群主须重建引擎（未实现，pending-restart）。
        if self.coordinator_id and self.coordinator_id != agent_id:
            msg = f"步骤完成：{task_content}\n\n结果：{snippet or '已完成'}"
            rt = registry.get_runtime(group_id)
            if rt is not None and rt._graph is not None:
                await rt.invoke_turn(
                    incoming_kind="agent_reply",
                    incoming_message=msg,
                    incoming_sender=agent_id,
                    incoming_data={"task_id": task_id, "success": success},
                )
            else:
                # dual-track fallback: no runtime (cold / compile-failed group /
                # pre-load race) → legacy notify to the resident coordinator
                # engine so its ``_handle_notify`` still drives the report-back.
                await push_notify(
                    group_id,
                    "agent_reply",
                    agent_id,
                    self.coordinator_id,
                    msg,
                    {"task_id": task_id, "success": success},
                )

    async def _on_task_cancelled(self, task: dict[str, Any]) -> None:
        """PL-11: cleanup when the current task is cancelled mid-execution.

        Marks the task ``failed`` (canceled) in the inbox queue, emits a
        ``task_failed`` bus event so the frontend WorkerTrace shows the stop,
        and posts a short agent_reply so the chat reflects the interruption.
        Mirrors the success-path side effects (complete_task + emit + reply)
        but with a canceled result — without this, the inbox task would stay
        ``claimed`` forever and the UI would show a phantom executing state.
        """
        task_id = task["id"]
        await complete_task(task_id, False, "任务已停止")
        await emit_task_completed(
            self.group_id, task_id, self.agent_id, False, "任务已停止", None
        )
        await self._publish_log(task_id, "⏹ 任务已被用户停止")
        # B22：透传 task_id（取消收尾的 announce 归属本任务，前端按 task_id 退场定稿气泡）。
        await self._reply("⏹ 任务已停止", task_id)

    # ── MT-17: worker-task timeout watchdog ─────────────────────────

    def _resolve_worker_timeout(self) -> float:
        """Resolve the per-task timeout (seconds) for *this* engine's group.

        Precedence: ``config.worker_timeout`` (a per-group override, read fresh
        via ``crud.get_group`` per task — the group settings Modal may change it
        without an engine restart) → module-level ``WORKER_TASK_TIMEOUT`` (the
        env default, 300s). A value <= 0 disables the timeout (hang-tolerant
        legacy behaviour). Synchronous (returns the resolved default; the
        per-group override is awaited lazily by the caller via
        ``_arm_timeout_watchdog``).
        """
        return WORKER_TASK_TIMEOUT

    async def _arm_timeout_watchdog(
        self, task: dict[str, Any], default_timeout: float
    ) -> asyncio.Task | None:
        """Arm an asyncio watchdog that cancels a hung worker task.

        A worker that produces no result within ``timeout`` seconds is treated
        as hung ("长时间无响应"). The watchdog sets ``_timeout_fired`` (so the
        ``CancelledError`` handler in ``_handle_task`` recognizes a
        timeout-driven cancel vs a PL-11 user stop) and cancels the child
        ``_worker_task``. The cancel propagates into the in-flight LLM call /
        tool execution (``astream_events`` / ``run_command``), the body
        unwinds, and ``_on_task_timed_out`` synthesizes a failure report-back
        so the coordinator's MT-15 recovery (retry/skip/reassign/keep_failed)
        wakes and the plan doesn't deadlock on a "dispatched" step.

        Returns the watchdog ``asyncio.Task`` (caller cancels it in ``finally``
        when the body settles normally). ``None`` when the timeout is disabled
        (<=0) — no watchdog is armed, preserving hang-tolerant behaviour.
        """
        # Per-group override takes precedence over the env default. Read fresh
        # per task (group settings are user-mutable) but tolerate read errors
        # by falling back to the default rather than blocking dispatch.
        timeout = default_timeout
        try:
            grp = await crud.get_group(self.group_id)
            if grp and grp.config and "worker_timeout" in (grp.config or {}):
                try:
                    timeout = float(grp.config.get("worker_timeout") or 0)
                except (TypeError, ValueError):
                    timeout = default_timeout
        except Exception:
            logger.warning(
                "[engine %s] worker_timeout config read failed, using default %ss",
                self.name, default_timeout,
            )
        if timeout <= 0:
            return None

        async def _watch() -> None:
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                return
            # fire only if the body is still running (a normal settle cancels
            # the watchdog before this line — defensive double-check).
            if self._worker_task is None or self._worker_task.done():
                return
            self._timeout_fired = True
            logger.warning(
                "[engine %s] worker task %s hung for %.0fs — degrading (cancel)",
                self.name, task["id"], timeout,
            )
            await self._publish_log(
                task["id"],
                f"⏱ 任务超时（{timeout:.0f}s 无响应），自动降级处理。",
            )
            self._worker_task.cancel()

        return asyncio.create_task(_watch())

    async def _on_task_timed_out(self, task: dict[str, Any]) -> None:
        """MT-17: cleanup when the worker task hangs past the timeout.

        Mirrors ``_on_task_cancelled`` but framed as a *timeout* (not a user
        stop): marks the task ``failed`` with a timeout result, emits
        ``task_failed`` so the frontend WorkerTrace shows the degradation, and
        posts a short agent_reply so the chat reflects it. Crucially, it also
        pushes the ``agent_reply`` report-back notify to the coordinator
        carrying ``{"task_id", "success": False}`` — same channel as
        ``_run_worker_task`` uses on a genuine failure — so the coordinator's
        ``node_handle_reply`` (MT-15) recovery decision (retry/skip/reassign/
        keep_failed) wakes and the plan doesn't deadlock on a "dispatched" step
        that will never complete.
        """
        task_id = task["id"]
        task_content = task.get("content") or ""
        timeout_result = f"任务超时（worker 长时间无响应）"
        await complete_task(task_id, False, timeout_result)
        await emit_task_completed(
            self.group_id, task_id, self.agent_id, False, timeout_result, None
        )
        await self._publish_log(task_id, "⏱ 任务已超时降级")
        # B22：透传 task_id（超时收尾的 announce 归属本任务，前端按 task_id 退场定稿气泡）。
        await self._reply(f"⏱ {timeout_result}", task_id)
        # report-back to coordinator via the per-group GroupRuntime (task-19④).
        # Same split-brain fix as ``_run_worker_task``: the timed-out step's
        # ``task_id`` is on ``rt._dispatch_plan`` (set by the Send fan-out), so
        # the recovery must reach the SAME runtime's ``handle_reply_group`` —
        # without this the step stays "dispatched" forever and the plan
        # deadlocks. ``invoke_turn`` flows through the report-back fork →
        # classify → handle_reply_group so MT-15 recovery (retry/skip/
        # reassign/keep_failed) wakes.
        if self.coordinator_id and self.coordinator_id != self.agent_id:
            msg = f"步骤完成：{task_content}\n\n结果：{timeout_result}"
            rt = registry.get_runtime(self.group_id)
            if rt is not None and rt._graph is not None:
                await rt.invoke_turn(
                    incoming_kind="agent_reply",
                    incoming_message=msg,
                    incoming_sender=self.agent_id,
                    incoming_data={"task_id": task_id, "success": False},
                )
            else:
                # dual-track fallback: no runtime → legacy notify to the
                # resident coordinator engine's _handle_notify.
                await push_notify(
                    self.group_id,
                    "agent_reply",
                    self.agent_id,
                    self.coordinator_id,
                    msg,
                    {"task_id": task_id, "success": False},
                )

    # ── notify handling ──────────────────────────────────────────────

    async def _handle_notify(self, notify: dict[str, Any]) -> None:
        """Process a notify item via the LangGraph graph.

        A notify whose ``type`` is ``plan_resume`` carries a resume payload
        (in ``data``) and is dispatched to the coordinator graph as
        ``Command(resume=<payload>)`` rather than a fresh-input dict. This is
        the plan-confirm entry point (PL-02/PL-03): ``node_dispatch`` paused
        the thread via ``interrupt()`` and a later ``/plan/confirm|direct|modify``
        API call pushes a ``plan_resume`` notify whose payload wakes the
        dispatch node to fan out. All other notify kinds go through as
        fresh-input dicts (the pre-existing path).
        """
        if notify.get("sender_id") == self.agent_id:
            return

        async def reply_cb(content: str) -> None:
            """Engine-side mention routing callback for graph nodes.

            防循环状态用群级共享 dict（``route_mentions`` 内部 None 时自动取
            ``_get_recent_routes(group_id)``）——原来传 self._recent_routes 是每个
            engine 一个空 dict，反向清键打不中对方 → 接龙 4 轮就断。群级共享后 A→B
            和 B→A 写进同一个 dict，反向清除才生效，持续交替不断链。
            """
            await route_mentions(
                self.group_id,
                self.agent_id,
                self.name,
                content,
                None,  # None → route_mentions 取群级共享 _get_recent_routes(group_id)
            )

        if self.graph_kind == "coordinator":
            coord_mod.set_reply_callback(reply_cb)
            try:
                # PL-02/PL-03 + MT-03: read the group's config flags per invoke
                # so the coordinator graph reflects the *current* group config
                # (GroupEntity.config). Read fresh each notify rather than caching
                # on the engine — group config is a user-mutable setting (the
                # plan-confirm/direct API may toggle auto_confirm; the group
                # settings Modal may update leader_strategy) and a stale cache
                # would freeze the wait/confirm vs 直接干 mode and a stale Leader
                # strategy until the engine restarts. Cost is one DB read per
                # coordinator notify, negligible vs the LLM call downstream.
                #
                # B11 时效口径契约·配置层（per-invoke 现读）：auto_confirm /
                # leader_strategy 是消息级行为旋钮，每 notify 现读（与身份层
                # coordinator_id 启动缓存对照——两类字段不同时效，有意分层非口径
                # 不一致：身份层决定编译哪张图不可运行期变；配置层只影响本轮图内
                # 决策节点可随时改，见类 docstring 时效口径契约）。
                auto_confirm = False
                leader_strategy = ""
                grp = await crud.get_group(self.group_id)
                if grp:
                    if grp.config:
                        auto_confirm = bool(grp.config.get("auto_confirm", False))
                    # MT-03: leader_strategy via the shared safe accessor so the
                    # read path has one source of truth for the default + key name.
                    leader_strategy = get_leader_strategy(grp.config)

                # PL-02 resume entry: a plan_resume notify carries the resume
                # payload (mode=confirm|direct|modify, optional amended_steps).
                # Dispatch it as Command(resume=...) so node_dispatch's
                # interrupt() returns the payload and the graph fans out — this
                # is the LangGraph-native resume path. Only meaningful on a
                # thread currently interrupted at the dispatch node; on an idle
                # thread Command(resume=) is a harmless no-op resume that runs
                # classify→llm with empty input (verified safe — see progress
                # notes for task 5). The plan_resume marker is set by the
                # plan-confirm API endpoints (api/plan.py /confirm | /direct |
                # /modify), the single inbound plan-confirm channel since task 11
                # retired the legacy ``plan_confirm`` fresh-input notify path.
                notify_type = notify.get("type") or ""
                if notify_type == "plan_resume":
                    resume_payload = notify.get("data") or {}
                    result = await self.graph.ainvoke(
                        Command(resume=resume_payload),
                        config={"configurable": {"thread_id": self.thread_id}},
                    )
                else:
                    result = await self.graph.ainvoke(
                        {
                            "group_id": self.group_id,
                            "agent_id": self.agent_id,
                            "agent_name": self.name,
                            # 群聊 Leader：system_prompt + COORDINATOR_SYSTEM 由 coordinator.py
                            # 三处 system 消息拼接（_leader_system）。
                            "system_prompt": self.system_prompt,
                            "incoming_message": notify.get("content") or "",
                            "incoming_sender": notify.get("sender_id") or "",
                            "incoming_kind": notify.get("type") or "",
                            "incoming_data": notify.get("data"),
                            "memory": self._memory,
                            "dispatch_plan": self._dispatch_plan,
                            "recent_routes": self._recent_routes,
                            "auto_confirm": auto_confirm,
                            "leader_strategy": leader_strategy,
                        },
                        config={"configurable": {"thread_id": self.thread_id}},
                    )
            finally:
                coord_mod.set_reply_callback(None)
            # sync dispatch_plan back from graph result (nodes mutate it).
            # On a resume (Command) turn the plan is sourced from the checkpointer
            # (node_dispatch already checkpointed it on the interrupt turn), so the
            # mirror here is synced back from the resume result to match the graph's
            # truth. The checkpointer is the source of truth; self._dispatch_plan is
            # a compatibility mirror retained for the /confirm | /direct | /modify
            # pending guards + the /modify patch source (see api/plan.py) and for
            # reset_session's belt-and-suspenders second wipe.
            if result and isinstance(result, dict):
                updated_plan = result.get("dispatch_plan")
                if updated_plan is not None:
                    self._dispatch_plan = list(updated_plan)
            # record memory (user side). A resume turn has no user message content
            # (the notify is a control signal), so skip memory append for plan_resume
            # — appending an empty "[user] " entry would pollute the coordinator's
            # conversation context with a noise turn.
            if notify_type != "plan_resume":
                self._memory.append(
                    {
                        "role": "user",
                        "content": f"[{notify.get('sender_id')}] {notify.get('content')}",
                    }
                )
        else:
            worker_mod.set_reply_callback(reply_cb)
            try:
                # 群聊普通成员的 system_prompt 追加「团队互动」语义：agent 自带的
                # system_prompt（如「你是后端工程师，负责 API 与数据层开发」）把人设
                # 锁死在本职工作，对成语接龙等非工作互动无参与意愿 → brain 倾向回避
                # （回「请前端先出第一个成语」搪塞）。这段直接加在 system 层（与人设同级
                # 权重），明确「本职外也正常参与群内互动」，压住纯工作人设的抗拒。单聊
                # agent 没有同事互动场景，不加（保持其原 persona）。
                #
                # B12：文案抽到 llm.prompts.TEAM_INTERACTION_SUFFIX 单一真源——与
                # build_brain_prompt 内嵌的决策级提醒同一段文字（system 层 persona 追加 +
                # 决策层 prompt 内嵌两层强化）。改文案只改常量一处，避免两处分叉。
                sys_for_invoke = self.system_prompt
                if not self.single_chat:
                    sys_for_invoke = (
                        (self.system_prompt or "") + "\n\n" + TEAM_INTERACTION_SUFFIX
                    )
                result = await self.graph.ainvoke(
                    {
                        "group_id": self.group_id,
                        "agent_id": self.agent_id,
                        "agent_name": self.name,
                        "agent_role": self.role,
                        # coordinator_id 用于 _build_context_from_db 把协调者消息标成
                        # 「协调者」而非裸 agent_id。
                        "coordinator_id": self.coordinator_id,
                        # 单聊/普通成员 worker brain：作为独立 system 消息注入，覆盖
                        # brain prompt 的兜底人设；空串时退化为兜底（与改前等价）。
                        "system_prompt": sys_for_invoke,
                        "incoming_message": notify.get("content") or "",
                        "incoming_sender": notify.get("sender_id") or "",
                        "memory": self._memory,
                    },
                    config={"configurable": {"thread_id": self.thread_id}},
                )
            finally:
                worker_mod.set_reply_callback(None)
            # worker 不再维护 self._memory（上下文改从 messages 表真源拉，见
            # worker._build_context_from_db）。原 append 有两个 bug：(1) 时序——append
            # 在 ainvoke 之后，当前 incoming 没进决策时 context；(2) 身份——peer 消息
            # 存成 role=user 被 _build_context 渲染成「用户:」。改走 DB 真源后这两个 bug
            # 一起消失，且换话题不污染（DB 即真源，无需 reset 清内存）。coordinator 分支
            # 仍用 self._memory（其上下文耦合 dispatch_plan 等状态，暂不动）。

    # ── unified reply / publish (Rust engine.reply / publish_log) ────

    async def _reply(self, content: str, task_id: str | None = None) -> None:
        """Persist an agent_reply message + emit + mention route (Rust engine.reply).

        Persistence + emit delegated to ``persist_agent_reply`` (engine.reply, B10)
        so the agent_reply shape is a single source shared with the coordinator /
        worker graphs' reply paths. Mention routing stays here — the engine owns
        its context and calls ``route_mentions`` directly (not via a callback,
        which is the graph-node mechanism).

        ``data`` 恒为 None：execute 路径的收尾 announce（``任务完成 🎉`` /
        ``执行出错了`` / ``⏹ 任务已停止`` / ``⏱ 超时``）是模板化文本，非 brain
        流式 LLM 输出，不携带 model/elapsed_ms/tokens 等 stats —— 前端
        extractCoordStats 在 data.elapsed_ms 缺失时返 null 不渲染状态行（与协调者
        dispatch announce 同理：announce 与流式决策文本不同源，stats 不匹配 content）。

        ``task_id`` (B22)：本 announce 收尾的任务 id。透传到 persist_agent_reply
        落盘到 message.task_id + 透传到 message_added WS 事件，让前端
        ``finalizedBubbles`` 退场判定能按 task_id 精确匹配「收尾事件 ↔ 持久化回复」
        （主路径），不再只靠 sender+时间戳（原 fragile：logs 追加路径 coerce WS 消息
        时 task_id 可能丢失，时间戳比较依赖前后端时钟同步）。3 个调用方
        （_run_worker_task 成功 / _on_task_cancelled 取消 / _on_task_timed_out 超时）
        都传 ``task["id"]``。chat 路径（coordinator/worker node_chat）不经 _reply，
        其 agent_reply 仍 task_id=None（graph 走 _unified_reply 不传 task_id）。

        已知限制（A6 评估·保守不改）：execute→task→「任务完成🎉」路径的 announce
        不带 stats。理论上可透传 ``run_agent_loop`` 累加的 create_react_agent usage
        （LangChain AIMessage.usage_metadata 含 input/output/total_tokens），但：
          (1) ``run_agent_loop`` 当前不捕获 usage（astream_events 只取 content/tool），
              需在 on_chat_model_end 读 msg.usage_metadata 累加 + return 加 usage 字段；
          (2) ``_reply`` 需加 data 参数 + 3 处调用方（成功/取消/超时）分别处理，
              后两处无 usage 需 data=None 兜底；
          (3) 改造跨 agent_loop.py + agent_executor.py + registry.py 三文件，触碰
              LangGraph 框架事件层（usage_metadata 字段稳定性跨 provider 需验证）。
        工程任务的「模型消耗」已由 task_think/task_token/task_tool 过程气泡（PL-08
        流式 + task_think 折叠块）体现，定稿 announce 带不带 token 数对用户价值有限。
        优先靠 A4/A5 把创作类请求锚定到 chat 路径（chat 路径 stats 已透传，见 A2/A3），
        execute 路径只服务真工程任务（写代码/改配置/运行命令/调工具），其 announce
        不带 stats 是设计取舍而非 bug。若未来要修，见上述三步改造路径。
        """
        await persist_agent_reply(self.group_id, self.agent_id, content, None, task_id)
        # 防循环用群级共享 dict（见 mention._get_recent_routes）——原 self._recent_routes
        # 是 per-engine，反向清键打不中对方 dict 导致接龙 4 轮断。传 None 让 route_mentions
        # 自动取群级共享视图。
        await route_mentions(
            self.group_id,
            self.agent_id,
            self.name,
            content,
            None,
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

    async def _reset_idle(self, task_id: str) -> None:
        self.status = "idle"
        self.current_task_id = None
        await emit_agent_status(
            self.group_id, self.agent_id, self.name, "idle", None
        )

    async def _drain_pending(self) -> None:
        """After finishing a task, process the backlog (Rust drain_pending)."""
        if self.status != "idle" or not self._pending_tasks:
            return
        next_task = self._pending_tasks.pop(0)
        await self._handle_task(next_task)

    async def reset_session(self) -> None:
        """BE-02: clear cross-invoke engine instance state + LangGraph interrupt.

        Wipes the coordinator/worker conversation memory (``_memory``), the
        resident dispatch plan (``_dispatch_plan``), the mention route-rate gate
        (``_recent_routes``), and the pending-task backlog (``_pending_tasks``) —
        the per-engine instance state accumulated across invocations. This is the
        in-process analogue of ``/new``: a fresh conversation without tearing
        down and rebuilding the engine (the compiled LangGraph graph, the inbox
        channel, and the run loop all stay live — only the cross-invoke state is
        reset, so the next ``ainvoke`` starts from an empty slate).

        PL-02 interrupt migration (this task): on a coordinator graph the old
        ``_dispatch_plan.clear()`` only wiped the engine's *compatibility mirror*
        — the thread could still be paused mid-``node_dispatch`` via
        ``interrupt()``, with the resident plan held in the MemorySaver
        checkpointer (the source of truth). A bare mirror clear left a dangling
        interrupt: the next fresh-input demand auto-resumed it, and a subsequent
        plan-confirm resume would re-fire the stale plan (409-style hazard,
        [[engine-audit-interrupt-replacement]]). So the coordinator reset now
        resolves the interrupt via the graph's checkpointer API —
        ``aupdate_state(values=None, as_node=END)`` writes a terminal checkpoint
        (``next == ()``, pending interrupt tasks cleared), matching the
        LangGraph idiom for "act as if the thread finished." A coordinator graph
        that never reached ``node_dispatch`` (idle / cold / auto_confirm-only)
        has no interrupt to resolve; the END write is a harmless no-op on a
        terminal or empty thread (verified across cold + interrupted +
        post-clear re-invoke cases). The mirror clear is kept as a
        belt-and-suspenders second wipe so readers that still touch
        ``self._dispatch_plan`` (the /confirm | /direct | /modify pending guards
        + the /modify patch source in api/plan.py) see ``[]``.

        Does NOT touch the LangGraph graph object, the MemorySaver checkpointer
        thread_id, or the inbox queue wiring — engine identity and graph topology
        are preserved (方案 B 引擎内存态清理，不改 LangGraph 图). If the engine
        is mid-execution (``status == "executing"``), the running worker task is
        cancelled first via ``request_cancel`` so the reset cannot race an
        in-flight ``_run_worker_task`` that would otherwise re-populate
        ``_memory`` / ``_dispatch_plan`` as it unwinds — the caller is expected to
        poll status back to ``idle`` (mirrors PL-11 stop semantics).

        No raise: safe on a not-yet-started or already-offline engine (the
        ``executing`` cancel is best-effort; an offline engine simply has nothing
        to cancel and clears the (already empty) state lists). The checkpointer
        write is best-effort too — any failure there degrades to the legacy
        mirror-only clear (logged) rather than aborting the reset.
        """
        if self.status == "executing":
            # cancel the in-flight worker task so it can't repopulate state as
            # it unwinds; reset takes effect once the engine returns to idle.
            self.request_cancel()
        # Resolve a dangling interrupt on the coordinator thread so the next
        # demand isn't auto-resumed into the stale plan. ``aupdate_state(...,
        # as_node=END)`` is the LangGraph idiom for "act as if the thread
        # finished": it clears the pending interrupt task and writes a terminal
        # checkpoint (next == ()). No-op on a thread with no checkpoint (cold
        # coordinator) or already-terminal (idle / auto_confirm-only) — verified
        # across cold + interrupted + post-clear re-invoke cases. Worker graphs
        # have no ``interrupt()`` site so they never pause; the END write is a
        # uniform no-op there too. Best-effort: a checkpointer failure degrades
        # to the legacy mirror-only clear (logged) rather than aborting reset.
        if self.graph_kind == "coordinator":
            try:
                await self.graph.aupdate_state(
                    config={"configurable": {"thread_id": self.thread_id}},
                    values=None,
                    as_node=END,
                )
            except Exception:
                logger.exception(
                    "[engine] %s reset_session: aupdate_state(END) failed, "
                    "falling back to mirror-only plan clear (interrupt may "
                    "linger on thread %s)",
                    self.name, self.thread_id,
                )
        self._memory.clear()
        self._dispatch_plan.clear()
        self._recent_routes.clear()
        self._pending_tasks.clear()
        # 群级共享防循环状态也清掉（A2A 轮次计数 + recent_routes）——reset_session
        # 是「换话题重来」，旧的来回传递记录不该留着卡新一轮。clear_group_routes
        # 清的是 mention 模块级 _group_recent_routes[group_id] + _a2a_turns[group_id]。
        clear_group_routes(self.group_id)
        logger.info(
            "[engine] %s session reset (interrupt resolved + memory + dispatch_plan cleared)",
            self.name,
        )


class AgentRegistry:
    """group_id -> agent_id -> AgentEngine  +  group_id -> GroupRuntime.

    Dual-track (task-18 通电点):
      · ``_engines`` (group_id -> agent_id -> ``AgentEngine``) — the *resident
        per-agent* engines that own the task inbox + the single-agent LangGraph
        graphs. The **execute path** stays here: ``_run_worker_task`` runs the
        agentic loop (``execute_agent_task`` / ``create_react_agent`` + bind_tools)
        with a cancellable ``_worker_task`` + the MT-17 timeout watchdog — mature
        code that is not worth re-platforming onto the group graph. Retiring
        ``AgentEngine`` would break execution's home + the task report-back
        closure (the coordinator's ``node_handle_reply_group`` waits for an
        ``agent_reply`` turn driven by the executor's completion), so the
        residual engine keeps that role.
      · ``_runtimes`` (group_id -> ``GroupRuntime``) — the *per-group turn
        controller* for the decentralized swarm graph. Owns the compiled
        ``build_group_graph`` + the cancellable turn ``asyncio.Task`` + the
        cooperative stop signal. The **orchestration path** (chat / @mention /
        plan-confirm / stop) moves here: ``GroupRuntime.invoke_turn`` /
        ``resume_plan`` replace the resident ``_handle_notify`` three-ainvoke
        site (task-19/20/22), and ``request_stop`` / ``cancel_turn`` replace
        ``AgentEngine.request_cancel`` for the「停不下来」defect (task-23/24).

    The two tracks are additive, not a flag: ``load_from_store`` builds BOTH a
    ``GroupRuntime`` (compile_graph) per group AND the per-agent ``AgentEngine``
    set, so execute (engine) + orchestration (runtime) are both live. The
    resident engines are kept idle w.r.t. their inbox-driven notify loop for the
    orchestration kinds that moved to the runtime (the runtime's
    ``invoke_turn`` is the single inbound entry for those); they stay active for
    ``task`` items (the execute path) + the resident notify kinds the runtime
    does not yet own. This keeps every existing test (which drives the resident
    ``AgentEngine._handle_notify`` / ``_run_worker_task`` directly) green while
    wiring the runtime as the production inbound path — no test changes needed.
    """

    def __init__(self) -> None:
        self._engines: dict[str, dict[str, AgentEngine]] = {}
        self._runtimes: dict[str, GroupRuntime] = {}

    async def ensure_runtime(self, group_id: str) -> GroupRuntime | None:
        """Resolve (or lazily build) the per-group ``GroupRuntime``.

        The runtime owns the compiled group graph + the turn task handle + the
        stop signal. ``load_from_store`` pre-builds one per group at startup;
        this is the lazy on-demand path for a group whose runtime is missing
        (e.g. a race: a group created after startup, before the next reload).
        Reads the group row to resolve ``coordinator_id`` + compiles the graph.
        Returns ``None`` when the group row is gone (caller degrades to the
        resident engine path). Idempotent: a second call returns the cached
        runtime without recompiling.
        """
        rt = self._runtimes.get(group_id)
        if rt is not None:
            return rt
        group = await crud.get_group(group_id)
        if not group:
            return None
        rt = GroupRuntime(group)
        await rt.compile_graph()
        self._runtimes[group_id] = rt
        logger.info(
            "[registry] lazily built GroupRuntime for group=%s (coordinator=%s)",
            group_id, rt.coordinator_id or "(none)",
        )
        return rt

    def get_runtime(self, group_id: str) -> GroupRuntime | None:
        """Return the group's ``GroupRuntime`` if built, else ``None``.

        The synchronous accessor for the inbound-path wiring (route_user_message
        etc.). Does NOT lazily build (that is async, ``ensure_runtime``); a
        ``None`` return means ``load_from_store`` has not run for this group yet
        (cold / new group) — callers fall back to the resident engine path.
        """
        return self._runtimes.get(group_id)

    async def add_engine(
        self,
        group_id: str,
        agent_def: dict[str, Any],
        coordinator_id: str = "",
        single_chat: bool = False,
    ) -> AgentEngine:
        if group_id not in self._engines:
            self._engines[group_id] = {}
        if agent_def["id"] in self._engines[group_id]:
            return self._engines[group_id][agent_def["id"]]
        engine = AgentEngine(agent_def, group_id, coordinator_id, single_chat)
        await engine.start()
        self._engines[group_id][agent_def["id"]] = engine
        return engine

    async def stop_group(self, group_id: str) -> int:
        """MT-07: stop every engine in a group (解散团队时停止引擎).

        Iterates a snapshot of the group's engines, stops each (cancels the run
        loop, unregisters the inbox, emits an ``offline`` status), then drops the
        now-empty group key from ``_engines``. Returns the number of engines
        stopped (0 if the group had no live engines — e.g. never started, or
        already torn down). Safe to call on an unknown / already-stopped group
        (no-op). Used by ``DELETE /api/groups/{id}`` so deleting a team reclaims
        its resident engines rather than leaking them until process shutdown.
        """
        group = self._engines.get(group_id)
        if not group:
            return 0
        stopped = 0
        for aid in list(group.keys()):
            await group[aid].stop()
            stopped += 1
        # stop() flips status to offline + unregisters inbox but leaves the entry;
        # drop the whole group now that every engine is offline.
        self._engines.pop(group_id, None)
        # dual-track (task-18): also tear down the per-group GroupRuntime so a
        # disband reclaims the compiled graph + any in-flight turn task. The
        # runtime has no inbox loop to cancel (a turn is one ``graph.ainvoke``),
        # so ``cancel_turn`` drains any active turn; the compiled graph is
        # dropped by the pop. Safe on a group with no runtime (no-op pop).
        rt = self._runtimes.pop(group_id, None)
        if rt is not None:
            rt.cancel_turn()
        logger.info(
            "[registry] stopped %d engine(s) for group %s (disband) runtime=%s",
            stopped, group_id, "dropped" if rt else "absent",
        )
        return stopped

    def get_engine(self, group_id: str, agent_id: str) -> AgentEngine | None:
        return self._engines.get(group_id, {}).get(agent_id)

    async def stop_task(
        self, group_id: str, agent_id: str, task_id: str | None = None
    ) -> bool:
        """PL-11: cancel the current executing task on an agent's engine.

        Delegates to ``AgentEngine.request_cancel`` which cancels the child
        ``_worker_task``; the next ``await`` raises ``CancelledError``, the
        body unwinds, and ``_handle_task`` runs ``_reset_idle`` to bring the
        engine back to idle. Returns whether a cancel was actually issued
        (``False`` if the agent isn't executing or task_id mismatches).

        This method returns *immediately* after issuing the cancel — it does
        not block on the task to finish unwinding. Callers that need the
        settled state should poll ``list_group_status`` (the PL-11 self-test
        does exactly this).
        """
        engine = self.get_engine(group_id, agent_id)
        if engine is None:
            return False
        return engine.request_cancel(task_id)

    async def stop_task_by_id(
        self, task_id: str, group_id: str | None = None
    ) -> dict[str, Any]:
        """PL-11: stop the engine currently executing ``task_id`` (if any).

        Scans engines — optionally narrowed to ``group_id`` — for the one whose
        ``current_task_id == task_id`` and ``status == "executing"``, then cancels
        it via :meth:`stop_task` (which cancels the child ``_worker_task`` so the
        next ``await`` raises ``CancelledError``; ``_handle_task`` absorbs it,
        runs ``_on_task_cancelled``, and the engine returns to idle).

        The ``tq_`` runtime ids are globally unique (uuid4), so at most one engine
        matches. Returns ``{"cancelled": bool, "group_id": str|None,
        "agent_id": str|None}`` — ``cancelled`` is False when no engine is
        executing that task (already finished, or only queued — the queued case is
        handled by ``inbox.cancel_task`` at the API layer, which complements this).

        ``stop_task`` → ``request_cancel`` is synchronous (no real await), so the
        dict iteration here never suspends mid-scan; the ``list()`` snapshots are
        defensive only.
        """
        groups: dict[str, dict[str, AgentEngine]] = (
            {group_id: self._engines.get(group_id, {})}
            if group_id is not None
            else self._engines
        )
        for gid, group in list(groups.items()):
            for aid, eng in list(group.items()):
                if eng.status == "executing" and eng.current_task_id == task_id:
                    cancelled = await self.stop_task(gid, aid, task_id)
                    return {
                        "cancelled": cancelled,
                        "group_id": gid,
                        "agent_id": aid,
                    }
        return {"cancelled": False, "group_id": None, "agent_id": None}

    async def load_from_store(self) -> None:
        """Spin up an engine for every coordinator + member across all groups.

        同一群组内所有引擎共享同一个 coordinator_id，引擎据此判定谁是群主
        （谁 == coordinator_id 谁就是协调者），不依赖 role 字符串。

        Dual-track (task-18): builds BOTH the per-agent ``AgentEngine`` set
        (the execute path's home — ``_run_worker_task`` runs the agentic loop)
        AND a per-group ``GroupRuntime`` (the orchestration path — the compiled
        group graph + turn task handle + stop signal). The two tracks are
        additive: keeping the engines intact means every existing execute-path
        test stays green, while wiring the runtime as the production inbound
        path for chat/@mention/plan-confirm/stop (the inbound entries swap to
        ``GroupRuntime.invoke_turn`` / ``resume_plan`` in task-19/20/22). A
        runtime whose group has no coordinator (single_chat / cold) still
        compiles — the decentralized path (agent nodes) runs without a
        coordinator subgraph branch, mirroring the resident engine's
        single-chat = worker-graph degradation.
        """
        groups = await crud.list_groups()
        for g in groups:
            coord_id = g.coordinator_id or ""
            # single_chat 群（唯一 agent 当 coordinator_id 但行为是个体）→ 选图传 True；
            # 群聊群 → False（Leader 走 coordinator 图）。统一传参保持签名一致。
            single = bool((g.config or {}).get("single_chat"))
            if coord_id:
                coord = await crud.get_agent(coord_id)
                if coord:
                    await self.add_engine(g.id, coord.model_dump(), coord_id, single)
            members = await crud.list_group_members_with_agent(g.id)
            for m in members:
                agent = await crud.get_agent(m.agent_id)
                if agent:
                    await self.add_engine(g.id, agent.model_dump(), coord_id, single)
            # Build the per-group GroupRuntime (orchestration track) + compile
            # its group graph. Lazy-member resolution happens inside
            # compile_graph (reads the roster + system_prompts from the DB) so
            # a member added after this loop is picked up on the next reload,
            # same staleness window as the resident engines. A failure to
            # compile degrades to the resident engine path (logged) rather than
            # aborting the whole load — the engines for that group are still
            # live for execute.
            try:
                rt = GroupRuntime(g)
                await rt.compile_graph()
                self._runtimes[g.id] = rt
            except Exception:
                logger.exception(
                    "[registry] GroupRuntime compile failed for group=%s "
                    "(degrading to resident-engine-only path)",
                    g.id,
                )
        logger.info(
            "[registry] loaded %d group(s) with engines, %d with group runtime",
            len(self._engines), len(self._runtimes),
        )

    async def shutdown_all(self) -> None:
        # dual-track (task-18): drain any in-flight group-graph turn first
        # (cancel_turn is a no-op when no turn is active) so the compiled
        # graphs are not left with a dangling task, then stop the resident
        # engines + clear both maps.
        for rt in list(self._runtimes.values()):
            rt.cancel_turn()
        for group in list(self._engines.values()):
            for engine in list(group.values()):
                await engine.stop()
        self._engines.clear()
        self._runtimes.clear()

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

    def list_all_status(self) -> dict[str, list[dict[str, Any]]]:
        """Return every group's agent statuses in one call (SA-01).

        Maps ``group_id -> [agent status, ...]`` — the same per-agent dict shape
        :meth:`list_group_status` produces (id / name / role / status /
        current_task_id). Delegates to :meth:`list_group_status` per group so the
        status row has a single construction site (no duplicate field list to
        drift).

        This is the backend foundation for eliminating frontend N+1 status
        polling: instead of one ``GET /api/status/{groupId}`` per group on every
        tick (SA-04 AgentPage, the /status slash command), the UI can issue a
        single ``GET /api/status`` (SA-02) and get all groups at once. Groups
        with no live engines are simply absent from the dict (the frontend treats
        a missing key as "no agents / all offline").
        """
        return {
            gid: self.list_group_status(gid)
            for gid in self._engines
        }

    async def reset_group_session(self, group_id: str) -> dict[str, int]:
        """BE-02: clear cross-invoke state on every engine + the group runtime.

        Iterates every resident engine (coordinator + workers) in ``group_id``
        and calls :meth:`AgentEngine.reset_session` on each — wiping
        ``_memory`` / ``_dispatch_plan`` / ``_recent_routes`` / ``_pending_tasks``
        without stopping the engines or touching the LangGraph graphs. Also
        resets the per-group ``GroupRuntime`` (task-18): its ``reset_session``
        resolves a dangling interrupt on the group graph's last turn thread +
        wipes ``_memory`` / ``_dispatch_plan`` (the runtime's resident
        cross-turn mirrors). Returns ``{"reset": <engine count>}`` so the API
        layer can confirm how many engines were affected (0 = group has no live
        engines, e.g. never started — the route still clears persisted messages,
        so a reset on a cold group is a no-op on the engine side but not an
        error).

        This is the registry-side counterpart of ``POST
        /api/groups/{id}/reset-session``: the route clears persisted messages
        (``crud.clear_messages_by_group``) + emits a cleared-plan bus event, then
        calls this to clear the in-memory engine + runtime state. Safe on an
        unknown / stopped group (returns ``{"reset": 0}``).
        """
        group = self._engines.get(group_id)
        if not group:
            # Still reset the runtime if one exists (a group with no engines but
            # a compiled runtime — unusual but defensive): the runtime owns the
            # group graph's cross-turn state + any dangling interrupt.
            rt = self._runtimes.get(group_id)
            if rt is not None:
                await rt.reset_session()
            return {"reset": 0}
        count = 0
        for eng in list(group.values()):
            await eng.reset_session()
            count += 1
        # dual-track (task-18): reset the group runtime's cross-turn mirrors +
        # resolve its dangling interrupt alongside the engines. ``reset_session``
        # cancels an in-flight turn first so it can't repopulate state as it
        # unwinds (mirrors the engine-side cancel-then-clear).
        rt = self._runtimes.get(group_id)
        if rt is not None:
            await rt.reset_session()
        logger.info(
            "[registry] reset session on %d engine(s) + runtime in group %s",
            count, group_id,
        )
        return {"reset": count}


registry = AgentRegistry()
