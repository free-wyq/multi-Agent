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
  /** 原始 BusEventData.type（agent_reply/user_input/task_log/coordinator_think/...）。
   *  ChatPanel 据此过滤：只把 agent_reply 桥接成聊天气泡，其余 trace 事件
   *  （coordinator_think/task_token/task_think/...）不进气泡——否则 coordinator_think
   *  携带的完整回复文本会被渲染成气泡，与随后 agent_reply 持久化消息重复（「回复两次」缺陷根因）。 */
  type: string
  /** 原始 BusEventData.data（透传）。
   *  协调者 chat 回复的 data 带 {reply_id, elapsed_ms, tokens} 流式统计——
   *  ChatPanel 桥接成 chatMessages 时保留 data，定稿气泡据此渲染「Ns · ↓ N tokens」
   *  状态行（流式统计在完成后保留可见，不随流式气泡退场消失）。 */
  data: unknown
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
    case 'task_token': return 'token'
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
      coordStreaming: ctx.coordStreaming,
      coordReasoning: ctx.coordReasoning,
      coordStats: ctx.coordStats,
      refreshPlan: ctx.refreshPlan,
    }
  }

  const [logs, setLogs] = useState<LogEntry[]>([])
  const [statusEvents, setStatusEvents] = useState<TaskStatusEvent[]>([])
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [agentStatuses, setAgentStatuses] = useState<Record<string, AgentStatusInfo>>({})
  const [plan, setPlan] = useState<PlanStep[] | null>(null)
  const [streaming, setStreaming] = useState<Record<string, string>>({})
  // 协调者流式回复（与 worker task_token 同构，但按 reply_id 而非 task_id 归并）：
  //  - coordStreaming[reply_id] = 累积的 content delta（逐字拼接，可见回复）
  //  - coordReasoning[reply_id] = 累积的 reasoning_content delta（模型内部推理链，
  //    DeepSeek/o1 类推理模型在可见 content 之前流出；非推理模型不流，map 不存在）
  //  - coordStats[reply_id] = 最新 { elapsed_ms, tokens, phase, model, reasoning_tokens }
  //    （~200ms 节流 + done 终态）
  // coordinator 的回复走独立 LLM 直调（非 create_react_agent），不经 worker task_token 通道。
  const [coordStreaming, setCoordStreaming] = useState<Record<string, string>>({})
  const [coordReasoning, setCoordReasoning] = useState<Record<string, string>>({})
  const [coordStats, setCoordStats] = useState<
    Record<string, { elapsed_ms: number; tokens: number; phase: string; model?: string; reasoning_tokens?: number }>
  >({})

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

  // refreshPlan: 主动从真源拉取驻留计划，对齐后端 _dispatch_plan。
  // 抽出于 handleReconnect + 切群首拉 + PlanConfirmCard 409 静默刷新三处复用。
  // plan 为空（已派发完/summarize/reset）时设 null，让 PlanConfirmCard 自动隐藏。
  const refreshPlan = useCallback(async () => {
    if (!groupId) return
    try {
      const resp = await planApi.getPlan(groupId)
      setPlan(resp.plan && resp.plan.length > 0 ? resp.plan : null)
    } catch {
      /* 静默 */
    }
  }, [groupId])

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
    void refreshPlan()

    // 重拉消息历史：把全量历史重新灌入 logs（供 GroupPage 聊天列表复原）。
    // 按 created_at 升序返回的最近 limit 条，重建 LogEntry（id 去重保留）。
    // 历史消息持久化时 type 就是真实类型（agent_reply/user_input/task_log），
    // 重建时带 type 供 ChatPanel 按类型过滤（见 LogEntry.type 注释）。
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
            type: m.type,
            data: m.data,
          }))
        setLogs(rebuilt.slice(-200))
      })
      .catch(() => { /* 静默 */ })
  }, [groupId])

  useEffect(() => {
    if (!groupId) return

    // 切群首拉：清旧群残留 plan，再主动从真源拉新群当前驻留计划。
    // 覆盖两个缺口：①引擎重启但 WS 未断连（onReconnect 不触发，真源已变但
    // plan state 停在旧群）；②切群首连不走 onReconnect（首次连接不算「重连」）。
    // 不影响后续 onBusEvent 订阅逻辑。
    setPlan(null)
    void refreshPlan()

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

      // → logs (GroupPage / LogPanel 依赖 content truthy)
      // 复制原始 type 到 LogEntry.type，供 ChatPanel 按类型过滤——只 agent_reply/
      // user_input/task_log 桥接成聊天气泡，coordinator_think 等思考事件不进气泡
      // （见 LogEntry.type 注释，避免与 agent_reply 持久化消息重复渲染）。
      // coordinator_token 例外：content 是逐字 delta（truthy），若进 logs 会把每个
      // token 都当一条日志灌进 LogPanel，且 coordinator_token 不在 CHAT_MESSAGE_TYPES
      // 白名单（本就不该成气泡）。从源头排除，避免流式 delta 污染日志流。
      // task_token 同理（PL-08 create_react_agent 逐字 token + task 25 worker 单聊
      // 流式 token）：content 是逐字 delta，进 logs 会把每个 token 当一条日志灌 LogPanel，
      // 且 task_token 不在白名单（本就不该成独立气泡）。从源头排除。
      if (d.content && d.type !== 'coordinator_token' && d.type !== 'task_token') {
        const entry: LogEntry = {
          id: d.id || `ipc-${ts}`,
          agentId: d.sender_id,
          agentName: d.sender_id,
          taskId: d.task_id || '',
          message: d.content,
          timestamp: ts,
          type: d.type,
          data: d.data,
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
      // 两条归并路径，按 task_id 是否「真任务 id」分流：
      //  1. 真 task_token（task_id 形如 `task_xxx`，PL-08 create_react_agent 执行路径）：
      //     按 task_id 累加进 streaming[task_id]，由 ChatPanel.streamingBubbles 渲染
      //     （executing agent 的 current_task_id 取对应缓冲）。task_complete/failed/dispatch
      //     收尾时清空对应 task 缓冲。
      //  2. worker 单聊 task_token（task 24，task_id 是 reply_id hex，无 `task_` 前缀）：
      //     单聊 worker 无 task_id，后端把 reply_id 塞进 task_id 槽位。归并进
      //     coordStreaming[reply_id]——复用协调者流式气泡渲染（coordinatorStreamingBubbles
      //     按 reply_id 取缓冲），单聊回复逐字流式可见。收尾靠持久化 agent_reply 落地触发
      //     finalizedBubbles 退场（已有逻辑），不在此清缓冲（worker 无 done phase 事件）。
      //     用 task_id 前缀判定路径：`task_` 前缀 → 真 task（PL-08）；否则 → worker 单聊 reply_id。
      //     后端 task id 一律 `_next_id("task")` = `task_` + hex（crud._PREFIX_MAP），reply_id 是
      //     裸 uuid4().hex（无前缀），故前缀判定可靠不混淆。
      if (d.type === 'task_token') {
        if (d.content && d.task_id) {
          const key = d.task_id as string
          if (key.startsWith('task_')) {
            // PL-08 真 task 执行流式 → streaming[task_id]
            setStreaming((prev) => ({
              ...prev,
              [key]: (prev[key] || '') + d.content,
            }))
          } else {
            // worker 单聊流式（reply_id）→ coordStreaming[reply_id]，复用协调者流式气泡渲染
            setCoordStreaming((prev) => ({
              ...prev,
              [key]: (prev[key] || '') + d.content,
            }))
          }
        }
      } else if (
        d.type === 'task_complete' ||
        d.type === 'task_failed' ||
        d.type === 'task_dispatch'
      ) {
        // 收尾清 PL-08 真 task 的流式缓冲（task_id 前缀）。worker 单聊 reply_id 缓冲
        // 不在此清——单聊回复无 task_complete 事件（非执行路径），靠持久化 agent_reply
        // 落地触发 finalizedBubbles 退场（finalizedBubbles 的 replied 判定过滤掉定稿气泡，
        // coordStreaming[reply_id] 残留也无害——下轮新 reply_id 覆盖，旧 key 不再写）。
        if (d.task_id) {
          const tid = d.task_id as string
          if (tid.startsWith('task_')) {
            setStreaming((prev) => {
              if (!(tid in prev)) return prev
              const next = { ...prev }
              delete next[tid]
              return next
            })
          }
        }
      }

      // → coordinator 流式回复（与 worker task_token 同构，按 reply_id 归并）
      // coordinator_token：逐字可见 content delta 累加到 coordStreaming[reply_id]。
      // coordinator_reasoning：推理模型在可见内容前流出的 reasoning_content delta，
      //   累加到 coordReasoning[reply_id]（前端折叠区展示，默认收起）。
      // coordinator_stats：更新 coordStats[reply_id] 的运行统计（耗时/token 数/phase/model/reasoning_tokens）。
      // phase="done" 时清空 coordStreaming + coordReasoning + coordStats[reply_id]
      //   —— 流式气泡退场，让随后落地的持久化 agent_reply 接管（同 worker streaming→finalized）。
      //   coordStats[reply_id] 也一并清空，避免陈旧统计行残留误导用户（下一轮新
      //   reply_id 会创建新条目，旧 reply_id 不再写入，清掉最干净）。
      if (d.type === 'coordinator_token') {
        const replyId =
          d.data && typeof d.data === 'object'
            ? (d.data as Record<string, unknown>).reply_id
            : null
        if (d.content && typeof replyId === 'string') {
          const rid = replyId
          setCoordStreaming((prev) => ({
            ...prev,
            [rid]: (prev[rid] || '') + d.content,
          }))
        }
      } else if (d.type === 'coordinator_reasoning') {
        const replyId =
          d.data && typeof d.data === 'object'
            ? (d.data as Record<string, unknown>).reply_id
            : null
        if (d.content && typeof replyId === 'string') {
          const rid = replyId
          setCoordReasoning((prev) => ({
            ...prev,
            [rid]: (prev[rid] || '') + d.content,
          }))
        }
      } else if (d.type === 'coordinator_stats') {
        const dd =
          d.data && typeof d.data === 'object'
            ? (d.data as Record<string, unknown>)
            : null
        const replyId = dd ? (dd['reply_id'] as string | undefined) : undefined
        if (dd && typeof replyId === 'string') {
          const phase = String(dd['phase'] || 'streaming')
          const elapsedMs = Number(dd['elapsed_ms'] || 0)
          const tokens = Number(dd['tokens'] || 0)
          const model =
            typeof dd['model'] === 'string' ? (dd['model'] as string) : undefined
          const reasoningTokensNum = Number(dd['reasoning_tokens'] || 0)
          const reasoning_tokens = Number.isFinite(reasoningTokensNum) && reasoningTokensNum > 0
            ? reasoningTokensNum
            : undefined
          // 只更新统计；phase=done 不再清 coordStreaming/coordReasoning/coordStats——
          // 改由持久化 agent_reply 落地时清（见下方 d.type==='agent_reply' 分支）。
          // 原 stats(done) 一到就清，但 agent_reply 几十毫秒后才到 → 中间空泡间隙，
          // 且多轮连发时下一轮的 stats(streaming,0 tokens) 趁虚混进空泡 → 「0 tokens 思考中」
          // 幽灵气泡 + 回复乱序。改以 agent_reply 落地为退场锚点：定稿气泡此刻同时入
          // chatMessages，流式气泡无缝交接，无空泡无乱序。
          // phase=done 仍写入 coordStats（isStreaming=phase!=='done' → false，气泡停流式光标
          // 显示「完成」，但内容/思考仍可见，等 agent_reply 落地才退场）。
          setCoordStats((prev) => ({
            ...prev,
            [replyId]: { elapsed_ms: elapsedMs, tokens, phase, model, reasoning_tokens },
          }))
        }
      }

      // → 持久化 agent_reply 落地：清该 reply_id 的流式缓冲（协调者 + worker 单聊统一）。
      // 流式气泡退场，让此刻落地的定稿气泡接管。agent_reply.data.reply_id 由后端 _stream_stats
      // 落盘（协调者 node_chat + worker node_chat 同形），作退场锚点比 stats(done) 更准——
      // 定稿气泡此刻同时入 chatMessages（logs effect），无缝交接。worker 单聊原靠
      // finalizedBubbles replied 判定 + coordStreaming 残留无害兜底，现统一显式清，更干净。
      if (d.type === 'agent_reply' && d.data && typeof d.data === 'object') {
        const rid = (d.data as Record<string, unknown>).reply_id
        if (typeof rid === 'string' && rid) {
          setCoordStreaming((prev) => {
            if (!(rid in prev)) return prev
            const next = { ...prev }
            delete next[rid]
            return next
          })
          setCoordReasoning((prev) => {
            if (!(rid in prev)) return prev
            const next = { ...prev }
            delete next[rid]
            return next
          })
          setCoordStats((prev) => {
            if (!(rid in prev)) return prev
            const next = { ...prev }
            delete next[rid]
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
  }, [groupId, handleReconnect, refreshPlan])

  return { logs, statusEvents, events, agentStatuses, plan, streaming, coordStreaming, coordReasoning, coordStats, refreshPlan }
}
