import { useEffect, useState } from 'react'
import {
  onBusEvent,
  systemApi,
  type BusEventData,
  type TraceEvent,
  type AgentStatusInfo,
  type PlanStep,
} from '../services/api'

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

/** Map BusEventData.type → TraceEvent.kind */
function mapKind(type: string): string {
  switch (type) {
    case 'task_tool': return 'tool'
    case 'task_think': return 'think'
    case 'task_log': return 'log'
    case 'task_dispatch': return 'dispatch'
    case 'task_complete': return 'complete'
    case 'task_failed': return 'failed'
    case 'agent_status': return 'status'
    case 'coordinator_plan': return 'plan'
    case 'coordinator_think': return 'coord_think'
    case 'agent_reply':
    case 'user_input': return 'reply'
    default: return type
  }
}

/** Parse ISO timestamp → epoch ms (fallback Date.now) */
function parseTs(ts: string): number {
  const n = new Date(ts).getTime()
  return Number.isNaN(n) ? Date.now() : n
}

/**
 * 实时事件 hook：通过 WebSocket 接收消息总线事件
 *
 * - logs: 保留 content truthy 事件（GroupPage 群聊依赖，cap 200）
 * - events: 结构化 TraceEvent[]（cap 500），供监控页渲染
 * - agentStatuses: 从 systemApi.listStatus 播种 + agent_status 事件实时更新
 * - plan: coordinator_plan 事件 → PlanStep[]
 */
export function useBusEvent(groupId: string | null) {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [agentStatuses, setAgentStatuses] = useState<Record<string, AgentStatusInfo>>({})
  const [plan, setPlan] = useState<PlanStep[] | null>(null)

  // 播种 agent status
  useEffect(() => {
    if (!groupId) {
      setAgentStatuses({})
      return
    }
    let cancelled = false
    systemApi
      .listStatus(groupId)
      .then((list) => {
        if (cancelled) return
        const m: Record<string, AgentStatusInfo> = {}
        list.forEach((s) => { m[s.id] = s })
        setAgentStatuses(m)
      })
      .catch(() => {
        /* 后端未启动时静默，WS 会逐步补齐 */
      })
    return () => { cancelled = true }
  }, [groupId])

  useEffect(() => {
    if (!groupId) return

    let unlisten: (() => void) | null = null
    let cancelled = false

    onBusEvent(groupId, (d: BusEventData) => {
      if (cancelled) return

      const ts = parseTs(d.timestamp)

      // → TraceEvent
      const ev: TraceEvent = {
        id: d.id || `evt-${ts}`,
        kind: mapKind(d.type),
        agentId: d.sender_id,
        agentName:
          (d.data && typeof d.data === 'object' && 'agent_name' in (d.data as Record<string, unknown>))
            ? String((d.data as Record<string, unknown>).agent_name)
            : d.sender_id,
        taskId: d.task_id,
        content: d.content,
        data: d.data,
        timestamp: ts,
      }
      setEvents((prev) => [...prev.slice(-499), ev])

      // → logs (GroupPage 依赖 content truthy)
      if (d.content) {
        const entry: LogEntry = {
          id: d.id || `ipc-${ts}`,
          agentId: d.sender_id,
          agentName: d.sender_id,
          taskId: d.task_id || '',
          message: d.content,
          timestamp: ts,
        }
        setLogs((prev) => [...prev.slice(-200), entry])
      }

      // → TaskStatusEvent (旧契约保留)
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

      // → agentStatuses 实时更新
      if (d.type === 'agent_status' && d.data && typeof d.data === 'object') {
        const dd = d.data as Record<string, unknown>
        const status = String(dd['status'] || 'idle') as AgentStatusInfo['status']
        const name = String(dd['agent_name'] || d.sender_id)
        const currentTaskId = (dd['current_task_id'] as string | null) ?? null
        setAgentStatuses((prev) => ({
          ...prev,
          [d.sender_id]: {
            id: d.sender_id,
            name,
            role: prev[d.sender_id]?.role || '',
            status,
            current_task_id: currentTaskId,
          },
        }))
      }

      // → plan
      if (d.type === 'coordinator_plan' && d.data && typeof d.data === 'object') {
        const planData = (d.data as Record<string, unknown>)['plan']
        if (Array.isArray(planData)) {
          setPlan(planData as unknown as PlanStep[])
        }
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

  return { logs, statusEvents, events, agentStatuses, plan }
}
