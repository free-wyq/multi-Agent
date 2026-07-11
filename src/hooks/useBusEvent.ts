import { useCallback, useContext, useEffect, useState } from 'react'
import {
  messageApi,
  onBusEvent,
  planApi,
  systemApi,
  type BusEventData,
  type Message,
  type TraceEvent,
  type AgentStatusInfo,
  type PlanStep,
} from '../services/api'
import { BusEventContext } from '../contexts/BusEventContext'

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
 * - streaming: task_token 增量按 task_id 拼接的「正在生成」缓冲区（PL-08 逐字流式）
 *
 * PL-10 重连重拉：onBusEvent 第三参 onReconnect 回调，连接曾中断重连后触发，
 * 按消息/计划/状态真源重新拉取——断线期间漏掉的事件（agent_status、coordinator_plan、
 * task_complete 等）上层状态已过期，必须重拉补齐。events（TraceEvent cap 500）不重拉
 * （无历史接口，属前端短时缓存，断线期间丢失的中间 trace 不影响任务推进，刷新即可）。
 *
 * WS-02 优先消费 BusEventContext：若上方有同 groupId 的 provider，直接复用其共享状态
 * （全应用一条 WS），不重复订阅。命中规则——groupId 相同且非 null。未命中（无 provider /
 * groupId 不同 / groupId 为 null）则走原自起 WS 分支，**原签名与返回结构零回归**：
 * 独立调用（无 provider 包裹的组件、或自起 WS 的 provider 自身）行为完全不变。provider
 * 自身调本 hook 时上方 context 为 null（provider 是树根），天然走自起分支，无递归。
 */
export function useBusEvent(groupId: string | null) {
  const ctx = useContext(BusEventContext)

  // WS-02 命中复用：上方 provider 绑定同一非空 groupId → 全应用共享一条 WS，零重复订阅。
  // 只在 groupId 完全相同且非 null 时复用 —— 不同群组各有自己的事件流，不能错用。
  if (ctx && ctx.groupId !== null && ctx.groupId === groupId) {
    return {
      logs: ctx.logs,
      statusEvents: ctx.statusEvents,
      events: ctx.events,
      agentStatuses: ctx.agentStatuses,
      plan: ctx.plan,
      streaming: ctx.streaming,
    }
  }

  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [agentStatuses, setAgentStatuses] = useState<Record<string, AgentStatusInfo>>({})
  const [plan, setPlan] = useState<PlanStep[] | null>(null)
  const [streaming, setStreaming] = useState<Record<string, string>>({})

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

  // PL-10 重连后重拉历史：连接曾中断 → 上层消息/计划/状态可能过期，从真源补齐。
  // - messages: 重拉后重建 logs（供 GroupPage 聊天列表复原，按 id 去重保留）
  // - plan: 重读 coordinator 引擎 _dispatch_plan（无变更则保持，有 pending 则卡片复现）
  // - agentStatuses: 重读 /api/status 重新播种（断线期间状态可能已 idle→executing→idle）
  // useCallback 使引用稳定，仅在 groupId 变化时换实例，避免 WS effect 重订阅。
  const handleReconnect = useCallback(() => {
    if (!groupId) return

    // 重拉 agent 状态（断线期间 agent_status 事件可能已多次迁移）
    systemApi
      .listStatus(groupId)
      .then((list) => {
        const m: Record<string, AgentStatusInfo> = {}
        list.forEach((s) => { m[s.id] = s })
        setAgentStatuses(m)
      })
      .catch(() => { /* 静默 */ })

    // 重拉驻留计划（coordinator_plan 事件可能漏收）
    planApi
      .getPlan(groupId)
      .then((resp) => {
        setPlan(resp.plan && resp.plan.length > 0 ? resp.plan : null)
      })
      .catch(() => { /* 静默 */ })

    // 重拉消息历史：把全量历史重新灌入 logs（供 GroupPage 聊天列表复原）。
    // 按 created_at 升序返回的最近 limit 条，重建 LogEntry（id 去重保留）。
    messageApi
      .listByGroup(groupId)
      .then((msgs: Message[]) => {
        const rebuilt: LogEntry[] = msgs
          .filter((m) => m.content)
          .map((m) => ({
            id: m.id,
            agentId: m.sender_id,
            agentName: m.sender_id,
            taskId: m.task_id || '',
            message: m.content || '',
            timestamp: parseTs(m.created_at),
          }))
        setLogs(rebuilt.slice(-200))
      })
      .catch(() => { /* 静默 */ })
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

      // → streaming token 增量拼接（PL-08 逐字流式）
      // 每个 task_token 事件按 task_id 累加 content delta；
      // task_complete / task_failed / task_dispatch 收尾时清空对应 task 的缓冲，
      // 避免「上一轮生成内容」残留到新一轮。未带 task_id 的 token 丢弃（无法归并）。
      if (d.type === 'task_token') {
        if (d.content && d.task_id) {
          setStreaming((prev) => ({
            ...prev,
            [d.task_id as string]: (prev[d.task_id as string] || '') + d.content,
          }))
        }
      } else if (
        d.type === 'task_complete' ||
        d.type === 'task_failed' ||
        d.type === 'task_dispatch'
      ) {
        if (d.task_id) {
          const tid = d.task_id as string
          setStreaming((prev) => {
            if (!(tid in prev)) return prev
            const next = { ...prev }
            delete next[tid]
            return next
          })
        }
      }
    }, handleReconnect).then((fn) => {
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
  }, [groupId, handleReconnect])

  return { logs, statusEvents, events, agentStatuses, plan, streaming }
}
