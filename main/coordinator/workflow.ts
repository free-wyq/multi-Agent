/**
 * Coordinator 工作流
 *
 * 替代 LangGraph 状态图：
 * - LangGraph 5 节点 → 简单 async 方法 + while 循环
 * - analyze → decompose → dispatch → monitor(循环) → summarize
 */

import { store } from '../store/store'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import { chatCompletion, structuredInvoke, getDefaultConfig } from './llm'
import {
  buildAnalyzePrompt, buildDecomposePrompt, buildSummarizePrompt,
  buildRolesHint, buildRolesContext, buildRolesHint as _unused,
  INTENT_ANALYSIS_SCHEMA, TASK_DECOMPOSITION_SCHEMA,
  ROLE_DESCRIPTIONS,
} from './prompts'
import { initialState } from './state'
import type { CoordinatorState, SubTask } from './state'
import type { IntentAnalysis, TaskDecomposition, SubTaskDef } from '../store/types'

export class CoordinatorWorkflow {
  private config = getDefaultConfig()

  async run(groupId: string, requirement: string): Promise<CoordinatorState> {
    const state = initialState(groupId, requirement)

    try {
      // 1. 分析意图
      await this.analyze(state)

      // 2. 拆解任务
      await this.decompose(state)

      // 3. 派发任务
      await this.dispatch(state)

      // 4. 监控循环
      while (this.shouldContinue(state) === 'monitor') {
        await this.monitor(state)
      }

      // 5. 汇总结果
      if (this.shouldContinue(state) === 'summarize') {
        await this.summarize(state)
      }
    } catch (err) {
      console.error('Coordinator workflow error:', err)
    }

    return state
  }

  // ── 分析意图 ──────────────────────────────────────────────

  private async analyze(state: CoordinatorState): Promise<void> {
    const availableRoles = store.getGroupRoles(state.group_id)
    const rolesHint = buildRolesHint(availableRoles)
    const prompt = buildAnalyzePrompt(state.requirement, rolesHint)

    const result = await structuredInvoke<IntentAnalysis>(
      this.config,
      prompt,
      INTENT_ANALYSIS_SCHEMA,
    )

    state.intent_analysis = result.analysis
    state.involved_roles = result.involved_roles
  }

  // ── 拆解任务 ──────────────────────────────────────────────

  private async decompose(state: CoordinatorState): Promise<void> {
    const availableRoles = store.getGroupRoles(state.group_id)
    const rolesContext = availableRoles.length
      ? availableRoles.map(r => `- ${r.role}: ${r.name}`).join('\n')
      : buildRolesContext(state.involved_roles)

    const prompt = buildDecomposePrompt(state.requirement, state.intent_analysis, rolesContext)
    const result = await structuredInvoke<TaskDecomposition>(
      this.config,
      prompt,
      TASK_DECOMPOSITION_SCHEMA,
    )

    // 将 SubTaskDef 转为 SubTask，并规范化角色
    const subtasks: SubTask[] = result.subtasks.map(st => ({
      title: st.title,
      description: st.description,
      assigned_agent_id: this.normalizeRole(st.assigned_role, availableRoles),
      dependencies: st.depends_on,
    }))

    // 构建 DAG 可视化结构
    const { dag_nodes, dag_edges } = this.buildDagStructure(subtasks)

    state.subtasks = subtasks
    state.dag_nodes = dag_nodes
    state.dag_edges = dag_edges
  }

  // ── 派发任务 ──────────────────────────────────────────────

  private async dispatch(state: CoordinatorState): Promise<void> {
    const groupId = state.group_id
    const subtasks = state.subtasks
    const roleToAgentId = store.resolveRoleMapping(groupId)
    const channel = `${CHANNEL_PREFIX}${groupId}`

    // 为所有子任务创建 Task 记录
    const taskIdMap: Record<number, string> = {}
    for (let idx = 0; idx < subtasks.length; idx++) {
      const st = subtasks[idx]
      const agentId = roleToAgentId[st.assigned_agent_id]

      // 解析依赖：子任务序号 → 真实 task ID
      const depIds = st.dependencies
        .filter(d => d in taskIdMap)
        .map(d => taskIdMap[d])

      const task = store.createTask({
        group_id: groupId,
        title: st.title,
        description: st.description,
        assigned_agent_id: agentId,
        dependencies: depIds,
        dag_order: idx,
      })
      taskIdMap[idx] = task.id
    }

    // 派发无依赖的任务（状态改为 working）
    for (let idx = 0; idx < subtasks.length; idx++) {
      if (!subtasks[idx].dependencies.length) {
        const taskId = taskIdMap[idx]
        store.updateTask(taskId, { status: 'working' })
        state.running_task_ids.push(taskId)

        // 发布 task_dispatch 事件
        eventBus.publishAndPersist(channel, {
          group_id: groupId,
          task_id: taskId,
          sender_id: 'coordinator',
          receiver_id: subtasks[idx].assigned_agent_id || 'broadcast',
          type: 'task_dispatch',
          content: `任务已派发: ${subtasks[idx].title}`,
          data: { task_id: taskId, agent_id: subtasks[idx].assigned_agent_id },
        })
      }
    }
  }

  // ── 监控任务 ──────────────────────────────────────────────

