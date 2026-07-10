"""ScheduledTask + ScheduledTaskRun + payload Pydantic models (PRD 3.5).

A scheduled task fires a prompt at a target agent on a schedule (cron / interval
/ one-shot). At fire time the scheduler pushes a task onto the agent's inbox
(reusing the resident engine), so scheduled execution goes through the same
agentic loop as interactive dispatch. ``ScheduledTaskRun`` records each
execution (status / produced output / timestamps) for the history view (TM-07).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ScheduledTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    # the prompt fired at the agent at each scheduled run
    content: str = ""
    # target agent id
    agent_id: str = ""
    # schedule type: cron | interval | once
    schedule_type: str = "interval"
    # cron: a cron expression (e.g. "0 8 * * *")
    cron: str = ""
    # interval: seconds between runs (e.g. 3600)
    interval_seconds: int = 0
    # once: ISO8601 datetime to fire once
    run_at: str = ""
    # group the agent belongs to (needed to push_task into the right engine)
    group_id: str = ""
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class ScheduledTaskCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    content: str
    agent_id: str
    group_id: str
    schedule_type: str = "interval"  # cron | interval | once
    cron: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    enabled: bool = True


class ScheduledTaskRun(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    scheduled_task_id: str
    status: str = "pending"  # pending | running | success | failed
    result: str | None = None
    started_at: str = ""
    finished_at: str = ""
