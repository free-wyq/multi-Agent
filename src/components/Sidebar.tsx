import { useMemo, useState } from 'react'
import {
  Avatar,
  Button,
  Checkbox,
  Collapse,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Spin,
  Tooltip,
} from 'antd'
import {
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
  TeamOutlined,
  UpOutlined,
  UserOutlined,
} from '@ant-design/icons'

import { useSelection } from '../contexts/SelectionContext'
import type { Conversation } from '../services/api'
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
    renameConversation,
    deleteConversation,
    deleteConversations,
    selectGroup,
    refreshAll,
  } = useSelection()

  // Collapse 展开态：默认两个分组都展开（首屏即见列表）。
  const [openKeys, setOpenKeys] = useState<string[]>(['conversations', 'groups'])

  // 会话分组的搜索 Modal 开关 + 管理模式开关。这两个交互入口已上移到「会话」分组标题
  // （标题在父组件 Collapse items 里渲染），故 state 提到 Sidebar 层，下发给
  // ConversationsPanel 消费/控制（标题按钮改 state，Panel 读 state 渲染对应 UI）。
  const [searchOpen, setSearchOpen] = useState(false)
  const [manageMode, setManageMode] = useState(false)

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
      {/* 分组列表（品牌区已上移至顶部栏）。
          参考 shadcn 后台侧栏：整个 nav 区是滚动容器，padding 12px 8px，
          分组之间用 marginBottom 16px 拉开。 */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '12px 8px' }}>
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
            expandIcon={({ isActive }) =>
              isActive ? (
                <UpOutlined style={{ fontSize: 10, color: '#8c8c8c' }} />
              ) : (
                <DownOutlined style={{ fontSize: 10, color: '#8c8c8c' }} />
              )
            }
            style={{ background: 'transparent', gap: 16 }}
            className="sidebar-collapse"
            items={[
              {
                key: 'conversations',
                label: (
                  <GroupLabel
                    title="会话"
                    count={conversations.length}
                    actions={
                      <Tooltip title="搜索会话">
                        <Button
                          size="small"
                          type="text"
                          icon={<SearchOutlined />}
                          onClick={() => setSearchOpen(true)}
                          style={{ color: '#8c8c8c' }}
                        />
                      </Tooltip>
                    }
                  />
                ),
                children: (
                  <ConversationsPanel
                    conversations={conversations}
                    agents={agents}
                    agentStatusMap={agentStatusMap}
                    activeConversation={activeConversation}
                    onSelectConversation={wrapSelect(selectGroup)}
                    onCreateConversation={createNewConversation}
                    onRenameConversation={renameConversation}
                    onDeleteConversation={deleteConversation}
                    onBatchDeleteConversations={deleteConversations}
                    searchOpen={searchOpen}
                    onSearchClose={() => setSearchOpen(false)}
                    manageMode={manageMode}
                    onManageModeChange={setManageMode}
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

/** 分组标题：参考 shadcn 后台侧栏——小号大写 tracked muted 标签 + 弱化计数。
 *  - 标签：fontSize 10, fontWeight 600, uppercase, letterSpacing 0.14em, color #8c8c8c。
 *  - 计数：fontSize 11，色更淡（#bbb），保留「会话 N」的语义价值但弱化视觉权重。
 *  - actions：可选的操作入口槽位（搜索/管理按钮），渲染在标签行右侧、计数旁——
 *    这样搜索放大镜紧挨「会话」标题，点击弹 Modal 搜列表（取代旧的常驻输入框）。 */
function GroupLabel({
  title,
  count,
  actions,
}: {
  title: string
  count: number
  actions?: React.ReactNode
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, width: '100%' }}>
      <span
        style={{
          fontSize: 10,
          color: '#8c8c8c',
          fontWeight: 600,
          letterSpacing: '0.14em',
          textTransform: 'uppercase',
        }}
      >
        {title}
      </span>
      <span style={{ fontSize: 11, color: '#bbb', marginRight: 'auto' }}>{count}</span>
      {actions}
    </div>
  )
}

/**
 * 会话分组列表内容。
 *
 * 单聊会话列表——按 updated_at 倒序。点击走 onSelectConversation
 * （selectGroup 用 conversation id 订阅 WS，与群组切换同机制）。
 *
 * 底部「+新对话」按钮——豆包式新建会话入口，后端每次新建一个绑平台助手的
 * ConversationEntity。平台助手是全平台唯一常驻 agent（slug='platform_assistant'），
 * 侧栏所有会话都绑它。
 *
 * 交互重构（2026-07-29）：
 *  - 搜索：不再是常驻输入框，改为「会话」标题旁的放大镜按钮点开 Modal 搜列表
 *    （searchOpen/onSearchClose 由父组件控制，因按钮在标题区）。
 *  - 管理：标题旁齿轮按钮切管理模式（manageMode/onManageModeChange 由父组件控制），
 *    管理模式工具栏（全选/批量删/完成）+ 每行 Checkbox。
 *  - 滚动：列表容器 maxHeight:300（约 8 项），超出内部滚动，不再顶出屏。
 *  antd Modal/Popconfirm/Checkbox 不手搓。
 */
function ConversationsPanel({
  conversations,
  agents,
  agentStatusMap,
  activeConversation,
  onSelectConversation,
  onCreateConversation,
  onRenameConversation,
  onDeleteConversation,
  onBatchDeleteConversations,
  searchOpen,
  onSearchClose,
  manageMode,
  onManageModeChange,
}: {
  conversations: Conversation[]
  agents: ReturnType<typeof useSelection>['agents']
  agentStatusMap: Record<string, string>
  activeConversation: Conversation | null
  /** 常规会话：走 selectGroup（conversation id 作 groupId 订阅 WS）。 */
  onSelectConversation: (conversationId: string) => void
  /** 新建会话：豆包式，后端新建一个绑平台助手的 ConversationEntity。 */
  onCreateConversation: () => Promise<void>
  /** 重命名会话：PUT /api/conversations/{id} 写回 name + 本地 patch。 */
  onRenameConversation: (conversationId: string, name: string) => Promise<void>
  /** 删除单个会话（级联消息/任务）。 */
  onDeleteConversation: (conversationId: string) => Promise<void>
  /** 批量删除会话。 */
  onBatchDeleteConversations: (conversationIds: string[]) => Promise<void>
  /** 搜索 Modal 是否打开（按钮在标题区，state 在父组件）。 */
  searchOpen: boolean
  /** 关闭搜索 Modal。 */
  onSearchClose: () => void
  /** 管理模式开关（按钮在标题区，state 在父组件）。 */
  manageMode: boolean
  /** 切换管理模式。 */
  onManageModeChange: (m: boolean) => void
}) {
  // 搜索关键字（实时过滤，大小写不敏感 includes）——现仅搜索 Modal 内用。
  const [keyword, setKeyword] = useState('')
  // 管理模式下选中的会话 id 集合（用 Set 避免 O(n) 去重）。
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  // 重命名 Modal 受控态：当前正在编辑的会话 + 输入框值。null=关闭。
  const [renameTarget, setRenameTarget] = useState<{ id: string; name: string } | null>(null)

  // 单聊会话：按 updated_at 倒序排（最近聊过的在最上）。
  const sortedConversations = useMemo(
    () =>
      conversations
        .slice()
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [conversations],
  )

  // 搜索 Modal 内的过滤结果（按关键字）。
  const searchResults = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return sortedConversations
    return sortedConversations.filter((c) => (c.name || '新对话').toLowerCase().includes(kw))
  }, [sortedConversations, keyword])

  // 侧栏列表本体始终展示全量已排序列表（搜索独立走 Modal，不再 inline 过滤侧栏列表）。
  const visibleConversations = sortedConversations

  const allSelected =
    visibleConversations.length > 0 &&
    visibleConversations.every((c) => selectedIds.has(c.id))

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (allSelected) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(visibleConversations.map((c) => c.id)))
    }
  }

  const handleBatchDelete = async () => {
    const ids = Array.from(selectedIds)
    if (ids.length === 0) return
    await onBatchDeleteConversations(ids)
    setSelectedIds(new Set())
    onManageModeChange(false)
  }

  const submitRename = async () => {
    if (!renameTarget) return
    const trimmed = renameTarget.name.trim()
    if (!trimmed) return
    await onRenameConversation(renameTarget.id, trimmed)
    setRenameTarget(null)
  }

  const exitManageMode = () => {
    onManageModeChange(false)
    setSelectedIds(new Set())
  }

  return (
    <div style={{ paddingBottom: 4 }}>
      {/* 管理模式工具栏：左已选/全选，右批量删除/完成。
          （搜索入口已上移到「会话」标题旁的放大镜按钮，弹 Modal 搜，不再常驻输入框。） */}
      {manageMode && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '2px 12px 6px 12px',
            borderBottom: '1px solid var(--border-soft)',
            marginBottom: 4,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Checkbox checked={allSelected} onChange={toggleSelectAll}>
              全选
            </Checkbox>
            <span style={{ fontSize: 11, color: '#8c8c8c' }}>
              已选 {selectedIds.size} / 共 {visibleConversations.length} 项
            </span>
          </div>
          <div style={{ display: 'flex', gap: 4 }}>
            <Popconfirm
              title="批量删除"
              description={`确定删除选中的 ${selectedIds.size} 个会话？`}
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={handleBatchDelete}
              disabled={selectedIds.size === 0}
            >
              <Button
                size="small"
                danger
                disabled={selectedIds.size === 0}
              >
                批量删除
              </Button>
            </Popconfirm>
            <Button size="small" onClick={exitManageMode}>
              完成
            </Button>
          </div>
        </div>
      )}

      {/* 会话列表：扁平 nav-link 风格（参考 shadcn：relative 容器 + 左图标灰 + truncate 文本）。
          maxHeight 300（约 8 项）超出内部滚动。 */}
      <div style={{ maxHeight: 300, overflowY: 'auto' }}>
        {visibleConversations.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无会话"
            style={{ margin: '12px 0' }}
          />
        ) : (
          visibleConversations.map((c) => {
            const agent = agents.find((a) => a.id === c.agent_id)
            const status = agent ? agentStatusMap[agent.id] ?? 'offline' : 'offline'
            const active = !!activeConversation && activeConversation.id === c.id
            const isSelected = selectedIds.has(c.id)
            return (
              <ConversationRow
                key={c.id}
                conversation={c}
                active={active}
                selected={isSelected}
                manageMode={manageMode}
                statusColor={STATUS_DOT[status] ?? STATUS_DOT.offline}
                onSelect={() => onSelectConversation(c.id)}
                onToggleSelect={() => toggleSelect(c.id)}
                onRename={() =>
                  setRenameTarget({ id: c.id, name: c.name || '' })
                }
                onDelete={() => onDeleteConversation(c.id)}
                onEnterManage={() => {
                  onManageModeChange(true)
                  // 进管理模式同时把这一项预选中，让用户直接续上批量删。
                  toggleSelect(c.id)
                }}
              />
            )
          })
        )}
      </div>

      {/* 普通模式下显示新建会话入口（管理模式聚焦批量操作，隐藏）。 */}
      {!manageMode && (
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
      )}

      {/* 重命名 Modal：受控输入，OK 校验 trim 非空。 */}
      <Modal
        title="重命名会话"
        open={renameTarget !== null}
        okText="保存"
        cancelText="取消"
        onOk={submitRename}
        onCancel={() => setRenameTarget(null)}
        destroyOnClose
      >
        <Input
          autoFocus
          placeholder="请输入会话名称"
          value={renameTarget?.name ?? ''}
          onChange={(e) =>
            setRenameTarget((prev) =>
              prev ? { ...prev, name: e.target.value } : prev,
            )
          }
          onPressEnter={submitRename}
        />
      </Modal>

      {/* 搜索 Modal：放大镜按钮点开，输入关键字实时过滤会话列表，
          点结果项进对应会话并关闭 Modal。 */}
      <Modal
        title="搜索会话"
        open={searchOpen}
        footer={null}
        onCancel={() => {
          onSearchClose()
          setKeyword('')
        }}
        destroyOnClose
        width={420}
      >
        <Input
          autoFocus
          allowClear
          placeholder="搜索会话名称"
          prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <div style={{ maxHeight: 360, overflowY: 'auto', marginTop: 8 }}>
          {searchResults.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={keyword ? '无匹配会话' : '暂无会话'}
              style={{ margin: '16px 0' }}
            />
          ) : (
            searchResults.map((c) => {
              const agent = agents.find((a) => a.id === c.agent_id)
              const status = agent ? agentStatusMap[agent.id] ?? 'offline' : 'offline'
              const active = !!activeConversation && activeConversation.id === c.id
              return (
                <ConversationRow
                  key={c.id}
                  conversation={c}
                  active={active}
                  selected={false}
                  manageMode={false}
                  enableActions={false}
                  statusColor={STATUS_DOT[status] ?? STATUS_DOT.offline}
                  onSelect={() => {
                    onSelectConversation(c.id)
                    onSearchClose()
                    setKeyword('')
                  }}
                  onToggleSelect={() => {}}
                  onRename={() => {}}
                  onDelete={() => Promise.resolve()}
                />
              )
            })
          )}
        </div>
      </Modal>
    </div>
  )
}