  private async monitor(state: CoordinatorState): Promise<void> {
    const groupId = state.group_id
    const channel = `${CHANNEL_PREFIX}${groupId}`

    if (!state.running_task_ids.length) return

    // 轮询等待（每 2s 检查一次，30s 超时）
    const startTime = Date.now()
    const TIMEOUT = 30_000
    const POLL_INTERVAL = 2_000

    while (Date.now() - startTime < TIMEOUT) {
      await this.sleep(POLL_INTERVAL)

      // 检查每个 running task 的状态
      const stillRunning: string[] = []
      for (const taskId of state.running_task_ids) {
        const task = store.getTask(taskId)
        if (!task) continue

        if (task.status === 'completed') {
          state.completed_task_ids.push(taskId)
        } else if (task.status === 'failed') {
          state.failed_task_ids.push(taskId)
        } else {
          stillRunning.push(taskId)
        }
      }

      state.running_task_ids = stillRunning

      // 检查是否有新的可派发任务
      const readyTasks = store.getReadyTasks(groupId)
      for (const rt of readyTasks) {
        if (!state.running_task_ids.includes(rt.id) && !state.completed_task_ids.includes(rt.id)) {
          store.updateTask(rt.id, { status: 'working' })
          state.running_task_ids.push(rt.id)

          eventBus.publishAndPersist(channel, {
            group_id: groupId,
            task_id: rt.id,
            sender_id: 'coordinator',
            receiver_id: rt.assigned_agent_id || 'broadcast',
            type: 'task_dispatch',
            content: `下游任务已派发: ${rt.title}`,
            data: { task_id: rt.id },
          })
        }
      }

      // 没有 running 任务了，退出轮询
      if (!state.running_task_ids.length) break
    }
  }

  // ── 汇总结果 ──────────────────────────────────────────────

  private async summarize(state: CoordinatorState): Promise<void> {
    const tasks = store.listTasksByGroup(state.group_id)
    const summaries: string[] = []
    const artifacts: Record<string, unknown>[] = []

    for (const t of tasks) {
      if (t.result_summary) {
        summaries.push(`- ${t.title}: ${t.result_summary}`)
      }
      if (t.artifact_path) {
        artifacts.push({
          task_id: t.id,
          title: t.title,
          path: t.artifact_path,
          description: t.result_summary || '',
        })
      }
    }

    const prompt = buildSummarizePrompt(summaries, state.requirement)
    const summaryText = await chatCompletion(
      { ...this.config, temperature: 0.3 },
      [{ role: 'user', content: prompt }],
    )

    state.summary = summaryText
    state.artifacts = artifacts
  }

  // ── 条件判断 ──────────────────────────────────────────────

  private shouldContinue(state: CoordinatorState): 'monitor' | 'summarize' | 'end' {
    const { running_task_ids, completed_task_ids, failed_task_ids, subtasks } = state

    if (!subtasks.length) return 'end'

    if (running_task_ids.length) return 'monitor'

    if (completed_task_ids.length + failed_task_ids.length >= subtasks.length) {
      return 'summarize'
    }

    return 'monitor'
  }

  // ── 辅助 ──────────────────────────────────────────────────

  private buildDagStructure(subtasks: SubTask[]): {
    dag_nodes: Record<string, unknown>[]
    dag_edges: Record<string, unknown>[]
  } {
    const nodes = subtasks.map((st, idx) => ({
      id: `task-${idx}`,
      label: st.title,
      agent_id: st.assigned_agent_id,
      status: 'submitted',
    }))

    const edges: Record<string, unknown>[] = []
    for (let idx = 0; idx < subtasks.length; idx++) {
      for (const dep of subtasks[idx].dependencies) {
        edges.push({ source: `task-${dep}`, target: `task-${idx}` })
      }
    }

    return { dag_nodes: nodes, dag_edges: edges }
  }

  private normalizeRole(
    roleStr: string,
    availableRoles: { id: string; role: string; name: string }[],
  ): string {
    if (!roleStr || !availableRoles.length) return roleStr

    const lowerStr = roleStr.toLowerCase()

    // 1. 精确匹配
    for (const r of availableRoles) {
      if (roleStr === r.role) return r.role
    }

    // 2. 模糊匹配
    for (const r of availableRoles) {
      if (r.role.toLowerCase().includes(lowerStr) || lowerStr.includes(r.role.toLowerCase())) {
        return r.role
      }
    }

    // 3. 关键词匹配
    let bestMatch = ''
    let bestScore = 0

    for (const r of availableRoles) {
      const keywords = new Set<string>()
      // 从 role 标识提取关键词
      for (const kw of r.role.toLowerCase().replace(/_/g, '-').split('-')) {
        if (kw.length > 1) keywords.add(kw)
      }
      // 从 name 提取
      if (r.name) {
        for (const kw of r.name.toLowerCase().split(/\s+/)) {
          if (kw.length > 1) keywords.add(kw)
        }
      }

      const hits = [...keywords].filter(kw => lowerStr.includes(kw)).length
      if (hits > bestScore) {
        bestScore = hits
        bestMatch = r.role
      }
    }

    return bestMatch && bestScore > 0 ? bestMatch : roleStr
  }

  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms))
  }
}

export const coordinatorWorkflow = new CoordinatorWorkflow()
