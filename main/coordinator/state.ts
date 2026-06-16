/**
 * Coordinator 状态定义
 *
 * 对应原 Python CoordinatorState TypedDict
 */

export interface SubTask {
  title: string
  description: string
  assigned_agent_id: string
  dependencies: number[]     // 依赖的前置子任务序号（0-based）
}

export interface CoordinatorState {
  // 输入
  group_id: string
  requirement: string

  // 意图分析
  intent_analysis: string
  involved_roles: string[]

  // 任务拆解
  subtasks: SubTask[]

  // DAG
  dag_nodes: Record<string, unknown>[]
  dag_edges: Record<string, unknown>[]

  // 调度执行
  pending_task_ids: string[]
  running_task_ids: string[]
  completed_task_ids: string[]
  failed_task_ids: string[]

  // 结果汇总
  summary: string
  artifacts: Record<string, unknown>[]
}

export function initialState(groupId: string, requirement: string): CoordinatorState {
  return {
    group_id: groupId,
    requirement,
    intent_analysis: '',
    involved_roles: [],
    subtasks: [],
    dag_nodes: [],
    dag_edges: [],
    pending_task_ids: [],
    running_task_ids: [],
    completed_task_ids: [],
    failed_task_ids: [],
    summary: '',
    artifacts: [],
  }
}
