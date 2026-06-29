/**
 * Agent Engine — 常驻智能体引擎（A2A 架构改造后）
 *
 * 核心变化：
 * 1. 不再使用私有 inbox 数组，而是轮询 SharedStateCenter 的"收件箱"
 * 2. 任务执行改为非阻塞：spawn CLI 后不再 await，主循环继续消费消息
 * 3. Coordinator 也走同样的 engine → inbox → brainDecide → execute 流程
 * 4. 完全解耦：只与 SharedStateCenter 交互，不直接与其他 engine 通信
 */

import { store } from '../store/store'
import { sharedState } from '../store/shared-state'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import { brainDecide } from './brain'
import { coordinatorBrainDecide } from './coordinator-brain'
import { getDefaultConfig } from '../coordinator/llm'
import { ClaudeCodeRuntime } from '../runtime/claude-code-runtime'
import { coordinatorWorkflow } from '../coordinator/workflow'
import type {
  AgentDefinition, BrainDecision, TaskQueueItem, NotifyQueueItem,
} from '../store/types'
import type { CoordinatorBrainDecision, DispatchStep } from './coordinator-brain'

export type EngineStatus = 'idle' | 'thinking' | 'executing' | 'offline'

export class AgentEngine {
  id: string
  name: string
  role: string
  systemPrompt: string
  groupId: string

  status: EngineStatus = 'idle'
  currentTaskId: string | null = null

  private intervalHandle: NodeJS.Timeout | null = null
  private shutdown = false
  private memory: { role: string; content: string; ts: string }[] = []
  private runtime: ClaudeCodeRuntime | null = null

  /** 上次轮询时间戳（用于增量拉取通知） */
  private lastPollAt = new Date(0).toISOString()

  /** 防循环路由表：agentId -> 上次路由时间(秒) */
  private recentRoutes: Record<string, number> = {}

  /** 正在处理的本地任务（防止重复消费） */
  private processingTaskIds = new Set<string>()

  /** ===== Coordinator 调度状态 ===== */
  private dispatchPlan: DispatchStep[] = []
  private dispatchStep = 0

  constructor(agentDef: AgentDefinition, groupId: string) {
    this.id = agentDef.id
    this.name = agentDef.name
    this.role = agentDef.role
    this.systemPrompt = agentDef.system_prompt || ''
    this.groupId = groupId
  }

  // ── 生命周期 ──────────────────────────────────────────────

  start(): void {
    this.shutdown = false
    this.status = 'idle'

    // 主循环：每 100ms 轮询共享状态的收件箱
    this.intervalHandle = setInterval(() => {
      this._pollLoop()
    }, 100)
  }

  stop(): void {
    this.shutdown = true
    if (this.intervalHandle) {
      clearInterval(this.intervalHandle)
      this.intervalHandle = null
    }
    if (this.runtime) {
      this.runtime.stop()
      this.runtime = null
    }
    this.status = 'offline'
  }

  pushMessage(message: Record<string, unknown>): void {
    // 旧 API 兼容：将 pushMessage 转译为 SharedStateCenter 的 pushTask/pushNotify
    if (message.type === 'task_dispatch') {
      sharedState.pushTask({
        group_id: this.groupId,
        sender_id: (message.sender_id as string) || 'coordinator',
        receiver_id: this.id,
        content: (message.content as string) || '',
        data: message.data as Record<string, unknown>,
      })
    } else {
      sharedState.pushNotify({
        group_id: this.groupId,
        type: 'agent_reply',
        sender_id: (message.sender_id as string) || 'user',
        receiver_id: this.id,
        content: (message.content as string) || '',
        data: message.data as Record<string, unknown>,
      })
    }
  }

  // ── 主循环：轮询共享收件箱 ────────────────────────────────

  private _pollLoop(): void {
    if (this.shutdown) return

    // 增量拉取：只取上次轮询之后的新消息
    const inbox = sharedState.pollInbox(this.groupId, this.id, {
      since: this.lastPollAt,
    })
    this.lastPollAt = new Date().toISOString()

    // 先处理任务（如果不在 executing 状态）
    if (this.status !== 'executing') {
      for (const task of inbox.tasks) {
        if (this.processingTaskIds.has(task.id)) continue
        this.processingTaskIds.add(task.id)
        this._claimAndExecute(task).catch(err => {
          console.error(`[AgentEngine ${this.name}] 执行任务失败:`, err)
          this.processingTaskIds.delete(task.id)
        })
        return // 一次只处理一个任务
      }
    }

    // 再处理通知（聊天消息等）—— 即使 executing 也可以消费通知（但暂不处理新的聊天触发 execute）
    for (const notify of inbox.notifies) {
      this._handleNotify(notify).catch(err => {
        console.error(`[AgentEngine ${this.name}] 处理通知失败:`, err)
      })
    }
  }

