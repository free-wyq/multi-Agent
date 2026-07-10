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
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("multi-agent.scheduler")

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
    """Build an APScheduler trigger from a ScheduledTask dict."""
    stype = task.get("schedule_type", "interval")
    if stype == "cron":
        return CronTrigger.from_crontab(task.get("cron", "* * * * *"))
    if stype == "once":
        return DateTrigger(run_date=task.get("run_at", ""))
    # interval (default)
    secs = int(task.get("interval_seconds", 0) or 0)
    if secs <= 0:
        secs = 3600  # safe fallback: hourly
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
    """Register (or replace) an APScheduler job for a scheduled task."""
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
        # job not present (e.g. disabled at creation) — ignore
        pass


async def load_from_store() -> None:
    """Rebuild all enabled jobs from the store (startup)."""
    from store import crud

    tasks = await crud.list_scheduled_tasks()
    count = 0
    for t in tasks:
        add_job(t.model_dump())
        count += 1
    logger.info("[scheduler] loaded %d scheduled task(s)", count)