/** 单个会话行：参考 shadcn 后台 nav-link 扁平结构（relative 容器 + truncate 文本）。
 *  - 文本：truncate（overflow:hidden + textOverflow:ellipsis + whiteSpace:nowrap），选中品牌橙+500 字重。
 *  - 状态点：7px 圆点放项右侧（语义色保留）。
 *  - 选中态：背景 #FFF3ED 浅底 + 左侧细竖条（absolute h:20 w:2 居中 BRAND）替代旧的 3px borderLeft。
 *  - hover：非选中项背景 #f5f5f5（参考 muted）。
 *  - actions：普通模式 hover 浮现重命名/删除，放项右侧；管理模式隐藏，行首 Checkbox。
 *  - 点击：管理模式=勾选；普通模式=导航进会话。actions 区 stopPropagation 防误触发。 */
function ConversationRow({
  conversation,
  active,
  selected,
  manageMode,
  statusColor,
  enableActions = true,
  onSelect,
  onToggleSelect,
  onRename,
  onDelete,
  onEnterManage,
}: {
  conversation: Conversation
  active: boolean
  selected: boolean
  manageMode: boolean
  statusColor: string
  /** 是否渲染 hover 三点菜单（搜索 Modal 结果项为纯导航行，设 false 隐藏）。 */
  enableActions?: boolean
  onSelect: () => void
  onToggleSelect: () => void
  onRename: () => void
  onDelete: () => Promise<void>
  /** 「批量删除」菜单项进入管理模式（勾选 + 工具栏）。 */
  onEnterManage?: () => void
}) {
  const [hovered, setHovered] = useState(false)
  const title = conversation.name || '新对话'

  // 管理模式点行=勾选；普通模式点行=导航进会话。
  const handleClick = manageMode ? onToggleSelect : onSelect

  // 三点菜单：仅普通模式 + 启用 actions + hover 时浮现。
  const showActions = enableActions && !manageMode && hovered

  return (
    <div
      onClick={handleClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '8px 10px',
        borderRadius: 6,
        cursor: 'pointer',
        background: active ? '#FFF3ED' : hovered ? '#f5f5f5' : 'transparent',
        transition: 'background 0.15s',
      }}
    >
      {/* 选中态左侧细竖条：absolute h:20 w:2 居中 BRAND，替代旧 3px borderLeft。 */}
      {active && (
        <span
          style={{
            position: 'absolute',
            left: 0,
            top: '50%',
            height: 20,
            width: 2,
            transform: 'translateY(-50%)',
            borderRadius: '0 2px 2px 0',
            background: BRAND,
          }}
        />
      )}

      {/* 管理模式行首 Checkbox。 */}
      {manageMode && (
        <Checkbox
          checked={selected}
          onClick={(e) => {
            // 阻止 Checkbox 自带 toggle 与行 onClick 双触发（行已处理 toggle）。
            e.stopPropagation()
            onToggleSelect()
          }}
          style={{ flexShrink: 0 }}
        />
      )}

      {/* 文本：truncate。 */}
      <span
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 13,
          fontWeight: active ? 500 : 400,
          color: active ? BRAND : '#333',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {title}
      </span>

      {/* 状态点：7px 圆点（语义色保留）。 */}
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: statusColor,
          flexShrink: 0,
        }}
      />

      {/* actions：普通模式 hover 浮现。 */}
      {showActions && (
        <span
          onClick={(e) => e.stopPropagation()}
          style={{ display: 'inline-flex', gap: 2, flexShrink: 0 }}
        >
          <Dropdown
            menu={{
              items: [
                {
                  key: 'rename',
                  icon: <EditOutlined />,
                  label: '重命名',
                  onClick: ({ domEvent }) => {
                    domEvent.stopPropagation()
                    onRename()
                  },
                },
                {
                  key: 'delete',
                  danger: true,
                  icon: <DeleteOutlined />,
                  label: '删除',
                  onClick: ({ domEvent }) => {
                    domEvent.stopPropagation()
                    // Dropdown 关闭后 Popconfirm 挂载点已失，用 Modal.confirm 二次确认
                    //（删会话级联清消息/任务，破坏性操作不可无确认直删）。
                    Modal.confirm({
                      title: '确定删除该会话？',
                      content: '会话内的消息和任务将一并删除，不可恢复',
                      okText: '删除',
                      okButtonProps: { danger: true },
                      cancelText: '取消',
                      onOk: () => onDelete(),
                    })
                  },
                },
                { type: 'divider' },
                {
                  key: 'manage',
                  icon: <MoreOutlined />,
                  label: '批量删除',
                  onClick: ({ domEvent }) => {
                    domEvent.stopPropagation()
                    onEnterManage?.()
                  },
                },
              ],
            }}
            trigger={['click']}
            placement="bottomRight"
          >
            <Button
              size="small"
              type="text"
              icon={<MoreOutlined />}
              onClick={(e) => e.stopPropagation()}
              style={{ color: '#8c8c8c' }}
            />
          </Dropdown>
        </span>
      )}
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

