import { useEffect, useRef, useState, useCallback } from 'react'

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
  data: any
  timestamp: string
}

/** WebSocket 协议消息 */
interface WSMessage {
  type: string
  data: BusMessageData
}

const WS_BASE = import.meta.env.VITE_WS_URL || 'ws://localhost:8000'

/**
 * WebSocket hook：连接 ws://host:port/ws/{groupId}，接收实时消息总线事件
 *
 * 替代原 useMockWebSocket（setInterval 假数据），改为真实 WebSocket 连接。
 * 支持自动重连（3 秒延迟）。
 */
export function useWebSocket(groupId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const connect = useCallback(() => {
    if (!groupId) return

    const url = `${WS_BASE}/ws/${groupId}`
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      console.log('[WS] Connected to', url)
    }

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data)
        const d = msg.data

        // 转换为 LogEntry
        if (d.content) {
          const entry: LogEntry = {
            id: d.id || `ws-${Date.now()}`,
            agentId: d.sender_id,
            agentName: d.sender_id, // 前端可后续映射为可读名称
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
      } catch (err) {
        console.error('[WS] Parse error:', err)
      }
    }

    ws.onclose = () => {
      console.log('[WS] Disconnected, reconnecting in 3s...')
      reconnectTimerRef.current = setTimeout(connect, 3000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [groupId])

  useEffect(() => {
    connect()
    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [connect])

  return { logs, statusEvents }
}

/**
 * 向后兼容：保留 useMockWebSocket 名称，内部调用真实 WebSocket
 * @deprecated 请使用 useWebSocket(groupId)
 */
export function useMockWebSocket(enabled: boolean, groupId?: string | null) {
  return useWebSocket(enabled ? (groupId ?? null) : null)
}
