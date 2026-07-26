"""Scheduled task scheduler — APScheduler fires prompts at agents on schedule.

PRD 3.5 (TM-01~07). Each enabled ``ScheduledTask`` is registered as an
APScheduler job. At fire time the job pushes the task's ``content`` prompt onto
the target agent's inbox via ``push_task`` (reusing the resident engine), so
scheduled execution goes through the **same agentic loop** as interactive
dispatch — no separate execution path. Each fire records a ``ScheduledTaskRun``
(running → success/failed) for the history view (TM-07).

Three schedule types (TM-03):
- ``cron``: APScheduler ``CronTrigger`` from the cron expression
- ``interval``: ``IntervalTrigger(seconds=...)``
- ``once``: ``DateTrigger(run_date=...)`` (one-shot, TM-03 一次性定时)

The scheduler is process-local (AsyncIOScheduler); jobs are rebuilt from the
store on ``load_from_store`` (startup) and on create/update/delete/toggle.

任务18c — ``_build_trigger`` 现在是「单源校验 + 构建」：在落库后 add_job 前先
``validate_schedule``，对「cron 空 / once run_at 空 / once run_at 已过 / interval
≤0」抛 ``ScheduleConfigError``（HTTP 400），由 create/update 路由捕获转 400 返回
前端——避免 e2e 暴露的「行已落库 → add_job raise ValueError → 500 + 孤儿行」。
``validate_schedule`` 在 *persist 前* 调用（路由层），故配置错的 task 根本不落库。
``_build_trigger`` 仍保留兜底防御（load_from_store 启动期直接读老库，行可能由旧版
本写入未过校验——此时不再 raise 崩 lifespan，而是跳过该 job + warn）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dateutil import parser as _dt_parser

logger = logging.getLogger("multi-agent.scheduler")


class ScheduleConfigError(ValueError):
    """Raised when a scheduled task's schedule fields are invalid (任务18c).

    Surfaced to the API as HTTP 400 by create/update route handlers (caught at
    the route boundary so the bad task never reaches ``crud.create_*`` — no
    orphan row + no 500). Carries a user-facing ``detail`` message in
    ``args[0]`` so the frontend can show exactly which field is wrong.
    """


def _parse_run_at(run_at: str) -> datetime:
    """Parse ``run_at`` (ISO8601) to an **aware** datetime (任务18c 时区修复).

    Frontend (``dayjs.toISOString()``) always sends UTC with a trailing ``Z``
    (e.g. ``2026-07-27T02:00:00.000Z``); direct API callers may send a naive
    local-time string (e.g. ``2026-07-27T10:00:00``). APScheduler's
    ``DateTrigger`` interprets a naive datetime in the *scheduler's* timezone
    (``AsyncIOScheduler`` defaults to local), and an aware one as-is — so the
    two forms previously fired at different wall-clock moments depending on
    who called. Normalize both to **aware UTC** here: ``Z`` / offset suffix →
    keep as parsed; naive → assume local tz, convert to UTC. The stored
    ``run_at`` string is unchanged (we only normalize at trigger-build time,
    not on persist), so the column stays the user's input verbatim and this
    function is the single place the once-task fire moment is resolved.
    """
    dt = _dt_parser.isoparse(run_at)
    if dt.tzinfo is None:
        # naive → assume local wall-clock time, convert to UTC for DateTrigger
        # (avoid the pytz/zoneinfo dependency dance; datetime.astimezone with no
        # arg uses the system local zone, which is what a naive local caller
        # meant).
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def validate_schedule(task: dict[str, Any]) -> None:
    """Validate a task's schedule fields before persist/register (任务18c).

    Raises ``ScheduleConfigError`` (→ HTTP 400) for:
      - ``cron`` type with empty/invalid cron expression
      - ``once`` type with empty ``run_at``
      - ``once`` type with a ``run_at`` already in the past (would silently
        never fire — APScheduler logs "was missed" and the job idles forever)
      - ``interval`` type with ``interval_seconds <= 0`` (previously fell back
        to a silent 3600s hourly — wrong schedule, no error to the user)

    Called by create/update route handlers *before* ``crud.create/update`` so a
    bad task never lands in the DB. ``_build_trigger`` calls this too as a
    defensive guard (load_from_store reads pre-existing rows that may predate
    this validation).
    """
    stype = task.get("schedule_type", "interval")
    if stype == "cron":
        cron = (task.get("cron") or "").strip()
        if not cron:
            raise ScheduleConfigError("cron 类型的定时任务必须提供 cron 表达式")
        try:
            CronTrigger.from_crontab(cron)
        except (ValueError, TypeError) as exc:
            raise ScheduleConfigError(f"cron 表达式无效: {exc}") from exc
    elif stype == "once":
        run_at = (task.get("run_at") or "").strip()
        if not run_at:
            raise ScheduleConfigError("once 类型的定时任务必须提供 run_at 时刻")
        try:
            fire_dt = _parse_run_at(run_at)
        except (ValueError, TypeError) as exc:
            raise ScheduleConfigError(f"run_at 时刻无效: {exc}") from exc
        if fire_dt <= datetime.now(timezone.utc):
            raise ScheduleConfigError(
                "once 类型的 run_at 时刻已过去，不会触发——请选择一个未来时刻"
            )
    else:  # interval (default)
        secs = int(task.get("interval_seconds", 0) or 0)
        if secs <= 0:
            raise ScheduleConfigError(
                "interval 类型的定时任务 interval_seconds 必须大于 0"
            )


# one process-local scheduler; started in main lifespan
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the process singleton, starting it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
        _scheduler.start()
        logger.info("[scheduler] AsyncIOScheduler started")
    return _scheduler


async def shutdown_scheduler() -> None:
    """Stop the scheduler on app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[scheduler] shut down")


