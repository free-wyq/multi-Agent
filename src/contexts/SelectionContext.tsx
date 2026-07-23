import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import {
  agentApi,
  conversationApi,
  groupApi,
  systemApi,
  type AgentDefinition,
  type Conversation,
  type Group,
} from '../services/api'
import { useBusEventContext } from './BusEventContext'

/**
 * SelectionContext — 左右布局的「选择模型」单一真源（布局重构 2026-07-11）。
 *
 * 背景：三栏+路由布局退役后，单聊/群聊都收敛到「一个会话 id + ChatPanel」。
 * 左侧 Sidebar 的智能体列表和多智能体列表点击后都走 setGroupId，但单聊需要
 * find-or-create 一个 ConversationEntity（Path C 后单聊独立实体，不再用
 * config.single_chat===true 的 GroupEntity）。
 * 这个 find-or-create 逻辑 + groups/agents/conversations/status 共享数据的加载，
 * 集中在本 context，Sidebar（触发选择）和 ChatView（消费 groups/agents/members
 * 渲染 ChatPanel）共享。
 *
 * 持有：
 *  - groups / agents / conversations / agentStatusMap：首屏加载一次，selectAgent
 *    创建单聊会话后刷新 conversations。
 *  - selectAgent(agentId)：find-or-create 单聊会话 → setConversationId（走 BusEventContext）。
 *  - selectGroup(groupId)：直接 setGroupId。
 *  - activeKind / activeAgentId：从当前 groupId + groups/conversations 派生
 *    （单聊 conversation→agent，多智能体群→group），供 Sidebar 高亮 + ChatView
 *    标题区判断单聊/群聊用——无需额外 state，纯派生避免漂移。
 *
 * Provider 必须在 BusEventProvider 内使用（selectAgent/selectGroup 调 setGroupId）。
 *
 * Path C（单聊分实体）：single_chat flag 删除，单聊由独立 ConversationEntity 承载。
 * selectAgent 改调 POST /api/conversations（find-or-create），activeKind 从 activeConversation
 * vs activeGroup 派生（不再读 config.single_chat）。
 */

/** 智能体运行时状态（从 systemApi.listAllStatus 派生，与 AgentPage STATUS_MAP 对齐）。 */
type AgentStatus = 'idle' | 'executing' | 'offline'

export interface SelectionContextValue {
  /** 全部群组（多智能体群聊，不含单聊——Path C 后单聊是独立 ConversationEntity）。 */
  groups: Group[]
  /** 全部单聊会话（Path C 独立实体）。 */
  conversations: Conversation[]
  /** 全部智能体。 */
  agents: AgentDefinition[]
  /** agentId → 运行时状态（idle/executing/offline），用于左栏状态圆点。 */
  agentStatusMap: Record<string, AgentStatus>
  /** 数据加载中态。 */
  loading: boolean
  /** 重新拉取 groups + conversations + agents + 全量状态（selectAgent 创建单聊后调用）。 */
  refreshAll: () => Promise<void>

  /** 当前选中类型：单聊会话→'agent'，多智能体群→'group'，未选→null。纯派生。 */
  activeKind: 'agent' | 'group' | null
  /** 当前选中的 agent id（仅 activeKind==='agent' 时非 null）。纯派生。 */
  activeAgentId: string | null
  /** 当前 groupId 对应的群组对象（null=未选或当前是单聊）。 */
  activeGroup: Group | null
  /** 当前 conversationId 对应的单聊会话对象（null=未选或当前是群聊）。 */
  activeConversation: Conversation | null

  /** 选智能体进单聊：find-or-create ConversationEntity → setGroupId（conversation id）。 */
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
  // Path C：setGroupId 接收的 id 可能是 group_id（群聊）或 conversation_id（单聊），
  // ChatPanel/BusEventContext 按 id 订阅 WS 通道，机制不变。
  const { groupId, setGroupId } = useBusEventContext()

  const [groups, setGroups] = useState<Group[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [agentStatusMap, setAgentStatusMap] = useState<Record<string, AgentStatus>>({})
  const [loading, setLoading] = useState(false)

  const refreshAll = useCallback(async () => {
    setLoading(true)
    try {
      const [gData, cData, aData] = await Promise.all([
        groupApi.list(),
        conversationApi.list(),
        agentApi.list(),
      ])
      setGroups(gData)
      setConversations(cData)
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

  // 当前 groupId 对应的群组对象（群聊场景）。
  const activeGroup = useMemo(
    () => groups.find((g) => g.id === groupId) ?? null,
    [groups, groupId],
  )
  // 当前 conversationId 对应的单聊会话对象（单聊场景）。
  const activeConversation = useMemo(
    () => conversations.find((c) => c.id === groupId) ?? null,
    [conversations, groupId],
  )

  // 派生：单聊会话（activeConversation 存在）→ activeKind='agent'，否则 'group'。
  // Path C：不再读 config.single_chat，改看 activeConversation vs activeGroup。
  const isSingleChat = !!activeConversation
  const activeKind: 'agent' | 'group' | null = (activeGroup || activeConversation)
    ? isSingleChat
      ? 'agent'
      : 'group'
    : null
  const activeAgentId = isSingleChat ? activeConversation?.agent_id ?? null : null

  /**
   * 选智能体进单聊：find-or-create ConversationEntity。
   *
   * Path C：不再 groupApi.create({config:{single_chat:true}})，改调
   * conversationApi.create({agent_id})（后端 POST /api/conversations find-or-create
   * 语义：已有该 agent 的单聊则返回，否则新建）。成功后刷新 conversations 列表
   * → setGroupId(created.id)（conversation id 作 BusEventContext 的 groupId 角色，
   * WS 通道 bus-event:{conversationId} 机制不变）。
   */
  const selectAgent = useCallback(
    async (agentId: string) => {
      // 先在已加载 conversations 里找该 agent 的单聊
      const existing = conversations.find((c) => c.agent_id === agentId)
      if (existing) {
        setGroupId(existing.id)
        return
      }
      try {
        const created = await conversationApi.create({ agent_id: agentId })
        // 刷新 conversations 列表让新单聊出现在左栏「智能体」分组
        const cData = await conversationApi.list()
        setConversations(cData)
        setGroupId(created.id)
      } catch {
        /* 创建失败静默——后续可加 toast。避免阻塞选择交互。 */
      }
    },
    [conversations, setGroupId],
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
      conversations,
      agents,
      agentStatusMap,
      loading,
      refreshAll,
      activeKind,
      activeAgentId,
      activeGroup,
      activeConversation,
      selectAgent,
      selectGroup,
    }),
    [
      groups,
      conversations,
      agents,
      agentStatusMap,
      loading,
      refreshAll,
      activeKind,
      activeAgentId,
      activeGroup,
      activeConversation,
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
