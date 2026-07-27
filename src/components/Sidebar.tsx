import { useState } from 'react'
import { Avatar, Button, Collapse, Empty, Spin, Tooltip } from 'antd'
import {
  PlusOutlined,
  RobotOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'

import { useSelection } from '../contexts/SelectionContext'
import CreateGroupModal from './CreateGroupModal'

/** Sidebar 宽度（固定，flexShrink:0）。 */
const SIDEBAR_WIDTH = 240
/** 品牌橙（强调色，选中项左条 + 品牌字）。 */
const BRAND = '#F26522'
/** 侧栏浮起面：白底（与顶栏一致，浮于主区冷灰之上）。 */
const SIDEBAR_BG = 'var(--surface-raised)'
/** 侧栏与主区的分隔线。 */
const SIDEBAR_BORDER = 'var(--border-soft)'

/** agent 状态 → 圆点色（与 AgentPage/Statusbar 对齐）。 */
const STATUS_DOT: Record<string, string> = {
  idle: '#52c41a',
  executing: '#F26522',
  offline: '#d9d9d9',
}

interface SidebarProps {
  /**
   * 选中列表项后切回聊天视图（顶部栏视图切换为 'chat'）。由 Layout 传入——
   * 广场页（AgentPage/SkillPage）展示时点侧栏会话/群组应立即进入对应单聊/群聊，
   * 而非停留在广场页。
   */
  onNavigateChat: () => void
  /**
   * 打开「用户信息」设置弹窗。用户入口（头像）现渲染在侧栏左下角（原顶栏右上角），
   * 点击触发由 Layout 下发的 openUserSettings（默认定位到 SettingsModal 的 user 项）。
   */
  onOpenUserSettings: () => void
}

/**
 * Sidebar — 左栏导航（顶部栏改版 2026-07-12）。
 *
 * 240px 浅灰侧栏，VS Code/Linear 极简风格。结构：
 *  - 上：两个可折叠分组（antd Collapse）：
 *    「会话」= 单聊会话列表（点选进单聊）；
 *    「智能体群」= 群组列表（点选进群聊）。
 *    会话分组底部不带 +新建——新建单聊的入口在智能体广场（点 agent find-or-create），
 *    这是产品决策：侧栏承载「最近会话」浏览，不承担新建职能。
 *    智能体群分组底部带 +新建群组（CreateGroupModal）。
 *  - 下：用户入口条（头像 + 「用户信息」），点击打开 SettingsModal 并默认落在用户信息项。
 *    2026-07-12 从顶栏右上角移来——顶栏是品牌+视图切换语义区，用户/登录入口混入语义杂；
 *    侧栏底部符合 VS Code/Cursor/Linear 等工具习惯。
 *    品牌区与设置入口已上移至全局顶部栏（见 Layout），本组件不再渲染头部。
 *
 * 选择态走 SelectionContext：
 *  - selectGroup（直接 setGroupId）：用于单聊会话（conversation id 作 groupId
 *    订阅 WS，机制不变）和群组切换。
 * 数据（groups/conversations/agents/agentStatusMap）由 SelectionContext 集中加载，
 * Sidebar 只消费渲染。
 * 选中项时同步调 onNavigateChat 把顶部栏视图切回聊天。
 *
 * 重构说明（2026-07-27）：侧栏不再列 agent（原「智能体」分组退役），agent 创建/
 * 管理归智能体广场页（AgentPage）。侧栏改为列「会话」（单聊 ConversationEntity 列表），
 * 让用户聚焦「最近聊过的会话」而非「全部 agent 实体」。「平台助手」即指这些会话本身——
 * 轻度用户在广场点 agent 开一个会话就当助手用，侧栏只列「最近会话」，无独立常驻条。
 */
export default function Sidebar({ onNavigateChat, onOpenUserSettings }: SidebarProps) {
  const {
    groups,
    conversations,
    agents,
    agentStatusMap,
    loading,
    activeConversation,
    activeGroup,
    createNewConversation,
    selectGroup,
    refreshAll,
  } = useSelection()

  // Collapse 展开态：默认两个分组都展开（首屏即见列表）。
  const [openKeys, setOpenKeys] = useState<string[]>(['conversations', 'groups'])

  // Path C：单聊是独立 ConversationEntity（不在 groups 里），groups 列表天然不含单聊，
  // 无需再过滤 config.single_chat。单聊在「会话」分组以单聊会话形式进入（selectGroup 选已有会话）。
  const multiAgentGroups = groups

  // 选中任一列表项后切回聊天视图（广场页 → 聊天的直觉切换）。
  const wrapSelect = (fn: (id: string) => void) => (id: string) => {
    fn(id)
    onNavigateChat()
  }

  return (
    <div
      style={{
        width: SIDEBAR_WIDTH,
        flexShrink: 0,
        background: SIDEBAR_BG,
        borderRight: `1px solid ${SIDEBAR_BORDER}`,
        boxShadow: 'var(--shadow-sidebar)',
        position: 'relative',
        zIndex: 1,
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
      }}
    >
      {/* 分组列表（品牌区已上移至顶部栏） */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        {loading && groups.length === 0 && agents.length === 0 ? (
          <div style={{ textAlign: 'center', padding: 24 }}>
            <Spin size="small" />
          </div>
        ) : (
          <Collapse
            ghost
            activeKey={openKeys}
            onChange={(keys) => setOpenKeys(keys as string[])}
            expandIconPosition="end"
            style={{ background: 'transparent' }}
            items={[
              {
                key: 'conversations',
                label: <GroupLabel title="会话" count={conversations.length} />,
                children: (
                  <ConversationsPanel
                    conversations={conversations}
                    agents={agents}
                    agentStatusMap={agentStatusMap}
                    activeConversation={activeConversation}
                    onSelectConversation={wrapSelect(selectGroup)}
                    onCreateConversation={createNewConversation}
                  />
                ),
              },
              {
                key: 'groups',
                label: <GroupLabel title="智能体群" count={multiAgentGroups.length} />,
                children: (
                  <GroupsPanel
                    groups={multiAgentGroups}
                    activeGroupId={activeGroup ? activeGroup.id : null}
                    onSelect={wrapSelect(selectGroup)}
                    agents={agents}
                    onCreated={refreshAll}
                  />
                ),
              },
            ]}
          />
        )}
      </div>

      {/* 用户入口条（头像 + 「用户信息」）。2026-07-12 从顶栏右上角移来——顶栏是品牌+
          视图切换语义区，用户/登录入口混入语义杂；侧栏底部符合 VS Code/Cursor/Linear 习惯。
          flexShrink:0 固定在侧栏底部，列表区 flex:1 滚动时此条不动。顶分隔线与列表区拉开。 */}
      <Tooltip title="用户信息" placement="right">
        <div
          onClick={onOpenUserSettings}
          style={{
            flexShrink: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '10px 12px',
            borderTop: `1px solid ${SIDEBAR_BORDER}`,
            cursor: 'pointer',
            transition: 'background 0.15s',
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLDivElement).style.background = '#f5f5f5'
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLDivElement).style.background = 'transparent'
          }}
        >
          <Avatar size={28} icon={<UserOutlined />} style={{ background: BRAND, flexShrink: 0 }} />
          <span style={{ fontSize: 13, color: '#333' }}>用户信息</span>
        </div>
      </Tooltip>
    </div>
  )
}

