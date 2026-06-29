import { useEffect, useState } from 'react'
import { onBusEvent, type BusEventData } from '../services/api'

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

/**
 * 实时事件 hook：通过 Tauri events 接收消息总线事件
 *
 * - onBusEvent 监听主进程 app.emit 的总线消息
 * - 返回值类型 LogEntry[] / TaskStatusEvent[]
 */
export function useBusEvent(groupId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])

  useEffect(() => {
    if (!groupId) return

    let unlisten: (() => void) | null = null
    let cancelled = false

    onBusEvent(groupId, (d: BusEventData) => {
      if (cancelled) return

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
    }).then((fn) => {
      if (cancelled) {
        fn()
      } else {
        unlisten = fn
      }
    })

    return () => {
      cancelled = true
      if (unlisten) unlisten()
    }
  }, [groupId])

  return { logs, statusEvents }
}
