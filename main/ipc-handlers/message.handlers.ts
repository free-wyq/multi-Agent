/**
 * Message IPC Handlers
 *
 * 替代 FastAPI messages 路由：
 * - 创建消息 + 事件总线发布
 * - 如果发送者是 user，触发自动回复（@mention 路由 / coordinator 回复）
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
import { agentRegistry } from '../agent-engine/registry'
import { chatCompletion, getDefaultConfig } from '../coordinator/llm'
import { buildCoordinatorReplyPrompt } from '../coordinator/prompts'
import {
  MESSAGE_LIST_BY_GROUP, MESSAGE_LIST_BY_TASK, MESSAGE_SEND, MESSAGE_CLEAR_BY_GROUP,
} from '../../src/ipc/channels'

export function registerMessageHandlers(): void {
  ipcMain.handle(MESSAGE_LIST_BY_GROUP, (_event, groupId: string, limit?: number) => {
    return store.listMessagesByGroup(groupId, limit)
  })

  ipcMain.handle(MESSAGE_LIST_BY_TASK, (_event, taskId: string, limit?: number) => {
    return store.listMessagesByTask(taskId, limit)
  })

  ipcMain.handle(MESSAGE_SEND, (_event, data: {
    group_id: string
    task_id?: string
    sender_id: string
    receiver_id?: string
    type?: string
    content?: string
    data?: Record<string, unknown>
  }) => {
    // 1. 创建消息
    const msg = store.createMessage({
      group_id: data.group_id,
      task_id: data.task_id,
      sender_id: data.sender_id,
      receiver_id: data.receiver_id || 'broadcast',
      type: data.type || 'user_input',
      content: data.content,
      data: data.data,
    })

    // 2. 发布到事件总线
    const channel = `${CHANNEL_PREFIX}${data.group_id}`
    eventBus.publish(channel, {
      id: msg.id,
      group_id: msg.group_id,
      task_id: msg.task_id,
      sender_id: msg.sender_id,
      receiver_id: msg.receiver_id,
      type: msg.type,
      content: msg.content,
      timestamp: msg.created_at,
    })

    // 3. 如果发送者是 user，异步触发自动回复
    if (data.sender_id === 'user') {
      triggerAutoReply(data.group_id, data.content || '').catch(err => {
        console.warn('Auto-reply error:', err)
      })
    }

    return msg
  })

  ipcMain.handle(MESSAGE_CLEAR_BY_GROUP, (_event, groupId: string) => {
    store.clearMessagesByGroup(groupId)
  })
}

/**
 * 触发自动回复
 * - 检查 @mention → 路由到指定智能体
 * - 否则 → coordinator 自动回复
 */
async function triggerAutoReply(groupId: string, content: string): Promise<void> {
  // 1. 检查 @mention
  const mentions = content.match(/@(\S+)/g)
  if (mentions) {
    const members = store.listGroupMembers(groupId)
    const agents = store.listAgents()

    for (const mention of mentions) {
      const mentionName = mention.substring(1)
      let targetId: string | undefined

      // 按 name 匹配
      const agent = agents.find(a => a.name === mentionName)
      if (agent) {
        const isMember = members.some(m => m.agent_id === agent.id)
        if (isMember) targetId = agent.id
      }

      // 按 agent_id 匹配
      if (!targetId) {
        const member = members.find(m => m.agent_id === mentionName)
        if (member) targetId = member.agent_id
      }

      if (targetId) {
        agentRegistry.routeMessage(targetId, {
          type: 'chat',
          content,
          sender_id: 'user',
          group_id: groupId,
        }, groupId)
        return // 路由到被 @ 的智能体，不再走 coordinator
      }
    }
  }

  // 2. Coordinator 自动回复
  const group = store.getGroup(groupId)
  if (!group) return

  const coordinator = store.getAgent(group.coordinator_id)
  if (!coordinator) return

  const memberNames = store.listGroupMembers(groupId).map(m => m.agent_name)

  const config = getDefaultConfig()
  const reply = await chatCompletion(
    { ...config, temperature: 0.3 },
    [{ role: 'user', content: buildCoordinatorReplyPrompt(content, coordinator.name, memberNames) }],
  )

  // 保存 + 发布 coordinator 回复
  const msg = store.createMessage({
    group_id: groupId,
    sender_id: group.coordinator_id,
    receiver_id: 'broadcast',
    type: 'coordinator_reply',
    content: reply,
  })

  const channel = `${CHANNEL_PREFIX}${groupId}`
  eventBus.publish(channel, {
    id: msg.id,
    group_id: msg.group_id,
    sender_id: msg.sender_id,
    receiver_id: msg.receiver_id,
    type: msg.type,
    content: msg.content,
    timestamp: msg.created_at,
  })

  // 检查 coordinator 回复中的 @mention
  const replyMentions = reply.match(/@(\S+)/g)
  if (replyMentions) {
    const members = store.listGroupMembers(groupId)
    const agents = store.listAgents()

    for (const m of replyMentions) {
      const mentionName = m.substring(1)
      const agent = agents.find(a => a.name === mentionName)
      if (agent) {
        const isMember = members.some(mm => mm.agent_id === agent.id)
        if (isMember) {
          agentRegistry.routeMessage(agent.id, {
            type: 'chat',
            content: reply,
            sender_id: group.coordinator_id,
            group_id: groupId,
          }, groupId)
        }
      }
    }
  }
}
