import { useState } from 'react'
import { Avatar, Button, Collapse, Empty, Form, Input, Modal, Select, Spin, Tooltip, message } from 'antd'
import {
  PlusOutlined,
  RobotOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'

import { useSelection } from '../contexts/SelectionContext'
import { agentApi, type AgentCreatePayload } from '../services/api'

/** Sidebar 宽度（固定，flexShrink:0）。 */
const SIDEBAR_WIDTH = 240
/** 品牌蓝（强调色，选中项左条 + 品牌字）。 */
const BRAND = '#0A5ACF'
/** 侧栏浮起面：白底（与顶栏一致，浮于主区冷灰之上）。 */
const SIDEBAR_BG = 'var(--surface-raised)'
/** 侧栏与主区的分隔线。 */
const SIDEBAR_BORDER = 'var(--border-soft)'

/** agent 状态 → 圆点色（与 AgentPage/Statusbar 对齐）。 */
const STATUS_DOT: Record<string, string> = {
  idle: '#52c41a',
  executing: '#1677ff',
  offline: '#d9d9d9',
}

interface SidebarProps {
  /**
   * 选中列表项后切回聊天视图（顶部栏视图切换为 'chat'）。由 Layout 传入——
   * 广场页（AgentPage/SkillPage）展示时点侧栏智能体/群组应立即进入对应单聊/群聊，
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
 *  - 上：两个可折叠分组（antd Collapse）：「智能体」= agent 列表（点选进单聊），
 *    「多智能体」= 群组列表（点选进群聊，过滤掉 config.single_chat===true 的单聊群）。
 *    每组底部带 +新建入口。
 *  - 下：用户入口条（头像 + 「用户信息」），点击打开 SettingsModal 并默认落在用户信息项。
 *    2026-07-12 从顶栏右上角移来——顶栏是品牌+视图切换语义区，用户/登录入口混入语义杂；
 *    侧栏底部符合 VS Code/Cursor/Linear 等工具习惯。
 *  - 品牌区与设置入口已上移至全局顶部栏（见 Layout），本组件不再渲染头部。
 *
 * 选择态走 SelectionContext：selectAgent（find-or-create 单聊群）/ selectGroup（直接切群）。
 * 数据（groups/agents/agentStatusMap）由 SelectionContext 集中加载，Sidebar 只消费渲染。
 * 选中项时同步调 onNavigateChat 把顶部栏视图切回聊天。
 *
 * 高亮：多智能体选中 = activeGroupId===g.id；智能体选中 = activeAgentId===agent.id。
 */
export default function Sidebar({ onNavigateChat, onOpenUserSettings }: SidebarProps) {
  const { groups, agents, agentStatusMap, loading, activeAgentId, activeGroup, selectAgent, selectGroup } =
    useSelection()

  // Collapse 展开态：默认两个分组都展开（首屏即见列表）。
  const [openKeys, setOpenKeys] = useState<string[]>(['agents', 'groups'])

  // 多智能体列表过滤掉单聊群（single_chat 群不显示在多智能体分组，只在智能体分组以单聊形式进入）。
  const multiAgentGroups = groups.filter((g) => !g.config?.single_chat)

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
                key: 'agents',
                label: <GroupLabel title="智能体" count={agents.length} />,
                children: <AgentsPanel
                  agents={agents}
                  agentStatusMap={agentStatusMap}
                  activeAgentId={activeAgentId}
                  onSelect={wrapSelect(selectAgent)}
                />,
              },
              {
                key: 'groups',
                label: <GroupLabel title="多智能体" count={multiAgentGroups.length} />,
                children: <GroupsPanel
                  groups={multiAgentGroups}
                  activeGroupId={activeGroup && !activeGroup.config?.single_chat ? activeGroup.id : null}
                  onSelect={wrapSelect(selectGroup)}
                />,
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

/** 智能体分组列表内容。 */
function AgentsPanel({
  agents,
  agentStatusMap,
  activeAgentId,
  onSelect,
}: {
  agents: ReturnType<typeof useSelection>['agents']
  agentStatusMap: Record<string, string>
  activeAgentId: string | null
  onSelect: (agentId: string) => void
}) {
  const [createOpen, setCreateOpen] = useState(false)
  const [form] = Form.useForm<AgentCreatePayload>()

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      await agentApi.create({
        name: values.name,
        role: values.role || '自定义',
        system_prompt: values.system_prompt,
        extra_skills: values.extra_skills ?? [],
      })
      message.success('智能体已创建')
      setCreateOpen(false)
      form.resetFields()
      // 刷新由 SelectionContext.refreshAll 负责——但此处简单复用：selectAgent 不会触发刷新，
      // 故创建后提示用户。后续可接 refreshAll。
    } catch {
      /* 校验失败 Form 已标红 */
    }
  }

  return (
    <div style={{ paddingBottom: 4 }}>
      {agents.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无智能体" style={{ margin: '12px 0' }} />
      ) : (
        agents.map((agent) => {
          const active = activeAgentId === agent.id
          const status = agentStatusMap[agent.id] ?? 'offline'
          return (
            <SidebarItem
              key={agent.id}
              active={active}
              onClick={() => onSelect(agent.id)}
              dotColor={STATUS_DOT[status] ?? STATUS_DOT.offline}
              title={agent.name}
              subtitle={agent.role}
              icon={<RobotOutlined />}
            />
          )
        })
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
          新建
        </Button>
      </div>

      {/* 新建智能体 Modal（极简：名+角色+prompt+技能，复用 agentApi.create） */}
      <Modal
        open={createOpen}
        title="新建智能体"
        onCancel={() => {
          setCreateOpen(false)
          form.resetFields()
        }}
        onOk={handleCreate}
        destroyOnClose
        okText="创建"
        width={480}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：后端小新" autoComplete="off" />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              placeholder="选择角色"
              options={[
                '后端开发工程师',
                '前端开发工程师',
                '测试工程师',
                'DevOps 工程师',
                '产品经理',
                '自定义',
              ].map((r) => ({ value: r, label: r }))}
            />
          </Form.Item>
          <Form.Item name="system_prompt" label="角色描述">
            <Input.TextArea rows={3} placeholder="自定义角色描述（system prompt）" />
          </Form.Item>
          <Form.Item name="extra_skills" label="额外技能">
            <Select mode="tags" placeholder="输入技能名后回车" tokenSeparators={[',']} allowClear />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

/** 多智能体分组列表内容。 */
function GroupsPanel({
  groups,
  activeGroupId,
  onSelect,
}: {
  groups: ReturnType<typeof useSelection>['groups']
  activeGroupId: string | null
  onSelect: (groupId: string) => void
}) {
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
          onClick={() => message.info('新建群组请在群组页操作（待接入）')}
          style={{ textAlign: 'left' }}
        >
          新建群组
        </Button>
      </div>
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
          background: active ? '#eaf2ff' : 'transparent',
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
