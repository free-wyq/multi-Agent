"""DAG dispatcher: fail-fast + parallel fan-out (Rust engine.rs 643-755).

``apply_fail_fast`` cascades failed status to pending steps whose dependencies
include a failed step, looping until stable. ``dispatch_ready_steps`` finds
ALL steps that are ready (pending + deps satisfied) and dispatches them
together — independent steps run concurrently as separate worker engines
(AgentEngine instances), each its own asyncio task. ``find_ready_steps`` is the
pure query. ``_dispatch_one`` dispatches a single step (mark dispatched, reply,
push_task, emit). If no step is dispatchable and all are done, the caller
routes to ``summarize``.

Group-graph fan-out (task: dispatch_next 改 LangGraph Send): ``build_dispatch_sends``
is the LangGraph-native twin of ``dispatch_ready_steps``. It runs the SAME
fail-fast + ready-query (``apply_fail_fast`` + ``find_ready_steps``, single
source of the DAG semantics) then, instead of ``push_task``-ing each ready step
to a worker inbox (the resident engine's band-out path), it returns one
``Send`` per ready step — each ``Send`` targets the agent node
``agent_<agent_id>`` with the step's instruction as ``incoming_message``.
LangGraph drives the ``Send``s in parallel within one ``ainvoke`` (the group
graph's in-graph fan-out), so independent steps run concurrently exactly as the
resident ``dispatch_ready_steps`` runs them as separate asyncio tasks. The step
status mutation (``pending`` → ``dispatched`` + ``task_id``) is identical so the
plan reflects the fan-out the same way; the only difference is the transport
(``Send`` to a node vs ``push_task`` to an inbox). ``build_dispatch_sends`` is
additive — ``dispatch_ready_steps`` stays untouched for the resident coordinator
engine until the group-graph migration swaps consumers over.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from langgraph.types import Send

from engine.inbox import push_task
from engine.reply import persist_agent_reply
from events import emit_task_dispatched

logger = logging.getLogger("multi-agent.dispatcher")

# Node-name convention shared with engine/group_graph + engine/worker: the
# per-agent node in the group graph is registered as ``agent_<agent_id>`` (the
# underscore separator — LangGraph forbids ':' and '|' in node names). This
# constant is the single source for the ``Send`` target so a step's ``agent_id``
# resolves to the same node name the group graph registered.
AGENT_NODE_PREFIX = "agent_"


def apply_fail_fast(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark pending steps failed if any dependency is failed, loop until stable.

    Each pass scans once: collect pending steps whose ``depends_on`` includes a
    failed step, mark them failed, then repeat until a pass finds none (fixpoint
    cascade — a newly-failed step can fail its own dependents on the next pass).

    B13 早退：内层依赖扫描在首次命中失败依赖后 ``break``（一个 failed dep 即足够
    判该 step 应级联失败，无需继续扫剩余 deps）；外层 while 在某轮
    ``failed_steps`` 为空时立即 ``break``（已无级联目标，达 fixpoint）。原实现
    命中后仍扫完该 step 的剩余 deps + 缺少「无 failed step 即 break」语义
    （靠 ``if not failed_steps: break`` 在每轮末尾判，等价但多一轮空扫描）。
    小 plan 无碍，但大 plan（深级联链）原 O(n²·deps) → 早退后均摊更省。
    行为零变：级联结果（哪些 step 最终 failed）与原实现逐字节一致——早退只跳过
    「已确定要 fail 的 step 的剩余 deps 扫描」，不改变判定逻辑。
    """
    while True:
        failed_steps: list[int] = []
        for s in plan:
            if s.get("status") != "pending":
                continue
            for dep in s.get("depends_on", []) or []:
                dep_step = next((d for d in plan if d.get("step") == dep), None)
                if dep_step and dep_step.get("status") == "failed":
                    failed_steps.append(s["step"])
                    break  # B13 早退：一个 failed dep 即级联失败，无需扫剩余 deps
        if not failed_steps:
            break  # B13 早退：本轮无新增失败 → fixpoint，跳出 while
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

    step_num: int = step["step"]
    agent_id: str = step["agent_id"]
    agent_name: str = step["agent_name"]
    instruction: str = step["instruction"]

    # reply: persist dispatch message + emit. This is a templated announce (B14):
    # the text is built from the step's agent_name + instruction, NOT streamed LLM
    # output, so it carries no model/elapsed_ms/tokens stats — ``data`` stays None
    # (aligns with A8/vg2: dispatch announce is excluded from the stats contract,
    # the same way node_dispatch's "📋 已制定协作计划" announce is excluded in
    # coordinator.py). The frontend's extractCoordStats returns null on a missing
    # elapsed_ms and renders no status line, which is correct for announce text
    # (stats wouldn't match the templated content). Delegating to persist_agent_reply
    # (engine.reply, B10) keeps the agent_reply shape a single source shared with the
    # registry's execute-path announce and the coordinator/worker graph replies.
    dispatch_msg = f"🚀 步骤 {step_num} 派发：\n@{agent_name} \n\n{instruction}"
    await persist_agent_reply(group_id, coordinator_id, dispatch_msg, None)

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

    This is the **resident coordinator engine's** fan-out path: each ready step
    is ``push_task``-ed to the target worker's inbox, waking that AgentEngine's
    run loop as an independent asyncio task. The group-graph twin
    (``build_dispatch_sends``) returns ``Send`` objects instead — same fail-fast
    + ready-query + step mutation, LangGraph-native transport.
    """
    plan = apply_fail_fast(plan)
    ready = find_ready_steps(plan)
    for step in ready:
        await _dispatch_one(group_id, coordinator_id, step)
    return ready


def agent_node_target(agent_id: str) -> str:
    """Canonical group-graph node name for an agent: ``agent_<agent_id>``.

    Single source for the ``Send`` target in ``build_dispatch_sends`` so a step's
    ``agent_id`` resolves to the same node name the group graph registered
    (``engine/group_graph.agent_node_name`` + ``engine.worker``'s goto convention).
    """
    return f"{AGENT_NODE_PREFIX}{agent_id}"


def build_dispatch_sends(
    group_id: str,
    coordinator_id: str,
    plan: list[dict[str, Any]],
) -> tuple[list[Send], list[dict[str, Any]]]:
    """LangGraph-native twin of ``dispatch_ready_steps``: return ``Send``s.

    Task: coordinator dispatch_next 节点 — dispatcher.dispatch_ready_steps 输出从
    ``push_task`` 改为 LangGraph ``Send``/并行 fan-out 到各 agent 节点（保 DAG
    fail-fast 与 ready_steps 逻辑）.

    Runs the **same** DAG fail-fast cascade + ready-query as
    ``dispatch_ready_steps`` (``apply_fail_fast(plan)`` + ``find_ready_steps(plan)``
    — single source of the DAG semantics, so fail-fast cascade + ready-step
    logic is byte-for-byte identical), then instead of ``push_task``-ing each
    ready step to a worker inbox, returns one ``Send`` per ready step. Each
    ``Send`` targets the agent node ``agent_<agent_id>`` (the group graph's
    per-agent node) with the step's ``instruction`` as ``incoming_message`` and
    the step's identity in ``incoming_data`` — so the agent node (``worker.
    make_agent_node``) speaks as that step's owner, exactly as the resident
    ``_dispatch_one`` set up the worker engine to do.

    Step mutation (``pending`` → ``dispatched`` + ``task_id``) is identical to
    ``_dispatch_one``: the plan reflects the fan-out the same way, so the
    coordinator's downstream ``handle_reply`` (MT-15 recovery + MT-14 adjust)
    matches the report-back to the dispatched step by ``task_id`` regardless of
    whether the dispatch transport was ``push_task`` (resident) or ``Send``
    (group graph). ``task_id`` is a fresh UUID per step (the resident path got it
    from ``push_task``'s return; here we mint it directly since no inbox is
    involved).

    Returns ``(sends, dispatched_steps)``:

    - ``sends`` — the list of ``Send`` objects for ``dispatch_next`` to return
      via ``Command(goto=sends, update=...)`` (LangGraph drives them in parallel
      within one ``ainvoke`` — the in-graph fan-out, each ``Send`` invokes the
      target agent node with its own state copy seeded by the ``Send``'s payload).
      Empty when no step is dispatchable (caller routes to ``summarize`` if all
      done, else ends — same ``dispatch_next`` routing as the resident path).
    - ``dispatched_steps`` — the dispatched step dicts (status mutated to
      ``dispatched``, ``task_id`` set), for ``dispatch_next`` to carry onto
      ``dispatch_plan`` via the ``replace_value`` reducer + re-emit the plan so
      the frontend PlanStep[] reflects the new statuses (mirrors the resident
      ``node_dispatch_next`` emit).

    The agent-node payload seeds each ``Send`` with the step's instruction +
    identity so ``worker.make_agent_node`` can build its brain context + speak as
    that step's owner without re-deriving from the plan. The payload keys mirror
    ``GroupState.incoming_*`` (``incoming_message`` / ``incoming_sender`` /
    ``incoming_kind`` / ``incoming_data``) so the agent node reads them exactly as
    the resident ``node_brain_decide`` reads ``WorkerState.incoming_*``.

    Additive — ``dispatch_ready_steps`` stays untouched for the resident
    coordinator engine (its consumers — m12 / mt15 / mt16 / vh10 / vh35 — keep
    patching + asserting on it unchanged). The group-graph ``dispatch_next``
    (a later wiring task) switches to ``build_dispatch_sends``.
    """
    plan = apply_fail_fast(plan)
    ready = find_ready_steps(plan)
    sends: list[Send] = []
    for step in ready:
        step["status"] = "dispatched"
        # fresh task_id — the resident path got this from push_task's return;
        # here no inbox is involved so mint it directly. Stored on the step so
        # handle_reply's task_id match (MT-15 recovery + MT-14 adjust) works
        # regardless of dispatch transport.
        task_id = f"task_{uuid.uuid4().hex}"
        step["task_id"] = task_id
        agent_id = step["agent_id"]
        sends.append(Send(
            agent_node_target(agent_id),
            {
                "group_id": group_id,
                "coordinator_id": coordinator_id,
                "current_speaker": agent_id,
                "incoming_message": step["instruction"],
                "incoming_sender": coordinator_id,
                # incoming_kind="coordinator_task" mirrors the resident path's
                # push_task semantics (a dispatched step is a coordinator-issued
                # task for the agent), so the agent node's brain context labels
                # the sender as the coordinator (not "user").
                "incoming_kind": "coordinator_task",
                "incoming_data": {
                    "step": step["step"],
                    "agent_id": agent_id,
                    "agent_name": step["agent_name"],
                    "instruction": step["instruction"],
                    "task_id": task_id,
                },
            },
        ))
    return sends, ready
