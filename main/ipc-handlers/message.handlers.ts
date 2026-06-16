/**
 * Message IPC Handlers（A2A 架构改造后）
 *
 * 核心变化：
 * 1. 用户发消息时，通过 sharedState.pushNotify 写入目标 agent 的收件箱
 * 2. @mention 不再直接 agentRegistry.routeMessage，而是通过 sharedState 扔字条
 * 3. 没有 @mention 时，将消息写入 coordinator 引擎的收件箱，由 coordinator agent 自行消费
 * 4. 去掉直接调用 chatCompletion 的 coordinator 自动回复逻辑（由 coordinator engine 负责）
 */

import { ipcMain } from 'electron'
import { store } from '../store/store'
import { sharedState } from '../store/shared-state'
import { eventBus, CHANNEL_PREFIX } from '../bus/event-bus'
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
    // 1. 创建消息（存 store + 持久化）
    const msg = store.createMessage({
      group_id: data.group_id,
      task_id: data.task_id,
      sender_id: data.sender_id,
      receiver_id: data.receiver_id || 'broadcast',
      type: data.type || 'user_input',
      content: data.content,
      data: data.data,
    })

    // 2. 发布到事件总线（推给前端 UI）
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

    // 3. 如果发送者是 user，通过 sharedState 扔字条给对应 agent
    if (data.sender_id === 'user') {
      triggerA2ARouting(data.group_id, data.content || '').catch(err => {
        console.warn('A2A routing error:', err)
      })
    }

    return msg
  })

  ipcMain.handle(MESSAGE_CLEAR_BY_GROUP, (_event, groupId: string) => {
    store.clearMessagesByGroup(groupId)
  })
}

/**
 * A2A 消息路由
 * - @mention → 扔字条到被 @ agent 的收件箱
 * - 否则 → 扔字条到 coordinator 的收件箱（由 coordinator engine 消费）
 */
async function triggerA2ARouting(groupId: string, content: string): Promise<void> {
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
        // A2A：扔字条到 sharedState，而非直接 routeMessage
        sharedState.pushNotify({
          group_id: groupId,
          type: 'agent_reply',
          sender_id: 'user',
          receiver_id: targetId,
          content,
        })
        return // 路由到被 @ 的 agent，不再走 coordinator
      }
    }
  }

  // 没有 @mention：扔字条到 coordinator 的收件箱
  const group = store.getGroup(groupId)
  if (!group || !group.coordinator_id) return

  sharedState.pushNotify({
    group_id: groupId,
    type: 'coordinator_reply',
    sender_id: 'user',
    receiver_id: group.coordinator_id,
    content,
  })
}