/** 分组标题：名称 + 计数。 */
function GroupLabel({ title, count }: { title: string; count: number }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' }}>
      <span style={{ fontSize: 12, color: '#8c8c8c', fontWeight: 600, letterSpacing: 0.5 }}>{title}</span>
      <span style={{ fontSize: 11, color: '#bbb' }}>{count}</span>
    </div>
  )
}

/**
 * 会话分组列表内容。
 *
 * 单聊会话列表——按 updated_at 倒序，图标 RobotOutlined。点击走 onSelectConversation
 * （selectGroup 用 conversation id 订阅 WS，与群组切换同机制）。
 *
 * 底部「+新对话」按钮——豆包式新建会话入口，后端每次新建一个绑平台助手的
 * ConversationEntity。平台助手是全平台唯一常驻 agent（slug='platform_assistant'），
 * 侧栏所有会话都绑它。
 */
function ConversationsPanel({
  conversations,
  agents,
  agentStatusMap,
  activeConversation,
  onSelectConversation,
  onCreateConversation,
}: {
  conversations: ReturnType<typeof useSelection>['conversations']
  agents: ReturnType<typeof useSelection>['agents']
  agentStatusMap: Record<string, string>
  activeConversation: ReturnType<typeof useSelection>['activeConversation']
  /** 常规会话：走 selectGroup（conversation id 作 groupId 订阅 WS）。 */
  onSelectConversation: (conversationId: string) => void
  /** 新建会话：豆包式，后端新建一个绑平台助手的 ConversationEntity。 */
  onCreateConversation: () => Promise<void>
}) {
  // 单聊会话：按 updated_at 倒序排（最近聊过的在最上）。
  const sortedConversations = conversations
    .slice()
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))

  return (
    <div style={{ paddingBottom: 4 }}>
      {/* 单聊会话列表。 */}
      {sortedConversations.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无会话"
          style={{ margin: '12px 0' }}
        />
      ) : (
        sortedConversations.map((c) => {
          const agent = agents.find((a) => a.id === c.agent_id)
          const status = agent ? agentStatusMap[agent.id] ?? 'offline' : 'offline'
          const active = !!activeConversation && activeConversation.id === c.id
          return (
            <SidebarItem
              key={c.id}
              active={active}
              onClick={() => onSelectConversation(c.id)}
              title={c.name || '新对话'}
              dotColor={STATUS_DOT[status] ?? STATUS_DOT.offline}
              icon={<RobotOutlined />}
            />
          )
        })
      )}
      {/* 新建会话入口——豆包式，底部固定。 */}
      <div style={{ padding: '4px 8px' }}>
        <Button
          block
          size="small"
          type="text"
          icon={<PlusOutlined />}
          onClick={() => onCreateConversation()}
          style={{ textAlign: 'left' }}
        >
          新对话
        </Button>
      </div>
    </div>
  )
}

