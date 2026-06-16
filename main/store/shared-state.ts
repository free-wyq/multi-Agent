/**
 * SharedStateCenter — 显式共享状态中心
 *
 * 替代点对点路由，实现 A2A "扔字条"式解耦通信：
 * - TaskQueue: 任何 agent 都可以向某个 agent 的收件箱「扔任务字条」
 * - NotifyQueue: 任务完成/状态变更等通知广播
 * - 接收者主动轮询（pollInbox）获取自己的邮件
 * - 完全解决耦：发布者不知道接收者是否存在、何时消费
 */

import { v4 as uuid } from 'uuid'
import { persistence } from './persistence'

export type TaskQueueStatus = 'pending' | 'claimed' | 'completed' | 'failed'

export interface TaskQueueItem {
  id: string
  group_id: string
  sender_id: string
  receiver_id: string     // 目标 agent id
  content: string
  data?: Record<string, unknown>
  created_at: string
  status: TaskQueueStatus
  claimed_by?: string     // instance_id
  result?: string
  result_data?: Record<string, unknown>
  completed_at?: string
}

export type NotifyType = 'task_complete' | 'task_failed' | 'agent_reply' | 'task_log' | 'coordinator_reply'

export interface NotifyQueueItem {
  id: string
  group_id: string
  type: NotifyType
  sender_id: string
  receiver_id: string     // 'broadcast' 或具体 agent id
  content: string
  data?: Record<string, unknown>
  created_at: string
}

/** 一个群组的队列快照（用于持久化和恢复） */
export interface GroupQueueSnapshot {
  group_id: string
  tasks: TaskQueueItem[]
  notifies: NotifyQueueItem[]
}

class SharedStateCenter {
  /** groupId -> TaskQueueItem[] */
  private taskQueues = new Map<string, TaskQueueItem[]>()

  /** groupId -> NotifyQueueItem[] */
  private notifyQueues = new Map<string, NotifyQueueItem[]>()

  /** 每个 agent 上次轮询时间（用于增量拉取） */
  private lastPollMap = new Map<string, string>()

  // ── 任务队列 ──────────────────────────────────────────────

  /**
   * 向指定 agent 的收件箱「扔任务字条」
   * 发送者无需知道接收者是否在线
   */
  pushTask(params: {
    group_id: string
    sender_id: string
    receiver_id: string
    content: string
    data?: Record<string, unknown>
  }): TaskQueueItem {
    const item: TaskQueueItem = {
      id: uuid(),
      group_id: params.group_id,
      sender_id: params.sender_id,
      receiver_id: params.receiver_id,
      content: params.content,
      data: params.data,
      created_at: new Date().toISOString(),
      status: 'pending',
    }
    const q = this._getTaskQueue(params.group_id)
    q.push(item)
    this._schedulePersist(params.group_id)
    return item
  }

  /**
   * Agent 主动来取一个属于自己的 pending 任务
   * 返回后自动标记为 claimed
   */
  claimTask(groupId: string, agentId: string, instanceId: string): TaskQueueItem | undefined {
    const q = this._getTaskQueue(groupId)
    const idx = q.findIndex(
      t => t.receiver_id === agentId && t.status === 'pending',
    )
    if (idx === -1) return undefined

    const item = q[idx]
    item.status = 'claimed'
    item.claimed_by = instanceId
    q[idx] = item
    this._schedulePersist(groupId)
    return item
  }

  /**
   * Agent 完成任务后写回结果
   * 同时自动在 NotifyQueue 发布 task_complete / task_failed
   */
  completeTask(
    taskId: string,
    success: boolean,
    result?: string,
    resultData?: Record<string, unknown>,
  ): TaskQueueItem | undefined {
    for (const [groupId, q] of this.taskQueues) {
      const item = q.find(t => t.id === taskId)
      if (item) {
        item.status = success ? 'completed' : 'failed'
        item.result = result
        item.result_data = resultData
        item.completed_at = new Date().toISOString()
        this._schedulePersist(groupId)

        // 自动发布通知给 sender
        this.pushNotify({
          group_id: item.group_id,
          type: success ? 'task_complete' : 'task_failed',
          sender_id: item.receiver_id,
          receiver_id: item.sender_id,
          content: result || (success ? '任务完成' : '任务失败'),
          data: { task_id: taskId, ...resultData },
        })
        return item
      }
    }
    return undefined
  }

  /**
   * 查询某个群的所有任务（用于 coordinator monitor）
   */
  listTasks(groupId: string): TaskQueueItem[] {
    return [...this._getTaskQueue(groupId)]
  }

