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
 * 左侧 Sidebar 的会话列表和群组列表点击后都走 setGroupId，单聊是独立
 * ConversationEntity（Path C 后单聊独立实体，不再用 config.single_chat===true
 * 的 GroupEntity）。
 * 会话创建逻辑 + groups/agents/conversations/status 共享数据的加载，集中在本
 * context，Sidebar（触发选择）和 ChatView（消费 groups/agents/members 渲染
 * ChatPanel）共享。
 *
 * 持有：
 *  - groups / agents / conversations / agentStatusMap：首屏加载一次，createNewConversation
 *    创建单聊会话后刷新 conversations。
 *  - createNewConversation()：豆包式新建会话 → setGroupId（走 BusEventContext）。
 *    后端每次新建一个绑平台助手（slug='platform_assistant'）的 ConversationEntity。
 *  - selectGroup(groupId)：直接 setGroupId。
 *  - activeKind / activeAgentId：从当前 groupId + groups/conversations 派生
 *    （单聊 conversation→agent，多智能体群→group），供 Sidebar 高亮 + ChatView
 *    标题区判断单聊/群聊用——无需额外 state，纯派生避免漂移。
 *
 * Provider 必须在 BusEventProvider 内使用（createNewConversation/selectGroup 调 setGroupId）。
 *
 * 豆包式重构（2026-07-27）：单聊从「find-or-create per agent」改为「每次新建绑平台助手」。
 * 平台助手是全平台唯一常驻 agent（slug='platform_assistant'，后端 seed），侧栏【会话】所有
 * 会话都绑它。新建单聊入口在侧栏「+新对话」（不再走广场点 agent find-or-create）。
 * 广场点 agent 改为「体验对话」（transient 会话，不进侧栏，独立路径不走本 context）。
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
  /** 重新拉取 groups + conversations + agents + 全量状态（createNewConversation 创建单聊后调用）。 */
  refreshAll: () => Promise<void>

  /** 当前选中类型：单聊会话→'agent'，多智能体群→'group'，未选→null。纯派生。 */
  activeKind: 'agent' | 'group' | null
  /** 当前选中的 agent id（仅 activeKind==='agent' 时非 null）。纯派生。 */
  activeAgentId: string | null
  /** 当前 groupId 对应的群组对象（null=未选或当前是单聊）。 */
  activeGroup: Group | null
  /** 当前 conversationId 对应的单聊会话对象（null=未选或当前是群聊）。 */
  activeConversation: Conversation | null

  /** 新建一个会话（豆包式）：后端每次新建一个绑平台助手的 ConversationEntity → setGroupId。
   *  用于侧栏「+新对话」按钮。平台助手是常驻 agent（slug='platform_assistant'），单聊永远绑它。 */
  createNewConversation: () => Promise<void>
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
   * 新建一个会话（豆包式）：后端每次新建一个绑平台助手的 ConversationEntity。
   *
   * 后端 POST /api/conversations（不再 find-or-create per agent，每次新建；agent_id 省略
   * → 后端绑 platform_assistant 常驻助手）。成功后刷新 conversations 列表 → setGroupId。
   */
  const createNewConversation = useCallback(
    async () => {
      try {
        const created = await conversationApi.create({})
        const cData = await conversationApi.list()
        setConversations(cData)
        setGroupId(created.id)
      } catch {
        /* 创建失败静默——后续可加 toast。避免阻塞新建交互。 */
      }
    },
    [setGroupId],
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
      createNewConversation,
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
      createNewConversation,
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
