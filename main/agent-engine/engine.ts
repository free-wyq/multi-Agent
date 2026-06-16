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
import { getDefaultConfig } from '../coordinator/llm'
import { ClaudeCodeRuntime } from '../runtime/claude-code-runtime'
import { coordinatorWorkflow } from '../coordinator/workflow'
import type {
  AgentDefinition, BrainDecision, TaskQueueItem, NotifyQueueItem,
} from '../store/types'

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
      // 生成一个任务投递给自己（走 SharedStateCenter）
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