  /**
   * 查询某个 agent 的 pending 任务数量
   */
  countPending(groupId: string, agentId: string): number {
    return this._getTaskQueue(groupId).filter(
      t => t.receiver_id === agentId && t.status === 'pending',
    ).length
  }

  // ── 通知队列 ──────────────────────────────────────────────

  /**
   * 向共享通知队列投递通知
   * receiver_id = 'broadcast' 时会被所有轮询者收到
   */
  pushNotify(params: {
    group_id: string
    type: NotifyType
    sender_id: string
    receiver_id: string
    content: string
    data?: Record<string, unknown>
  }): NotifyQueueItem {
    const item: NotifyQueueItem = {
      id: uuid(),
      group_id: params.group_id,
      type: params.type,
      sender_id: params.sender_id,
      receiver_id: params.receiver_id,
      content: params.content,
      data: params.data,
      created_at: new Date().toISOString(),
    }
    const q = this._getNotifyQueue(params.group_id)
    q.push(item)

    // 通知队列过长时截断（保留最近 500 条）
    if (q.length > 500) {
      q.splice(0, q.length - 500)
    }

    this._schedulePersist(params.group_id)
    return item
  }

  // ── 收件箱轮询（核心解耦 API）──────────────────────────────

  /**
   * 轮询某个 agent 的收件箱：返回属于它的 pending 任务 + 定向/广播通知
   *
   * 这是 AgentEngine 主循环里替代 `inbox.shift()` 的核心方法。
   */
  pollInbox(
    groupId: string,
    agentId: string,
    options?: {
      since?: string       // ISO timestamp，只返回大于此时间的
      taskOnly?: boolean   // 只取任务（不取通知）
    },
  ): { tasks: TaskQueueItem[]; notifies: NotifyQueueItem[] } {
    const since = options?.since
    const key = `${groupId}:${agentId}`

    // 属于我的 pending 任务
    const tasks = this._getTaskQueue(groupId).filter(
      t => t.receiver_id === agentId && t.status === 'pending',
    )

    // 筛选通知：定向给我 或 broadcast，且时间 > since
    const allNotifies = this._getNotifyQueue(groupId)
    const notifies = allNotifies.filter(n => {
      if (n.receiver_id !== 'broadcast' && n.receiver_id !== agentId) return false
      if (!since) return true
      return n.created_at > since
    })

    // 更新时间戳
    this.lastPollMap.set(key, new Date().toISOString())

    return { tasks, notifies }
  }

  /**
   * 一次性拉取 inbox 中最老的一条，用于 AgentEngine 逐个消费
   * 优先返回 tasks，没有 tasks 再返回 notifies
   */
  pollOne(groupId: string, agentId: string):
    | { kind: 'task'; item: TaskQueueItem }
    | { kind: 'notify'; item: NotifyQueueItem }
    | undefined {
    const { tasks, notifies } = this.pollInbox(groupId, agentId)

    if (tasks.length) {
      // 返回最老的一条任务
      const oldest = tasks.sort((a, b) => a.created_at.localeCompare(b.created_at))[0]
      return { kind: 'task', item: oldest }
    }

    if (notifies.length) {
      // 返回最老的一条通知
      const oldest = notifies.sort((a, b) => a.created_at.localeCompare(b.created_at))[0]
      return { kind: 'notify', item: oldest }
    }

    return undefined
  }

  // ── 持久化 ────────────────────────────────────────────────

  /**
   * 启动时从 JSON 恢复所有队列
   */
  async loadAll(): Promise<void> {
    const snapshots = await persistence.loadAllQueues()
    for (const snap of snapshots) {
      this.taskQueues.set(snap.group_id, snap.tasks || [])
      this.notifyQueues.set(snap.group_id, snap.notifies || [])
    }
  }

  private _schedulePersist(groupId: string): void {
    const snap: GroupQueueSnapshot = {
      group_id: groupId,
      tasks: this._getTaskQueue(groupId),
      notifies: this._getNotifyQueue(groupId),
    }
    persistence.scheduleSaveQueue(groupId, snap)
  }

  // ── 内部辅助 ──────────────────────────────────────────────

  private _getTaskQueue(groupId: string): TaskQueueItem[] {
    if (!this.taskQueues.has(groupId)) {
      this.taskQueues.set(groupId, [])
    }
    return this.taskQueues.get(groupId)!
  }

  private _getNotifyQueue(groupId: string): NotifyQueueItem[] {
    if (!this.notifyQueues.has(groupId)) {
      this.notifyQueues.set(groupId, [])
    }
    return this.notifyQueues.get(groupId)!
  }
}

export const sharedState = new SharedStateCenter()
