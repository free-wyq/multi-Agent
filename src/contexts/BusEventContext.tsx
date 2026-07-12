/**
 * BusEventContext — 全应用共享一个群组 WS 连接的单一真源（WS-01）。
 *
 * 背景：此前 GroupPage / MonitorPage / LeaderPanel / WorkerTrace / LogPanel 各自
 * 调 `useBusEvent(groupId)`，每个调用都 `onBusEvent` 起一条独立 WebSocket —— 同群
 * 多组件 = 多条重复 WS，事件被各自重复解析、状态被各自重复播种，浪费且易漂移。
 *
 * 解法：在 provider 层调一次 `useBusEvent(groupId)` 起一条 WS，把返回的
 * {logs, statusEvents, events, agentStatuses, plan, streaming} 经 Context 下发，
 * 同群所有组件消费同一份状态 —— 一条 WS 喂全应用。
 *
 * 与 WS-02 的协作：WS-02 会把 `useBusEvent(groupId)` 重构为「优先读 Context，命中同
 * groupId 则复用，否则自起 WS」。为此本文件**同时导出 context 对象本身**
 * （`BusEventContext`，可空默认 null）——WS-02 用 `useContext(BusEventContext)`
 * 做可空查找（无 provider 时回退自起 WS），而非走会 throw 的 `useBusEventContext`。
 * Provider 自身调的是「未重构的」`useBusEvent`：它读 context（provider 上方为 null）
 * → 命中空 → 自起 WS，无递归。
 *
 * 消费方（WS-04/WS-05）：用 `useBusEventContext()`（throw 版）拿确定非空的共享状态，
 * 省去 null 处理样板；裸用 provider 外会 throw，是编程错误应尽早暴露。
 */
import { createContext, useContext, useMemo, type ReactNode } from 'react'

import { useBusEvent, type LogEntry, type TaskStatusEvent } from '../hooks/useBusEvent'
import type { AgentStatusInfo, PlanStep, TraceEvent } from '../services/api'

/** Context 下发的共享状态：绑定的 groupId + useBusEvent 的全部返回字段。 */
export interface BusEventContextValue {
  /** 本 provider 绑定的群组 id（null = 未选群，WS 不订阅，状态全空）。 */
  groupId: string | null
  /** 切换当前聚焦群组（WS-03：App 层 state 经 provider 下发的 setter）。
   *  null = 取消聚焦（断开 WS）。供 Layout/页面选中群组时调用。 */
  setGroupId: (groupId: string | null) => void
  logs: LogEntry[]
  statusEvents: TaskStatusEvent[]
  events: TraceEvent[]
  agentStatuses: Record<string, AgentStatusInfo>
  plan: PlanStep[] | null
  streaming: Record<string, string>
  /** 协调者流式回复：reply_id → 累积的可见 content delta（coordinator_token 逐字拼接）。 */
  coordStreaming: Record<string, string>
  /** 协调者流式推理：reply_id → 累积的 reasoning_content delta（coordinator_reasoning 逐字拼接）。
   *  推理模型（DeepSeek/o1 类）在可见 content 之前流出内部思维链；非推理模型不流，条目不存在。 */
  coordReasoning: Record<string, string>
  /** 协调者流式统计：reply_id → 最新 { elapsed_ms, tokens, phase, model, reasoning_tokens }（coordinator_stats 节流更新）。 */
  coordStats: Record<string, { elapsed_ms: number; tokens: number; phase: string; model?: string; reasoning_tokens?: number }>
  /** 主动从真源拉取驻留计划（对齐后端 _dispatch_plan）。PlanConfirmCard 409 静默刷新、切群首拉复用。 */
  refreshPlan: () => Promise<void>
}

/**
 * Context 对象。默认 null —— 故意不提供 fallback 值：
 *  - `useBusEventContext()`（throw 版）给消费组件，裸用即暴露 bug；
 *  - `BusEventContext` 本身导出给 WS-02 的 `useBusEvent` 做 `useContext` 可空查找
 *    （命中 null → 无 provider → 自起 WS，零回归）。
 */
export const BusEventContext = createContext<BusEventContextValue | null>(null)

export interface BusEventProviderProps {
  /** 当前激活群组。null/空串时不订阅 WS（useBusEvent 内部判空跳过），状态为空。 */
  groupId: string | null
  /** 切换激活群组的 setter（WS-03：App 层 state 下发，经 context 暴露给消费方）。 */
  setGroupId: (groupId: string | null) => void
  children: ReactNode
}

/**
 * 在树顶层包一层，全应用共享一个群组 WS。
 *
 * 内部调一次 `useBusEvent(groupId)` 起唯一 WS，结果经 Context 下发。
 * `useMemo` 稳定 value 引用：仅在 groupId 变化或某状态字段引用变化时才换 value，
 * 避免父组件无关重渲染时新对象波及所有消费者。
 */
export function BusEventProvider({ groupId, setGroupId, children }: BusEventProviderProps) {
  const bus = useBusEvent(groupId)

  const value = useMemo<BusEventContextValue>(
    () => ({
      groupId,
      setGroupId,
      logs: bus.logs,
      statusEvents: bus.statusEvents,
      events: bus.events,
      agentStatuses: bus.agentStatuses,
      plan: bus.plan,
      streaming: bus.streaming,
      coordStreaming: bus.coordStreaming,
      coordReasoning: bus.coordReasoning,
      coordStats: bus.coordStats,
      refreshPlan: bus.refreshPlan,
    }),
    [
      groupId,
      setGroupId,
      bus.logs,
      bus.statusEvents,
      bus.events,
      bus.agentStatuses,
      bus.plan,
      bus.streaming,
      bus.coordStreaming,
      bus.coordReasoning,
      bus.coordStats,
      bus.refreshPlan,
    ],
  )

  return <BusEventContext.Provider value={value}>{children}</BusEventContext.Provider>
}

/**
 * 消费共享的群组 WS 状态。必须在 `BusEventProvider` 内使用 —— 裸用即 throw，
 * 尽早暴露「未包 provider」的接线错误（标准 React Context 模式）。
 *
 * 需要 nullable 查找（WS-02 的 `useBusEvent` 回退自起 WS）请直接
 * `useContext(BusEventContext)`，勿用本 hook。
 */
export function useBusEventContext(): BusEventContextValue {
  const ctx = useContext(BusEventContext)
  if (!ctx) {
    throw new Error('useBusEventContext 必须在 <BusEventProvider> 内使用')
  }
  return ctx
}
