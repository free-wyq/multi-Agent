import { useEffect, useState } from 'react'

export interface LogEntry {
  id: string
  agentId: string
  agentName: string
  taskId: string
  message: string
  timestamp: number
}

export interface TaskStatusEvent {
  taskId: string
  status: string
  groupId: string
  agentId?: string
  updatedAt: string
}

/** Bus 消息的 data 字段结构 */
interface BusMessageData {
  id: string
  group_id: string
  task_id: string | null
  sender_id: string
  receiver_id: string
  type: string
  content: string | null
  data: unknown
  timestamp: string
}

/**
 * 实时事件 hook：通过 Electron IPC 接收消息总线事件
 *
 * 替代原 WebSocket 连接：
 * - onBusEvent 监听主进程转发的总线消息
 * - 返回值类型 LogEntry[] / TaskStatusEvent[] 保持不变
 */
export function useWebSocket(groupId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])

  useEffect(() => {
    if (!groupId) return

    const cleanup = window.electronAPI.onBusEvent(groupId, (data: unknown) => {
      const d = data as BusMessageData

      // 转换为 LogEntry
      if (d.content) {
        const entry: LogEntry = {
          id: d.id || `ipc-${Date.now()}`,
          agentId: d.sender_id,
          agentName: d.sender_id,
          taskId: d.task_id || '',
          message: d.content,
          timestamp: new Date(d.timestamp).getTime(),
        }
        setLogs((prev) => [...prev.slice(-200), entry])
      }

      // 转换为 TaskStatusEvent
      if (d.type === 'task_complete' || d.type === 'task_failed' || d.type === 'task_dispatch') {
        const evt: TaskStatusEvent = {
          taskId: d.task_id || '',
          status:
            d.type === 'task_complete'
              ? 'completed'
              : d.type === 'task_failed'
                ? 'failed'
                : 'working',
          groupId: d.group_id,
          agentId: d.sender_id,
          updatedAt: d.timestamp,
        }
        setStatusEvents((prev) => [...prev.slice(-50), evt])
      }
    })

    return cleanup
  }, [groupId])

  return { logs, statusEvents }
}

/**
 * 向后兼容
 * @deprecated 请使用 useWebSocket(groupId)
 */
export function useMockWebSocket(enabled: boolean, groupId?: string | null) {
  return useWebSocket(enabled ? (groupId ?? null) : null)
}
