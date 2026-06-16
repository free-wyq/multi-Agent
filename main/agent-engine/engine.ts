/**
 * Agent Engine
 *
 * 替代 Python AgentEngine：
 * - 每个智能体一个实例，常驻运行
 * - 内部队列（数组 push/shift）替代 asyncio.Queue
 * - 主循环 setInterval 检查队列
 * - 大脑决策（chat/execute/ask）
 * - 执行时 spawn Claude Code CLI 进程
 */

import { store } from '../store/store'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import { brainDecide } from './brain'
import { getDefaultConfig } from '../coordinator/llm'
import { ClaudeCodeRuntime } from '../runtime/claude-code-runtime'
import type { AgentDefinition, BrainDecision } from '../store/types'

export class AgentEngine {
  id: string
  name: string
  role: string
  systemPrompt: string
  groupId: string

  status: 'idle' | 'thinking' | 'executing' | 'offline' = 'idle'
  currentTaskId: string | null = null

  private inbox: Record<string, unknown>[] = []
  private intervalHandle: NodeJS.Timeout | null = null
  private shutdown = false
  private memory: { role: string; content: string; ts: string }[] = []
  private runtime: ClaudeCodeRuntime | null = null
  private recentRoutes: Record<string, number> = {}

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

    // 主循环：每 100ms 检查队列
    this.intervalHandle = setInterval(() => {
      this._processNextMessage()
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
    this.inbox.push(message)
  }

  // ── 主循环 ────────────────────────────────────────────────

  private _processNextMessage(): void {
    if (this.shutdown || !this.inbox.length) return
    if (this.status === 'executing') return // 正在执行，不处理新消息

    const msg = this.inbox.shift()
    if (!msg) return

    // 异步处理
    const type = msg.type as string
    if (type === 'task_dispatch') {
      this._doExecute(msg).catch(err => console.error(`AgentEngine execute error:`, err))
    } else {
      this._handleMessage(msg).catch(err => console.error(`AgentEngine handleMessage error:`, err))
    }
  }

  // ── 消息处理 ──────────────────────────────────────────────

  private async _handleMessage(msg: Record<string, unknown>): Promise<void> {
    this.status = 'thinking'
    const content = (msg.content as string) || ''
    const sender = (msg.sender_id as string) || 'user'

    const context = this._buildContext()
    const displayMsg = sender !== 'user' && sender !== 'coordinator'
      ? `[来自智能体 ${sender}] ${content}`
      : content

    const config = getDefaultConfig()
    const decision = await brainDecide(config, this.role, this.name, context, displayMsg)

    // 记忆
    this.memory.push({ role: 'user', content, ts: new Date().toISOString() })

    if (decision.action === 'chat') {
      await this._reply(decision.content, msg)
      this.memory.push({ role: 'assistant', content: decision.content, ts: new Date().toISOString() })
    } else if (decision.action === 'execute') {
      await this._reply(`收到，我来 ${decision.content.substring(0, 30)}...`, msg)
      await this._doExecute({
        type: 'task_dispatch',
        task_id: `task-${crypto.randomUUID().substring(0, 8)}`,
        content: decision.content,
        sender_id: sender,
      })
    } else if (decision.action === 'ask') {
      await this._reply(decision.content, msg)
    } else {
      await this._reply(decision.content, msg)
    }

    this.status = 'idle'
  }

  // ── 执行能力 ──────────────────────────────────────────────

  private async _doExecute(msg: Record<string, unknown>): Promise<void> {
    this.status = 'executing'
    const taskId = (msg.task_id as string) || `task-${crypto.randomUUID().substring(0, 8)}`
    this.currentTaskId = taskId
    const taskContent = (msg.content as string) || ''

    await this._publishLog(taskId, `▶ [${this.name}] 开始执行任务...`)

    // 获取智能体定义
    const agentDef = store.getAgent(this.id)
    if (!agentDef) {
      await this._publishLog(taskId, '❌ 找不到智能体定义')
      this.status = 'idle'
      this.currentTaskId = null
      return
    }

    // 使用 Claude Code Runtime 执行
    this.runtime = new ClaudeCodeRuntime(this.groupId, agentDef)
    try {
      await this._publishLog(taskId, `🚀 启动 Claude Code CLI...`)

      const result = await this.runtime.execute(taskContent, taskId)

      if (result.success) {
        await this._publishLog(taskId, `✅ 任务完成`)
        await this._reply(`任务完成 🎉\n${result.output?.substring(0, 200) || '已完成'}`, msg, taskId)
      } else {
        await this._publishLog(taskId, `❌ 执行失败`)
        await this._reply(`执行出错了: ${result.output || '未知错误'}`, msg, taskId)
      }
    } catch (err) {
      await this._publishLog(taskId, `❌ 执行异常: ${err}`)
      await this._reply(`执行出错了: ${err}`, msg, taskId)
    } finally {
      this.runtime = null
      this.currentTaskId = null
      this.status = 'idle'
    }
  }

  // ── 辅助方法 ──────────────────────────────────────────────

  private async _reply(
    content: string,
    parentMsg?: Record<string, unknown>,
    taskId?: string,
  ): Promise<void> {
    await this._saveAndPublish(content, 'agent_reply', taskId, parentMsg)
  }

  private async _publishLog(taskId: string, line: string): Promise<void> {
    try {
      const channel = `${CHANNEL_PREFIX}${this.groupId}`
      eventBus.publish(channel, {
        id: crypto.randomUUID(),
        group_id: this.groupId,
        task_id: taskId,
        sender_id: this.id,
        receiver_id: 'broadcast',
        type: 'task_log',
        content: line,
        timestamp: new Date().toISOString(),
      })
    } catch (err) {
      console.warn('发布日志失败:', err)
    }
  }

  private async _saveAndPublish(
    content: string,
    msgType: string,
    taskId?: string,
    _parentMsg?: Record<string, unknown>,
  ): Promise<void> {
    // 存到 store
    try {
      store.createMessage({
        group_id: this.groupId,
        sender_id: this.id,
        receiver_id: 'broadcast',
        type: msgType,
        content,
        task_id: taskId,
      })
    } catch (err) {
      console.warn('保存消息失败:', err)
    }

    // 发布到事件总线
    try {
      const channel = `${CHANNEL_PREFIX}${this.groupId}`
      eventBus.publish(channel, {
        id: crypto.randomUUID(),
        group_id: this.groupId,
        task_id: taskId,
        sender_id: this.id,
        receiver_id: 'broadcast',
        type: msgType,
        content,
        timestamp: new Date().toISOString(),
      })
    } catch (err) {
      console.warn('发布消息失败:', err)
    }

    // 检查 @mention 并路由
    this._routeMentions(content)
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

    // 防循环
    const now = Date.now() / 1000
    this.recentRoutes = Object.fromEntries(
      Object.entries(this.recentRoutes).filter(([, ts]) => now - ts < 30),
    )

    // 查找目标智能体
    const members = store.listGroupMembers(this.groupId)
    const agents = store.listAgents()

    for (const mention of mentions) {
      const mentionName = mention.substring(1) // 去掉 @

      // 不路由给自己
      if (mentionName === this.id || mentionName === this.name) continue

      // 查找匹配的智能体
      let targetId: string | undefined

      // 按 agent_id 精确匹配
      const member = members.find(m => m.agent_id === mentionName)
      if (member) {
        targetId = member.agent_id
      }

      // 按 name 匹配
      if (!targetId) {
        const agent = agents.find(a => a.name === mentionName)
        if (agent) {
          // 确认该 agent 是群成员
          const isMember = members.some(m => m.agent_id === agent.id)
          if (isMember) targetId = agent.id
        }
      }

      // 按 alias 模糊匹配
      if (!targetId) {
        for (const m of members) {
          if (m.alias && mentionName.includes(m.alias)) {
            targetId = m.agent_id
            break
          }
        }
      }

      if (!targetId || targetId === this.id) continue

      // 防循环
      if (this.recentRoutes[targetId]) {
        console.log(`防循环: 跳过重复路由 ${targetId}`)
        continue
      }
      this.recentRoutes[targetId] = now

      // 路由消息
      const { agentRegistry } = require('./registry') as { agentRegistry: AgentRegistry }
      agentRegistry.routeMessage(targetId, {
        type: 'chat',
        content,
        sender_id: this.id,
        group_id: this.groupId,
      }, this.groupId)
    }
  }
}

// 前置声明（避免循环依赖）
import type { AgentRegistry } from './registry'