/** 侧栏列表项：参考 shadcn 后台 nav-link 扁平结构（relative 容器 + 左灰图标 + truncate 文本）。
 *  - 左图标：灰色（#8c8c8c），选中变品牌橙。
 *  - 文本：truncate，选中品牌橙+500 字重。
 *  - 选中态：背景 #FFF3ED 浅底 + 左侧细竖条（absolute h:20 w:2 居中 BRAND）。
 *  - hover：非选中项背景 #f5f5f5（参考 muted）。
 *  - 状态点：7px 圆点放项右侧（语义色保留）。 */
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
  const [hovered, setHovered] = useState(false)
  return (
    <Tooltip title={subtitle && subtitle.length > 16 ? subtitle : undefined} placement="right">
      <div
        onClick={onClick}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        style={{
          position: 'relative',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '8px 10px',
          borderRadius: 6,
          cursor: 'pointer',
          background: active ? '#FFF3ED' : hovered ? '#f5f5f5' : 'transparent',
          transition: 'background 0.15s',
        }}
      >
        {/* 选中态左侧细竖条：absolute h:20 w:2 居中 BRAND。 */}
        {active && (
          <span
            style={{
              position: 'absolute',
              left: 0,
              top: '50%',
              height: 20,
              width: 2,
              transform: 'translateY(-50%)',
              borderRadius: '0 2px 2px 0',
              background: BRAND,
            }}
          />
        )}
        <span style={{ color: active ? BRAND : '#8c8c8c', fontSize: 16, flexShrink: 0 }}>{icon}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: active ? 500 : 400,
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
