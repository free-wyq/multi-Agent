"""DAG dispatcher: fail-fast + parallel fan-out (Rust engine.rs 643-755).

``apply_fail_fast`` cascades failed status to pending steps whose dependencies
include a failed step, looping until stable. ``dispatch_ready_steps`` finds
ALL steps that are ready (pending + deps satisfied) and dispatches them
together — independent steps run concurrently as separate worker engines
(AgentEngine instances), each its own asyncio task. ``find_ready_steps`` is the
pure query. ``_dispatch_one`` dispatches a single step (mark dispatched, reply,
push_task, emit). If no step is dispatchable and all are done, the caller
routes to ``summarize``.
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


def find_ready_steps(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return all steps ready to dispatch now (pending + deps all completed).

    Independent steps (empty ``depends_on`` or whose deps are all completed) are
    returned together so the coordinator can fan them out in parallel — each
    goes to its own worker engine which runs as an independent asyncio task.
    """
    ready: list[dict[str, Any]] = []
    for s in plan:
        if s.get("status") != "pending":
            continue
        deps_ok = all(
            any(d.get("step") == dep and d.get("status") == "completed" for d in plan)
            for dep in s.get("depends_on", []) or []
        )
        if deps_ok:
            ready.append(s)
    return ready


async def _dispatch_one(
    group_id: str,
    coordinator_id: str,
    step: dict[str, Any],
) -> None:
    """Dispatch a single ready step: mark dispatched, reply, push_task, emit.

    Mutates ``step`` to ``dispatched`` and stores the pushed ``task_id`` on it.
    """
    step["status"] = "dispatched"

    step_num = step["step"]
    agent_id = step["agent_id"]
    agent_name = step["agent_name"]
    instruction = step["instruction"]

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

    # push task to worker — this wakes the target AgentEngine's run loop as an
    # independent asyncio task, so multiple dispatched steps run concurrently.
    pushed = await push_task(
        group_id,
        coordinator_id,
        agent_id,
        instruction,
        {"step": step_num, "agent_name": agent_name},
    )
    step["task_id"] = pushed["id"]

    await emit_task_dispatched(
        group_id, pushed["id"], step_num, agent_id, agent_name, instruction
    )

    logger.info(
        "[dispatcher] dispatched step %s to %s (task_id=%s)",
        step_num, agent_name, pushed["id"],
    )


async def dispatch_ready_steps(
    group_id: str,
    coordinator_id: str,
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dispatch ALL ready steps in parallel (DAG fan-out).

    Finds every step that is pending with dependencies satisfied and dispatches
    each one. Independent steps (no shared dependency) are dispatched together so
    their worker engines run concurrently as separate asyncio tasks. Mutates
    ``plan`` in place. Returns the list of dispatched step dicts (empty if none
    were dispatchable — caller checks whether all are done to route to summarize).
    """
    plan = apply_fail_fast(plan)
    ready = find_ready_steps(plan)
    for step in ready:
        await _dispatch_one(group_id, coordinator_id, step)
    return ready
