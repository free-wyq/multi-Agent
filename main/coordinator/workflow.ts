/**
 * Coordinator 工作流（A2A 架构改造后）
 *
 * 核心变化：
 * 1. dispatch() 不再直接调用 store.createTask() + eventBus.publishAndPersist()
 *    改为通过 sharedState.pushTask() 向各 agent 的收件箱投递任务
 * 2. monitor() 改为轮询 sharedState 的 notifyQueue，而非直接读 store.tasks
 * 3. summarize() 完成后通过 sharedState.pushNotify() 发布结果通知
 * 4. 仍然在主进程内运行（coordinator 不走 CLI spawn）
 */

import { store } from '../store/store'
import { sharedState } from '../store/shared-state'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import { chatCompletion, structuredInvoke, getDefaultConfig } from './llm'
import {
  buildAnalyzePrompt, buildDecomposePrompt, buildSummarizePrompt,
  buildRolesHint, buildRolesContext,
  INTENT_ANALYSIS_SCHEMA, TASK_DECOMPOSITION_SCHEMA,
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

      // 3. 派发任务（通过 SharedStateCenter 扔字条）
      await this.dispatch(state)

      // 4. 监控循环（轮询 notifyQueue）
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

    const subtasks: SubTask[] = result.subtasks.map(st => ({
      title: st.title,
      description: st.description,
      assigned_agent_id: this.normalizeRole(st.assigned_role, availableRoles),
      dependencies: st.depends_on,
    }))

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

    // 同时也在 store 中创建 Task 记录（供前端 DAG 展示）
    const taskIdMap: Record<number, string> = {}
    for (let idx = 0; idx < subtasks.length; idx++) {
      const st = subtasks[idx]
      const agentId = roleToAgentId[st.assigned_agent_id]
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

    state.taskIdMap = taskIdMap  // 保存映射供 monitor 使用

    // 派发无依赖的任务：通过 SharedStateCenter 向各 agent 扔字条
    for (let idx = 0; idx < subtasks.length; idx++) {
      if (!subtasks[idx].dependencies.length) {
        const taskId = taskIdMap[idx]
        const agentId = roleToAgentId[subtasks[idx].assigned_agent_id]

        store.updateTask(taskId, { status: 'working' })
        state.running_task_ids.push(taskId)

        // A2A：扔字条到 sharedState
        sharedState.pushTask({
          group_id: groupId,
          sender_id: 'coordinator',
          receiver_id: agentId || 'broadcast',
          content: subtasks[idx].description || subtasks[idx].title,
          data: { task_id: taskId, dag_order: idx, title: subtasks[idx].title },
        })

        // 同时广播通知给前端
        eventBus.publishAndPersist(`${CHANNEL_PREFIX}${groupId}`, {
          group_id: groupId,
          task_id: taskId,
          sender_id: 'coordinator',
          receiver_id: agentId || 'broadcast',
          type: 'task_dispatch',
          content: `任务已派发: ${subtasks[idx].title}`,
          data: { task_id: taskId, agent_id: agentId },
        })
      }
    }
  }

  // ── 监控任务 ──────────────────────────────────────────────

  private async monitor(state: CoordinatorState): Promise<void> {
    const groupId = state.group_id
    if (!state.running_task_ids.length) return

    const startTime = Date.now()
    const TIMEOUT = 60_000    // 延长到 60s（CLI 执行可能较慢）
    const POLL_INTERVAL = 2_000

    while (Date.now() - startTime < TIMEOUT) {
      await this.sleep(POLL_INTERVAL)

      // 轮询 notifyQueue：查找 task_complete / task_failed 通知
      const { notifies } = sharedState.pollInbox(groupId, 'coordinator')
      const taskNotifies = notifies.filter(
        n => n.type === 'task_complete' || n.type === 'task_failed',
      )

      const stillRunning: string[] = []
      for (const taskId of state.running_task_ids) {
        const notify = taskNotifies.find(n => n.data?.task_id === taskId)
        if (notify) {
          if (notify.type === 'task_complete') {
            state.completed_task_ids.push(taskId)
            // 同步更新 store 中的 task 状态
            const task = store.getTask(taskId)
            if (task) {
              store.updateTask(taskId, {
                status: 'completed',
                result_summary: notify.content,
              })
            }
          } else {
            state.failed_task_ids.push(taskId)
            const task = store.getTask(taskId)
            if (task) {
              store.updateTask(taskId, {
                status: 'failed',
                result_summary: notify.content,
              })
            }
          }
        } else {
          // 还在 running，检查 store 中是否已有状态更新
          const task = store.getTask(taskId)
          if (task?.status === 'completed') {
            state.completed_task_ids.push(taskId)
          } else if (task?.status === 'failed') {
            state.failed_task_ids.push(taskId)
          } else {
            stillRunning.push(taskId)
          }
        }
      }

      state.running_task_ids = stillRunning

      // 检查是否有新的可派发任务（依赖已完成的下游任务）
      for (let idx = 0; idx < state.subtasks.length; idx++) {
        const st = state.subtasks[idx]
        const taskId = state.taskIdMap?.[idx]
        if (!taskId) continue

        // 如果已在运行/完成/失败，跳过
        if (state.running_task_ids.includes(taskId)) continue
        if (state.completed_task_ids.includes(taskId)) continue
        if (state.failed_task_ids.includes(taskId)) continue

        // 检查依赖是否全部完成
        const depsDone = st.dependencies.every(depIdx => {
          const depTaskId = state.taskIdMap?.[depIdx]
          return depTaskId ? state.completed_task_ids.includes(depTaskId) : true
        })

        if (depsDone) {
          const roleToAgentId = store.resolveRoleMapping(groupId)
          const agentId = roleToAgentId[st.assigned_agent_id]

          store.updateTask(taskId, { status: 'working' })
          state.running_task_ids.push(taskId)

          // A2A：扔字条
          sharedState.pushTask({
            group_id: groupId,
            sender_id: 'coordinator',
            receiver_id: agentId || 'broadcast',
            content: st.description || st.title,
            data: { task_id: taskId, dag_order: idx, title: st.title },
          })

          eventBus.publishAndPersist(`${CHANNEL_PREFIX}${groupId}`, {
            group_id: groupId,
            task_id: taskId,
            sender_id: 'coordinator',
            receiver_id: agentId || 'broadcast',
            type: 'task_dispatch',
            content: `下游任务已派发: ${st.title}`,
            data: { task_id: taskId },
          })
        }
      }

      // 没有 running 了，退出轮询
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

    // A2A：通过 notifyQueue 发布结果
    sharedState.pushNotify({
      group_id: state.group_id,
      type: 'coordinator_reply',
      sender_id: 'coordinator',
      receiver_id: 'broadcast',
      content: summaryText,
      data: { artifacts },
    })
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

    for (const r of availableRoles) {
      if (roleStr === r.role) return r.role
    }

    for (const r of availableRoles) {
      if (r.role.toLowerCase().includes(lowerStr) || lowerStr.includes(r.role.toLowerCase())) {
        return r.role
      }
    }

    let bestMatch = ''
    let bestScore = 0

    for (const r of availableRoles) {
      const keywords = new Set<string>()
      for (const kw of r.role.toLowerCase().replace(/_/g, '-').split('-')) {
        if (kw.length > 1) keywords.add(kw)
      }
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
