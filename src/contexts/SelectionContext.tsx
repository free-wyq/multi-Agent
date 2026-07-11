import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import {
  agentApi,
  groupApi,
  systemApi,
  type AgentDefinition,
  type Group,
} from '../services/api'
import { useBusEventContext } from './BusEventContext'

/**
 * SelectionContext — 左右布局的「选择模型」单一真源（布局重构 2026-07-11）。
 *
 * 背景：三栏+路由布局退役后，单聊/群聊都收敛到「一个 groupId + ChatPanel」。
 * 左侧 Sidebar 的智能体列表和多智能体列表点击后都走 setGroupId，但单聊需要
 * find-or-create 一个 config.single_chat===true 的群组（群主=被选 agent）。
 * 这个 find-or-create 逻辑 + groups/agents/status 共享数据的加载，集中在本 context，
 * Sidebar（触发选择）和 ChatView（消费 groups/agents/members 渲染 ChatPanel）共享。
 *
 * 持有：
 *  - groups / agents / agentStatusMap：首屏加载一次，selectAgent 创建单聊群后刷新 groups。
 *  - selectAgent(agentId)：find-or-create 单聊群组 → setGroupId（走 BusEventContext）。
 *  - selectGroup(groupId)：直接 setGroupId。
 *  - activeKind / activeAgentId：从当前 groupId + groups 派生（单聊群→agent，多智能体群→group），
 *    供 Sidebar 高亮 + ChatView 标题区判断单聊/群聊用——无需额外 state，纯派生避免漂移。
 *
 * Provider 必须在 BusEventProvider 内使用（selectAgent/selectGroup 调 setGroupId）。
 */

/** 智能体运行时状态（从 systemApi.listAllStatus 派生，与 AgentPage STATUS_MAP 对齐）。 */
type AgentStatus = 'idle' | 'executing' | 'offline'

export interface SelectionContextValue {
  /** 全部群组（含单聊群 config.single_chat===true）。 */
  groups: Group[]
  /** 全部智能体。 */
  agents: AgentDefinition[]
  /** agentId → 运行时状态（idle/executing/offline），用于左栏状态圆点。 */
  agentStatusMap: Record<string, AgentStatus>
  /** 数据加载中态。 */
  loading: boolean
  /** 重新拉取 groups + agents + 全量状态（selectAgent 创建单聊群后、GroupInfoDrawer 改群后调用）。 */
  refreshAll: () => Promise<void>

  /** 当前选中类型：单聊群→'agent'，多智能体群→'group'，未选→null。纯派生。 */
  activeKind: 'agent' | 'group' | null
  /** 当前选中的 agent id（仅 activeKind==='agent' 时非 null）。纯派生。 */
  activeAgentId: string | null
  /** 当前 groupId 对应的群组对象（null=未选）。 */
  activeGroup: Group | null

  /** 选智能体进单聊：find-or-create single_chat 群组 → setGroupId。 */
  selectAgent: (agentId: string) => Promise<void>
  /** 选多智能体群组进群聊：直接 setGroupId。 */
  selectGroup: (groupId: string) => void
}

const SelectionContext = createContext<SelectionContextValue | null>(null)

export interface SelectionProviderProps {
  children: ReactNode
}

export function SelectionProvider({ children }: SelectionProviderProps) {
  // setGroupId 来自 BusEventContext（App 层 state 经 provider 下发）。
  const { groupId, setGroupId } = useBusEventContext()

  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [agentStatusMap, setAgentStatusMap] = useState<Record<string, AgentStatus>>({})
  const [loading, setLoading] = useState(false)

  const refreshAll = useCallback(async () => {
    setLoading(true)
    try {
      const [gData, aData] = await Promise.all([groupApi.list(), agentApi.list()])
      setGroups(gData)
      setAgents(aData)
      // SA-04：单次拉全所有群组所有 agent 状态（GET /api/status 一次返回
      // {group_id: AgentStatusInfo[]}），合并成 {agentId: status}。与 AgentPage 同逻辑。
      const statusMap: Record<string, AgentStatus> = {}
      try {
        const allStatus = await systemApi.listAllStatus()
        Object.values(allStatus).forEach((list) => {
          list.forEach((s) => {
            statusMap[s.id] = (s.status as AgentStatus) || 'offline'
          })
        })
      } catch {
        /* 状态聚合拉取失败静默（后端未启动 / 无引擎时不影响列表展示） */
      }
      setAgentStatusMap(statusMap)
    } catch {
      /* 数据加载失败静默——左栏列表显示空，用户可重试。避免 toast 噪音。 */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshAll()
  }, [refreshAll])

  // 当前 groupId 对应的群组对象。
  const activeGroup = useMemo(
    () => groups.find((g) => g.id === groupId) ?? null,
    [groups, groupId],
  )

  // 派生：单聊群（config.single_chat===true）→ activeKind='agent'，否则 'group'。
  const isSingleChat = !!activeGroup?.config?.single_chat
  const activeKind: 'agent' | 'group' | null = activeGroup
    ? isSingleChat
      ? 'agent'
      : 'group'
    : null
  const activeAgentId = isSingleChat ? activeGroup?.coordinator_id ?? null : null

  /**
   * 选智能体进单聊：find-or-create 单聊群组。
   *
   * 先在已加载 groups 里找 config.single_chat===true 且 coordinator_id===agentId 的群；
   * 找到直接 setGroupId。没找到则 groupApi.create（name=agent.name，coordinator_id=agentId，
   * config:{single_chat:true}）→ 刷新 groups → setGroupId(新群 id)。这样单聊也复用 ChatPanel，
   * 该 group 的 coordinator 就是被选中的 agent，单聊时群主直接回话。
   */
  const selectAgent = useCallback(
    async (agentId: string) => {
      const existing = groups.find(
        (g) => g.config?.single_chat === true && g.coordinator_id === agentId,
      )
      if (existing) {
        setGroupId(existing.id)
        return
      }
      const agent = agents.find((a) => a.id === agentId)
      const name = agent?.name ?? '单聊'
      try {
        const created = await groupApi.create({
          name,
          coordinator_id: agentId,
          config: { single_chat: true },
        })
        // 刷新 groups 列表让新单聊群出现在左栏「多智能体」之外（单聊群不显示在多智能体分组，
        // 但出现在 groups 数据里供 activeGroup 派生）。
        const gData = await groupApi.list()
        setGroups(gData)
        setGroupId(created.id)
      } catch {
        /* 创建失败静默——后续可加 toast。避免阻塞选择交互。 */
      }
    },
    [groups, agents, setGroupId],
  )

  const selectGroup = useCallback(
    (gId: string) => {
      setGroupId(gId)
    },
    [setGroupId],
  )

  const value = useMemo<SelectionContextValue>(
    () => ({
      groups,
      agents,
      agentStatusMap,
      loading,
      refreshAll,
      activeKind,
      activeAgentId,
      activeGroup,
      selectAgent,
      selectGroup,
    }),
    [
      groups,
      agents,
      agentStatusMap,
      loading,
      refreshAll,
      activeKind,
      activeAgentId,
      activeGroup,
      selectAgent,
      selectGroup,
    ],
  )

  return <SelectionContext.Provider value={value}>{children}</SelectionContext.Provider>
}

/** 消费选择上下文。必须在 <SelectionProvider> 内使用。 */
export function useSelection(): SelectionContextValue {
  const ctx = useContext(SelectionContext)
  if (!ctx) {
    throw new Error('useSelection 必须在 <SelectionProvider> 内使用')
  }
  return ctx
}