/** 智能体群分组列表内容。 */
function GroupsPanel({
  groups,
  activeGroupId,
  onSelect,
  agents,
  onCreated,
}: {
  groups: ReturnType<typeof useSelection>['groups']
  activeGroupId: string | null
  onSelect: (groupId: string) => void
  agents: ReturnType<typeof useSelection>['agents']
  onCreated?: () => void
}) {
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <div style={{ paddingBottom: 4 }}>
      {groups.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无群组" style={{ margin: '12px 0' }} />
      ) : (
        groups.map((g) => (
          <SidebarItem
            key={g.id}
            active={activeGroupId === g.id}
            onClick={() => onSelect(g.id)}
            title={g.name}
            subtitle={g.description ?? undefined}
            icon={<TeamOutlined />}
          />
        ))
      )}
      <div style={{ padding: '4px 8px' }}>
        <Button
          block
          size="small"
          type="text"
          icon={<PlusOutlined />}
          onClick={() => setCreateOpen(true)}
          style={{ textAlign: 'left' }}
        >
          新建群组
        </Button>
      </div>
      <CreateGroupModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        agents={agents}
        onCreated={onCreated}
      />
    </div>
  )
}

/** 侧栏列表项：图标 + 主标题 + 副标题 + 选中态左条 + 状态圆点（可选）。 */
function SidebarItem({
  active,
  onClick,
  title,
  subtitle,
  icon,
  dotColor,
}: {
  active: boolean
  onClick: () => void
  title: string
  subtitle?: string
  icon: React.ReactNode
  dotColor?: string
}) {
  return (
    <Tooltip title={subtitle && subtitle.length > 16 ? subtitle : undefined} placement="right">
      <div
        onClick={onClick}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 12px',
          margin: '2px 6px',
          borderRadius: 6,
          cursor: 'pointer',
          background: active ? '#FFF3ED' : 'transparent',
          borderLeft: active ? `3px solid ${BRAND}` : '3px solid transparent',
          transition: 'background 0.15s',
        }}
        onMouseEnter={(e) => {
          if (!active) (e.currentTarget as HTMLDivElement).style.background = '#ececec'
        }}
        onMouseLeave={(e) => {
          if (!active) (e.currentTarget as HTMLDivElement).style.background = 'transparent'
        }}
      >
        <span style={{ color: active ? BRAND : '#8c8c8c', fontSize: 14, flexShrink: 0 }}>{icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: active ? 600 : 400,
              color: active ? BRAND : '#333',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {title}
          </div>
          {subtitle && (
            <div
              style={{
                fontSize: 11,
                color: '#aaa',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {subtitle}
            </div>
          )}
        </div>
        {dotColor && (
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: '50%',
              background: dotColor,
              flexShrink: 0,
            }}
          />
        )}
      </div>
    </Tooltip>
  )
}
