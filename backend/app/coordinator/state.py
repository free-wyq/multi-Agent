"""
群主状态定义

LangGraph 状态图的状态对象，贯穿整个调度流程。
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph.message import add_messages


class SubTask(TypedDict):
    """子任务定义（群主拆解后的输出）"""
    title: str
    description: str
    assigned_agent_id: str
    dependencies: list[str]  # 依赖的前置子任务序号（0-based）


class CoordinatorState(TypedDict):
    """群主 LangGraph 状态图的全局状态

    贯穿 analyze → decompose → dispatch → monitor → summarize 全流程。
    """
    # ── 输入 ────────────────────────────────────────────────────
    group_id: str                          # 所属群组
    requirement: str                       # 用户原始需求

    # ── 意图分析 ─────────────────────────────────────────────────
    intent_analysis: str                    # 意图分析结果（自然语言）
    involved_roles: list[str]              # 涉及的角色标识列表

    # ── 任务拆解 ─────────────────────────────────────────────────
    subtasks: list[SubTask]                 # 拆解后的子任务列表

    # ── DAG ──────────────────────────────────────────────────────
    dag_nodes: list[dict]                   # DAG 节点列表 [{id, label, agent_id, status}]
    dag_edges: list[dict]                   # DAG 边列表 [{source, target}]

    # ── 调度执行 ──────────────────────────────────────────────────
    pending_task_ids: list[str]             # 待派发的任务 ID
    running_task_ids: list[str]             # 正在执行的任务 ID
    completed_task_ids: list[str]           # 已完成的任务 ID
    failed_task_ids: list[str]              # 失败的任务 ID

    # ── 结果汇总 ──────────────────────────────────────────────────
    summary: str                            # 最终汇总结果
    artifacts: list[dict]                   # 产出物列表 [{task_id, path, description}]

    # ── 消息（LangGraph 内部节点通信） ────────────────────────────
    messages: list[dict]                    # add_messages reducer 管理
