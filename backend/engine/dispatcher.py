"""DAG dispatcher: fail-fast + dispatch_next_step (Rust engine.rs 643-755).

``apply_fail_fast`` cascades failed status to pending steps whose dependencies
include a failed step, looping until stable. ``dispatch_next_step`` finds the
first pending step whose dependencies are all completed, marks it dispatched,
replies with the dispatch message, ``push_task`` to the worker, and emits the
``task_dispatched`` bus event. If no step is dispatchable and all are done,
the caller routes to ``summarize``.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.inbox import push_task
from events import emit_message_added, emit_task_dispatched
from store import crud

logger = logging.getLogger("multi-agent.dispatcher")


def apply_fail_fast(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark pending steps failed if any dependency is failed, loop until stable."""
    while True:
        failed_steps: list[int] = []
        for s in plan:
            if s.get("status") != "pending":
                continue
            for dep in s.get("depends_on", []) or []:
                dep_step = next((d for d in plan if d.get("step") == dep), None)
                if dep_step and dep_step.get("status") == "failed":
                    failed_steps.append(s["step"])
                    break
        if not failed_steps:
            break
        for s in plan:
            if s.get("step") in failed_steps:
                s["status"] = "failed"
                s["result"] = "上游步骤失败，跳过"
    return plan


async def dispatch_next_step(
    group_id: str,
    coordinator_id: str,
    plan: list[dict[str, Any]],
) -> dict | None:
    """Dispatch the next ready step (pending + deps all completed).

    Mutates the step in ``plan`` to ``dispatched``, replies with the dispatch
    message (persist + emit + mention route via crud.add_message +
    emit_message_added), ``push_task`` to the worker, stores the pushed
    ``task_id`` back on the step, and emits ``task_dispatched``.

    Returns the dispatched step dict, or ``None`` if no step was dispatchable
    (caller checks whether all are done to route to summarize).
    """
    plan = apply_fail_fast(plan)

    next_step: dict | None = None
    for s in plan:
        if s.get("status") != "pending":
            continue
        deps_ok = all(
            any(d.get("step") == dep and d.get("status") == "completed" for d in plan)
            for dep in s.get("depends_on", []) or []
        )
        if deps_ok:
            next_step = s
            break

    if next_step is None:
        return None

    next_step["status"] = "dispatched"

    step_num = next_step["step"]
    agent_id = next_step["agent_id"]
    agent_name = next_step["agent_name"]
    instruction = next_step["instruction"]

    # reply: persist dispatch message + emit
    dispatch_msg = f"🚀 步骤 {step_num} 派发：\n@{agent_name} \n\n{instruction}"
    msg = await crud.create_message(
        {
            "group_id": group_id,
            "task_id": None,
            "sender_id": coordinator_id,
            "receiver_id": "broadcast",
            "type": "agent_reply",
            "content": dispatch_msg,
            "data": None,
        }
    )
    await emit_message_added(msg.model_dump())

    # push task to worker
    pushed = await push_task(
        group_id,
        coordinator_id,
        agent_id,
        instruction,
        {"step": step_num, "agent_name": agent_name},
    )
    next_step["task_id"] = pushed["id"]

    await emit_task_dispatched(
        group_id, pushed["id"], step_num, agent_id, agent_name, instruction
    )

    logger.info(
        "[dispatcher] dispatched step %s to %s (task_id=%s)",
        step_num, agent_name, pushed["id"],
    )
    return next_step