def _build_trigger(task: dict[str, Any]):
    """Build an APScheduler trigger from a ScheduledTask dict.

    Assumes ``validate_schedule`` has already accepted the fields (create/
    update routes call it before persist). For the startup ``load_from_store``
    path — which reads rows that may predate validation — this guards again
    with a try/except so one bad legacy row doesn't crash the whole lifespan:
    the bad job is skipped (logged at warning), the rest still load.
    """
    stype = task.get("schedule_type", "interval")
    if stype == "cron":
        return CronTrigger.from_crontab(task.get("cron", "* * * * *"))
    if stype == "once":
        # normalize to aware UTC so naive-local and Z-suffixed forms fire at the
        # same wall-clock moment (任务18c 时区修复; see _parse_run_at).
        return DateTrigger(run_date=_parse_run_at(task.get("run_at", "")))
    # interval (default)
    secs = int(task.get("interval_seconds", 0) or 0)
    if secs <= 0:
        secs = 3600  # safe fallback: hourly (load_from_store legacy-row guard)
    return IntervalTrigger(seconds=secs)


def _job_id(task_id: str) -> str:
    return f"sched_{task_id}"


async def _fire(task_id: str, force: bool = False) -> None:
    """Job callback: push the task's content to the agent + record a run.

    Runs in the scheduler's asyncio loop. Reuses ``push_task`` so the resident
    AgentEngine picks it up exactly like an interactive dispatch. We do not
    block on the agent finishing — the run is marked 'success' once the task is
    queued (the agent's own task_log streams the actual work over WS).

    ``force=True`` skips the enabled check so TM-04 立即执行 fires even a
    paused task (explicit manual override); scheduled fires pass ``force=False``
    and bail out if the task has been disabled since the job was registered.
    """
    from store import crud
    from engine.inbox import push_task

    task = await crud.get_scheduled_task(task_id)
    if not task:
        return
    if not task.enabled and not force:
        # disabled since the job was scheduled — remove it
        remove_job(task_id)
        return

    run = await crud.create_scheduled_task_run(task_id)
    try:
        await push_task(
            task.group_id,
            "scheduler",
            task.agent_id,
            f"[定时任务:{task.name}] {task.content}",
            {"scheduled_task_id": task_id, "run_id": run.id},
        )
        await crud.finish_scheduled_task_run(
            run.id, True, f"已派发给智能体 {task.agent_id}"
        )
        logger.info("[scheduler] fired task '%s' -> agent %s", task.name, task.agent_id)
    except Exception as exc:
        logger.exception("[scheduler] fire failed for task %s", task_id)
        await crud.finish_scheduled_task_run(run.id, False, str(exc))


def add_job(task: dict[str, Any]) -> None:
    """Register (or replace) an APScheduler job for a scheduled task.

    Assumes the caller (route handler or ``load_from_store``) has already run
    ``validate_schedule``; this is the *register* step, not the *validate* step.
    Disabled tasks early-return (no job registered) — the create path only
    calls this when ``task.enabled`` is true, and ``resume`` calls it after
    flipping enabled to true.
    """
    if not task.get("enabled", True):
        return
    sched = get_scheduler()
    trigger = _build_trigger(task)
    sched.add_job(
        _fire,
        trigger=trigger,
        args=[task["id"]],
        id=_job_id(task["id"]),
        replace_existing=True,
    )
    logger.info("[scheduler] registered job %s (%s)", task["name"], task["schedule_type"])


def remove_job(task_id: str) -> None:
    """Remove a scheduled task's job (on delete / disable)."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(task_id))
    except Exception:
        # APScheduler raises JobLookupError when the job was never added
        # (e.g. task was disabled at creation, so add_job early-returned). This
        # is the expected benign outcome of remove-on-never-scheduled, so `pass`
        # is correct — but log at debug so a *real* scheduler failure isn't
        # silently hidden behind the benign path (B31 错误处理重巡航——原裸
        # `pass` 不分良性 JobLookupError 与真 scheduler 错误，全静默).
        logger.debug(
            "[scheduler] remove_job skipped (job %s not present)", _job_id(task_id),
            exc_info=True,
        )


async def load_from_store() -> None:
    """Rebuild all enabled jobs from the store (startup).

    Each enabled task's schedule is validated (``validate_schedule``) before
    the job is registered. A row whose schedule fields are invalid (e.g. a
    ``cron`` task with an empty cron string written by an older build that
    predates create-time validation) is skipped with a warning rather than
    crashing the whole lifespan — one bad row shouldn't block the rest from
    loading. The row stays in the DB (visible + editable in the UI) so the
    user can fix it; it just won't fire until corrected.
    """
    from store import crud

    tasks = await crud.list_scheduled_tasks()
    count = 0
    skipped = 0
    for t in tasks:
        td = t.model_dump()
        try:
            validate_schedule(td)
        except ScheduleConfigError as exc:
            logger.warning(
                "[scheduler] skipping task %s (%s) on load: %s",
                t.id, t.name, exc,
            )
            skipped += 1
            continue
        add_job(td)
        count += 1
    logger.info(
        "[scheduler] loaded %d scheduled task(s) (skipped %d invalid)",
        count, skipped,
    )
