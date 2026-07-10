"""Task + TaskCreatePayload Pydantic models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

TaskStatus = Literal[
    "submitted", "working", "completed", "failed", "canceled", "input_required"
]


class Task(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    group_id: str
    parent_task_id: str | None = None
    title: str
    description: str | None = None
    status: TaskStatus | str = "submitted"
    assigned_agent_id: str | None = None
    instance_id: str | None = None
    dependencies: list[str] = []
    artifact_path: str | None = None
    artifact: dict[str, Any] | None = None
    exit_code: int | None = None
    error_message: str | None = None
    result_summary: str | None = None
    dag_order: int | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


class TaskCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    group_id: str
    title: str
    description: str | None = None
    assigned_agent_id: str | None = None
    dependencies: list[str] = []
    dag_order: int | None = None