  // ── 任务执行（非阻塞）─────────────────────────────────────

  private async _claimAndExecute(task: TaskQueueItem): Promise<void> {
    const instance = store.listInstancesByGroup(this.groupId).find(i => i.definition_id === this.id)
    const claimed = sharedState.claimTask(this.groupId, this.id, instance?.id || this.id)
    if (!claimed || claimed.id !== task.id) {
      this.processingTaskIds.delete(task.id)
      return
    }

    this.status = 'executing'
    this.currentTaskId = task.id

    await this._publishLog(task.id, `▶ [${this.name}] 开始执行任务: ${task.content.substring(0, 50)}...`)

    try {
      if (this.role === 'coordinator') {
        await this._executeCoordinator(task)
      } else {
        await this._executeNormal(task)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      sharedState.completeTask(task.id, false, `❌ 执行异常: ${msg}`)
      await this._publishLog(task.id, `❌ 执行异常: ${msg}`)
      this._resetIdle()
    }
  }

  private async _executeNormal(task: TaskQueueItem): Promise<void> {
    const agentDef = store.getAgent(this.id)
    if (!agentDef) {
      sharedState.completeTask(task.id, false, '找不到智能体定义')
      await this._publishLog(task.id, '❌ 找不到智能体定义')
      this._resetIdle()
      return
    }

    const runtime = new ClaudeCodeRuntime(this.groupId, agentDef)
    this.runtime = runtime

    await this._publishLog(task.id, `🚀 启动 Claude Code CLI...`)

    // 非阻塞执行！不 await，用 then/catch/finally 收尾
    runtime.execute(task.content, task.id)
      .then(result => {
        sharedState.completeTask(
          task.id,
          result.success,
          result.output?.substring(0, 500),
          { exit_code: result.exitCode, full_output: result.output },
        )
        if (result.success) {
          this._reply(`任务完成 🎉\n${result.output?.substring(0, 200) || '已完成'}`)
        } else {
          this._reply(`执行出错了: ${result.output || '未知错误'}`)
        }
        // 自动向 coordinator 汇报子任务完成
        const group = store.getGroup(this.groupId)
        if (group?.coordinator_id && group.coordinator_id !== this.id) {
          sharedState.pushNotify({
            group_id: this.groupId,
            type: 'agent_reply',
            sender_id: this.id,
            receiver_id: group.coordinator_id,
            content: `步骤完成：${task.content}\n\n结果：${result.output?.substring(0, 200) || '已完成'}`,
            data: { task_id: task.id, success: result.success },
          })
        }
      })
      .catch((err: Error) => {
        sharedState.completeTask(task.id, false, `执行异常: ${err.message}`)
        this._reply(`执行出错了: ${err.message}`)
      })
      .finally(() => {
        runtime.stop()
        this.runtime = null
        this.currentTaskId = null
        this.status = 'idle'
        this.processingTaskIds.delete(task.id)
      })
  }

  private async _executeCoordinator(task: TaskQueueItem): Promise<void> {
    // Coordinator 调用工作流（不 spawn CLI），异步不阻塞
    coordinatorWorkflow.run(this.groupId, task.content)
      .then(state => {
        sharedState.completeTask(
          task.id,
          true,
          state.summary,
          { artifacts: state.artifacts },
        )
        this._reply(`协调完成 🎉\n${state.summary?.substring(0, 300) || '已完成'}`)
      })
      .catch(err => {
        sharedState.completeTask(task.id, false, `协调者工作流失败: ${err.message}`)
        this._reply(`协调失败: ${err.message}`)
      })
      .finally(() => {
        this.currentTaskId = null
        this.status = 'idle'
        this.processingTaskIds.delete(task.id)
      })
  }

  // ── 通知处理（聊天消息）───────────────────────────────────

  private async _handleNotify(notify: NotifyQueueItem): Promise<void> {
    if (notify.sender_id === this.id) return

    const content = notify.content
    const sender = notify.sender_id

    // ===== Coordinator：走调度大脑 =====
    if (this.role === 'coordinator') {
      await this._handleNotifyAsCoordinator(content, sender)
      return
    }

    // ===== 普通成员：走通用 brainDecide =====
    const context = this._buildContext()
    const displayMsg = sender !== 'user' && sender !== 'coordinator'
      ? `[来自智能体 ${sender}] ${content}`
      : content

    const config = getDefaultConfig()
    const decision = await brainDecide(config, this.role, this.name, context, displayMsg)

    this.memory.push({ role: 'user', content, ts: new Date().toISOString() })

    if (decision.action === 'chat') {
      await this._reply(decision.content)
      this.memory.push({ role: 'assistant', content: decision.content, ts: new Date().toISOString() })
    } else if (decision.action === 'execute') {
      await this._reply(`收到，我来 ${decision.content.substring(0, 30)}...`)
      sharedState.pushTask({
        group_id: this.groupId,
        sender_id: this.id,
        receiver_id: this.id,
        content: decision.content,
      })
    } else if (decision.action === 'ask') {
      await this._reply(decision.content)
    } else {
      await this._reply(decision.content)
    }
  }

  /** Coordinator 专属：调度中枢 */
  private async _handleNotifyAsCoordinator(content: string, sender: string): Promise<void> {
    const config = getDefaultConfig()
    const members = store.listGroupMembers(this.groupId)

    // 构建成员列表
    const memberList = members.map(m => ({
      id: m.agent_id,
      name: m.agent_name,
      role: m.agent_role,
    }))

    // 构建对话上下文摘要
    const conversation = this.memory.slice(-8).join('\n')

    // 构建当前调度状态
    const dispatchState = this.dispatchPlan.length > 0
      ? this.dispatchPlan.map(s => {
        let icon = '⏳'
        if (s.status === 'completed') icon = '✅'
        if (s.status === 'failed') icon = '❌'
        if (s.status === 'dispatched') icon = '🔄'
        return `${icon} 步骤${s.step}: ${s.agent_name} ${icon}`
      }).join('\n')
      : ''

    const decision = await coordinatorBrainDecide(config, {
      name: this.name,
      members: memberList,
      conversation,
      dispatchState,
      sender,
      message: content,
    })

    this.memory.push({ role: 'user', content: `[${sender}] ${content}`, ts: new Date().toISOString() })

    // --- action: chat --- 直接回复，不涉及调度
    if (decision.action === 'chat') {
      await this._reply(decision.content)
      this.memory.push({ role: 'assistant', content: decision.content, ts: new Date().toISOString() })
      return
    }

    // --- action: ask --- 向用户提问
    if (decision.action === 'ask') {
      await this._reply(decision.content)
      return
    }

    // --- action: dispatch --- 生成调度计划并启动
    if (decision.action === 'dispatch' && decision.plan && decision.plan.length > 0) {
      this.dispatchPlan = decision.plan
      this.dispatchStep = 0

      // 在群里官宣调度计划
      const planSummary = decision.plan
        .map(s => `${s.step}. ${s.agent_name} → ${s.instruction.substring(0, 40)}...`)
        .join('\n')

      await this._reply(`📋 已制定协作计划，开始调度：\n${planSummary}`)

      // 立即派发第一步
      this._dispatchNextStep()
      return
    }

    // --- action: continue --- 收到成员汇报，继续下一步
    if (decision.action === 'continue') {
      // 更新当前步骤状态
      const currentStep = this.dispatchPlan.find(s => s.status === 'dispatched')
      if (currentStep) {
        currentStep.result = content
        currentStep.status = 'completed'
      }

      // 回复确认
      await this._reply(decision.content || '收到汇报，继续下一步。')

      // 继续派发后续步骤
      this._dispatchNextStep()
    }
  }

  /** 派发下一个待执行的步骤 */
  private _dispatchNextStep(): void {
    if (!this.dispatchPlan.length) return

    // 找到第一个 pending 且依赖已完成的步骤
    const next = this.dispatchPlan.find(s => {
      if (s.status !== 'pending') return false
      return s.depends_on.every(depStepNum => {
        const dep = this.dispatchPlan.find(d => d.step === depStepNum)
        return dep?.status === 'completed'
      })
    })

    if (!next) {
      // 所有步骤都完成了，汇总
      const allDone = this.dispatchPlan.every(s => s.status === 'completed' || s.status === 'failed')
      if (allDone) {
        const summary = this.dispatchPlan.map(s =>
          `${s.status === 'completed' ? '✅' : '❌'} ${s.agent_name}: ${s.result || s.instruction}`
        ).join('\n')
        setTimeout(() => {
          this._reply(`🎉 全部完成！协作结果汇总：\n${summary}`).catch(() => {})
        }, 1000)
        this.dispatchPlan = []
        this.dispatchStep = 0
      }
      return
    }

    // 标记为已派发，然后 @该成员
    next.status = 'dispatched'
    this.dispatchStep = next.step

    const mentionMsg = `@${next.agent_name} \n\n${next.instruction}\n\n完成后请 @我 汇报。`
    this._reply(mentionMsg).catch(err => console.error('[Coordinator] 派发步骤失败:', err))
  }

  // ── 辅助方法 ──────────────────────────────────────────────

  private async _reply(content: string): Promise<void> {
    const msg = store.createMessage({
      group_id: this.groupId,
      sender_id: this.id,
      receiver_id: 'broadcast',
      type: 'agent_reply',
      content,
    })

    eventBus.publish(`${CHANNEL_PREFIX}${this.groupId}`, {
      id: msg.id,
      group_id: this.groupId,
      sender_id: this.id,
      receiver_id: 'broadcast',
      type: 'agent_reply',
      content,
      timestamp: msg.created_at,
    })

    this._routeMentions(content)
  }

  private async _publishLog(taskId: string, line: string): Promise<void> {
    try {
      const payload = {
        id: crypto.randomUUID(),
        group_id: this.groupId,
        task_id: taskId || undefined,
        sender_id: this.id,
        receiver_id: 'broadcast',
        type: 'task_log',
        content: line,
        timestamp: new Date().toISOString(),
      }
      eventBus.publish(`${CHANNEL_PREFIX}${this.groupId}`, payload)
      store.createMessage({
        group_id: this.groupId,
        sender_id: this.id,
        receiver_id: 'broadcast',
        type: 'task_log',
        content: line,
        task_id: taskId || undefined,
      })
    } catch (err) {
      console.warn('发布日志失败:', err)
    }
  }

  private _buildContext(): string {
    const recent = this.memory.slice(-5)
    const lines = recent.map(m =>
      m.role === 'user' ? `用户: ${m.content}` : `${this.name}: ${m.content}`,
    )
    return lines.length ? lines.join('\n') : '（无历史对话）'
  }

  private _routeMentions(content: string): void {
    const mentions = content.match(/@(\S+)/g)
    if (!mentions) return

    const now = Date.now() / 1000
    this.recentRoutes = Object.fromEntries(
      Object.entries(this.recentRoutes).filter(([, ts]) => now - ts < 30),
    )

    const members = store.listGroupMembers(this.groupId)
    const agents = store.listAgents()

    for (const mention of mentions) {
      const mentionName = mention.substring(1)
      if (mentionName === this.id || mentionName === this.name) continue

      let targetId: string | undefined

      const member = members.find(m => m.agent_id === mentionName)
      if (member) targetId = member.agent_id

      if (!targetId) {
        const agent = agents.find(a => a.name === mentionName)
        if (agent && members.some(m => m.agent_id === agent.id)) {
          targetId = agent.id
        }
      }

      if (!targetId) {
        for (const m of members) {
          if (m.alias && mentionName.includes(m.alias)) {
            targetId = m.agent_id
            break
          }
        }
      }

      if (!targetId || targetId === this.id) continue
      if (this.recentRoutes[targetId]) {
        console.log(`防循环: 跳过重复路由 ${targetId}`)
        continue
      }
      this.recentRoutes[targetId] = now

      // A2A 解耦：通过 SharedStateCenter 扔字条，不再直接调用 agentRegistry.routeMessage
      sharedState.pushTask({
        group_id: this.groupId,
        sender_id: this.id,
        receiver_id: targetId,
        content,
      })
    }
  }

  private _resetIdle(): void {
    this.runtime = null
    this.currentTaskId = null
    this.status = 'idle'
  }
}
