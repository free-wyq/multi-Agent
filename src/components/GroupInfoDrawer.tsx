import { useEffect, useState } from 'react'
import {
  Button,
  Modal,
  Form,
  Input,
  Select,
  Segmented,
  message,
  Spin,
  Typography,
  Popconfirm,
  List,
  Tag,
  Drawer,
  Avatar,
  Divider,
} from 'antd'
import {
  PlusOutlined,
  RobotOutlined,
  DeleteOutlined,
  CloseCircleOutlined,
  EditOutlined,
  PushpinOutlined,
  FileOutlined,
  FolderOpenOutlined,
  DownOutlined,
  RightOutlined,
  BulbOutlined,
} from '@ant-design/icons'
import {
  groupApi,
  messageApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
  type GroupFile,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import MemberCapabilityOverview from './MemberCapabilityOverview'
import './GroupInfoDrawer.css'

const { Text } = Typography

/** 获取成员显示名（从 GroupPage 抽出，逻辑不变）。 */
function getMemberDisplayName(member: GroupMember): string {
  return member.alias || member.agent_name
}

/** 格式化文件大小（从 GroupPage 抽出，逻辑不变）。 */
function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(2)} MB`
  return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/** Drawer 内成员条目（含 isCoordinator 标记，群主也入列展示能力）。
 *  L2-02：MemberCapabilityOverview 已提取为独立组件，DrawerMemberItem 类型随之导出复用。 */
interface DrawerMemberItem extends GroupMember {
  isCoordinator?: boolean
}

interface GroupInfoDrawerProps {
  /** 抽屉开关（由父 ChatPage 持有，ChatPanel onOpenInfo 触发）。 */
  open: boolean
  /** 关闭回调。 */
  onClose: () => void
  /** 当前群组（null/未选群时抽屉内容不渲染）。 */
  group: Group | null
  /** 当前群组 id。 */
  groupId: string | null
  /** 当前群成员（父加载后透传，能力概况 + 成员列表渲染用）。 */
  members: GroupMember[]
  /** 成员加载中态。 */
  membersLoading?: boolean
  /** 全部智能体（加成员候选 + 能力概况 + 群主解析）。 */
  agents: AgentDefinition[]
  /** 群组/成员变更后通知父刷新（create/update/delete/member 增删后调用）。 */
  onChanged?: () => void
}

/**
 * L2-01 GroupInfoDrawer：群信息抽屉 + 群管理 Modal 集合。
 *
 * 从 GroupPage(1507L) 整体拆出「群信息 Drawer + 添加成员 Modal + 编辑群信息 Modal」+
 * 全部群管理 handler。GroupPage 的聊天主区已由 ChatPanel 承接（SH-04），本组件只收敛
 * 群管理职能——单一职责，ChatPage 持有 open 态、ChatPanel onOpenInfo 触发（L2-03 接通）。
 *
 * 承载内容（与 GroupPage 原 Drawer 1:1，零功能丢弃）：
 *  - 群信息头：大头像 + 群名 + 描述
 *  - 群公告（占位）
 *  - Leader 指挥策略展示（group.config.leader_strategy）+ 修改入口
 *  - 群共享文件（groupApi.listFiles，可折叠，文件类型图标）
 *  - 成员列表（含群主 Tag + 单个移除 + 全部移除 + 添加入口）
 *  - 成员能力概况（MemberCapabilityOverview，L2-02 提取为独立组件）
 *  - 编辑群信息 / 清空聊天记录 / 删除群组
 *  - 添加成员 Modal（候选=未入群智能体）
 *  - 编辑群信息 Modal（群名/描述/换群主/Leader策略）
 *
 * 新建群组 Modal + handleCreate + handleGenerateNameDesc 仍留 ChatPage（plan 功能保留
 * 对照表：新建入口在 SessionList 顶部，非抽屉触发，且 ChatPage 已有可用实现，不搬动
 * 避免无谓风险——零功能丢弃原则下保留更稳妥）。
 *
 * 数据自洽：
 *  - groupFiles 由本组件按 open+groupId 加载（群信息抽屉专属数据，开门才需，不污染父
 *    ChatPage）；skillNameMap/mcpNameMap 由 MemberCapabilityOverview 组件内聚加载。
 *    members/agents 由父透传（ChatPage 本就加载，复用）。
 *  - 群组切换：删群 → context.setGroupId(null)；其余变更 → onChanged() 通知父刷新。
 */
export default function GroupInfoDrawer({
  open,
  onClose,
  group,
  groupId,
  members,
  membersLoading,
  agents,
  onChanged,
}: GroupInfoDrawerProps) {
  const { setGroupId } = useBusEventContext()

  // ── 添加成员 Modal ──
  const [addMemberOpen, setAddMemberOpen] = useState(false)
  const [addMemberForm] = Form.useForm()

  // ── 群设置 Modal ──
  const [groupSettingsOpen, setGroupSettingsOpen] = useState(false)
  const [groupSettingsForm] = Form.useForm()

  // ── 群共享文件 ──
  const [groupFiles, setGroupFiles] = useState<GroupFile[]>([])
  const [filesLoading, setFilesLoading] = useState(false)
  const [filesExpanded, setFilesExpanded] = useState(true)

  // L2-02：skillNameMap/mcpNameMap 加载逻辑移入 MemberCapabilityOverview 组件内聚，
  // 本组件不再持有——能力盘的映射与展示是同一关注点，独立组件自给自足更内聚。

  // 群文件：抽屉开 + 有 groupId 时加载（开门才需，避免常驻轮询）
  useEffect(() => {
    if (!open || !groupId) {
      setGroupFiles([])
      return
    }
    setFilesLoading(true)
    groupApi
      .listFiles(groupId)
      .then(setGroupFiles)
      .catch(() => setGroupFiles([]))
      .finally(() => setFilesLoading(false))
  }, [open, groupId])

  // 群主信息
  const coordinatorAgent = group ? agents.find((a) => a.id === group.coordinator_id) : null

  // Drawer 内用的完整成员数据源（含群主）
  const drawerMembers: DrawerMemberItem[] = [
    ...(coordinatorAgent
      ? [{
          id: 'coordinator',
          agent_id: coordinatorAgent.id,
          group_id: groupId || '',
          alias: null,
          joined_at: '',
          agent_name: coordinatorAgent.name,
          agent_role: coordinatorAgent.role,
          isCoordinator: true,
        }]
      : []),
    ...members
      .filter((m) => m.agent_id !== group?.coordinator_id)
      .map((m) => ({ ...m, isCoordinator: false })),
  ]

  // 已加入的成员 agent_id 集合（加成员候选排除）
  const existingMemberAgentIds = new Set(members.map((m) => m.agent_id))
  const availableAgents = agents.filter((a) => !existingMemberAgentIds.has(a.id))

  const handleAddMember = async (values: Record<string, unknown>) => {
    if (!groupId) return
    try {
      const agentId = values.agent_id as string
      // MT-06: 防止添加已入群成员（含群主）——uq_group_agent 唯一约束后端兜底，前端先校验
      // 给更友好的提示。已加入的 agent 从 availableAgents 选项里已排除，保留防御。
      const alreadyIn = members.some((m) => m.agent_id === agentId) || group?.coordinator_id === agentId
      if (alreadyIn) {
        message.warning('该智能体已在群组中')
        return
      }
      await groupApi.addMember(groupId, agentId, (values.alias as string) || undefined)
      message.success('添加成功')
      setAddMemberOpen(false)
      addMemberForm.resetFields()
      onChanged?.()
    } catch {
      message.error('添加失败')
    }
  }

  const handleRemoveMember = async (memberId: string) => {
    if (!groupId) return
    try {
      // MT-06: 群主不可移除——drawerMembers 把群主标 isCoordinator=true，渲染时不显移除按钮，
      // 这里再做一道防御：若误传群主 member id 则拒绝。移除群主走「编辑群信息」改 coordinator_id。
      const target = members.find((m) => m.id === memberId)
      if (target && group?.coordinator_id === target.agent_id) {
        message.warning('不能移除群主，请在「编辑群信息」中更换群主')
        return
      }
      await groupApi.removeMember(groupId, memberId)
      message.success('移除成功')
      onChanged?.()
    } catch {
      message.error('移除失败')
    }
  }

  // MT-06: 批量移除普通成员（保留群主），Popconfirm 二次确认防误删。
  const handleRemoveAllMembers = async () => {
    if (!groupId) return
    try {
      const removable = members.filter((m) => m.agent_id !== group?.coordinator_id)
      await Promise.all(
        removable.map((m) => groupApi.removeMember(groupId, m.id)),
      )
      message.success(`已移除 ${removable.length} 个成员`)
      onChanged?.()
    } catch {
      message.error('移除失败')
    }
  }

  // ── 群设置 ──

  const handleOpenGroupSettings = () => {
    if (!group) return
    // MT-03: 预填 Leader 指挥策略（group.config.leader_strategy，未设为空串）。
    const strategy = (group.config?.leader_strategy as string | undefined) ?? ''
    // 协作模式预填（config.collaboration_mode，未设兜底 centralized）。
    const mode = (group.config?.collaboration_mode as string | undefined) ?? 'centralized'
    groupSettingsForm.setFieldsValue({
      name: group.name,
      description: group.description,
      coordinator_id: group.coordinator_id,
      leader_strategy: strategy,
      collaboration_mode: mode,
    })
    setGroupSettingsOpen(true)
  }

  // MT-06: Leader 指令快捷修改——抽屉内「修改指挥策略」链接点击后，直接调群设置 Modal
  // 预填当前策略，用户改完点保存走 handleUpdateGroup 写 config.leader_strategy。
  const handleEditLeaderStrategy = () => {
    handleOpenGroupSettings()
  }

  const handleUpdateGroup = async (values: Record<string, unknown>) => {
    if (!groupId) return
    try {
      // MT-03: Leader 指挥策略写入 group.config.leader_strategy。后端 update_group 对 config
      // 做 key 级 merge（不整体替换），故把当前群已有 config 与新 leader_strategy 合并后整体
      // 传 config——保留共存键（如 auto_confirm），仅覆盖 leader_strategy。trim 后空串也写入。
      // 协作模式 collaboration_mode 同样走 key-merge——切换触发后端 recompile_group 重编译群图。
      const strategy = (values.leader_strategy as string | undefined)?.trim() ?? ''
      const mode = (values.collaboration_mode as string | undefined) || 'centralized'
      const mergedConfig: Record<string, unknown> = {
        ...(group?.config ?? {}),
        leader_strategy: strategy,
        collaboration_mode: mode,
      }
      await groupApi.update(groupId, {
        name: values.name as string | undefined,
        description: values.description as string | undefined,
        coordinator_id: values.coordinator_id as string | undefined,
        config: mergedConfig,
      })
      message.success('更新成功')
      setGroupSettingsOpen(false)
      onChanged?.()
    } catch (e) {
      // MT-06: 后端对「设非成员为群主」返回 409，给出可读提示而非裸状态码。
      const msg = e instanceof Error ? e.message : '更新失败'
      if (msg.includes('409')) {
        message.error('新群主必须是该群组的现有成员，请先添加为成员再设为群主')
      } else {
        message.error(msg)
      }
    }
  }

  const handleDeleteGroup = async () => {
    if (!groupId) return
    try {
      await groupApi.delete(groupId)
      message.success('删除成功')
      setGroupId(null)
      onClose()
      onChanged?.()
    } catch {
      message.error('删除失败')
    }
  }

  const handleClearMessages = async () => {
    if (!groupId) return
    try {
      await messageApi.clearByGroup(groupId)
      message.success('聊天记录已清空')
      onChanged?.()
    } catch {
      message.error('清空失败')
    }
  }

  return (
    <>
      <Drawer
        title="群信息"
        placement="right"
        open={open}
        onClose={onClose}
        width={320}
        styles={{ body: { padding: 0 } }}
      >
        {group && (
          <div style={{ padding: '16px 16px 0' }}>
            {/* 群信息头部 */}
            <div style={{ textAlign: 'center', padding: '12px 0 20px' }}>
              <div
                className="group-avatar-wrap"
                style={{ width: 64, height: 64, borderRadius: 8, margin: '0 auto' }}
              >
                <img
                  src="/group-avatar.png"
                  alt="群聊头像"
                  className="group-avatar-img"
                  style={{ width: 64, height: 64, borderRadius: 8 }}
                />
              </div>
              <div style={{ fontSize: 16, fontWeight: 600, marginTop: 12 }}>
                {group.name}
              </div>
              <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 4 }}>
                {group.description || '暂无描述'}
              </Text>
            </div>

            <Divider style={{ margin: '0' }} />

            {/* 群公告 */}
            <div style={{ padding: '12px 0' }}>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                <PushpinOutlined style={{ color: '#faad14' }} />
                群公告
              </div>
              <div style={{ fontSize: 13, color: '#999', background: '#f5f5f5', padding: '8px 12px', borderRadius: 4 }}>
                暂无公告
              </div>
            </div>

            <Divider style={{ margin: '0' }} />

            {/* MT-03: Leader 指挥策略展示（group.config.leader_strategy）。 */}
            <div style={{ padding: '12px 0' }}>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                <BulbOutlined style={{ color: '#722ed1' }} />
                Leader 指挥策略
              </div>
              <div
                style={{
                  fontSize: 13,
                  color: (group.config?.leader_strategy as string | undefined)
                    ? '#333'
                    : '#b0b0b0',
                  background: (group.config?.leader_strategy as string | undefined)
                    ? '#f6f0ff'
                    : '#f5f5f5',
                  border: (group.config?.leader_strategy as string | undefined)
                    ? '1px solid #d3adf7'
                    : '1px solid transparent',
                  padding: '8px 12px',
                  borderRadius: 4,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {(group.config?.leader_strategy as string | undefined)?.trim() || '未设置指挥策略'}
              </div>
              <Button
                type="link"
                size="small"
                style={{ padding: '4px 0', color: '#722ed1' }}
                onClick={handleEditLeaderStrategy}
              >
                修改指挥策略
              </Button>
            </div>

            <Divider style={{ margin: '0' }} />

            {/* 群共享文件 */}
            <div style={{ padding: '16px 12px', background: '#fafbfd', borderRadius: 8, margin: '12px 0' }}>
              <div
                style={{
                  fontSize: 14, fontWeight: 700, marginBottom: filesExpanded ? 12 : 0,
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  paddingLeft: 10, position: 'relative', cursor: 'pointer',
                }}
                onClick={() => setFilesExpanded(!filesExpanded)}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{
                    position: 'absolute', left: 0, top: 2, bottom: 2, width: 3,
                    borderRadius: 2, background: '#1677ff',
                  }} />
                  <FolderOpenOutlined style={{ color: '#1677ff', fontSize: 16 }} />
                  <span>群文件</span>
                  <span style={{ fontSize: 11, color: '#999', fontWeight: 400, marginLeft: 2 }}>
                    ({groupFiles.length})
                  </span>
                </div>
                <div style={{ color: '#999', fontSize: 12 }}>
                  {filesExpanded ? <DownOutlined /> : <RightOutlined />}
                </div>
              </div>

              {filesExpanded && (
                <>
                  {filesLoading ? (
                    <div style={{ textAlign: 'center', padding: 20 }}>
                      <Spin size="small" />
                    </div>
                  ) : groupFiles.length === 0 ? (
                    <div style={{
                      fontSize: 13, color: '#b0b0b0',
                      border: '1px dashed #d0d7de',
                      padding: '14px 16px',
                      borderRadius: 8, textAlign: 'center', display: 'flex',
                      alignItems: 'center', justifyContent: 'center', gap: 8,
                    }}>
                      <FileOutlined style={{ fontSize: 14, color: '#b0b0b0' }} />
                      群组暂无共享文件
                    </div>
                  ) : (
                    <div style={{
                      display: 'flex', flexDirection: 'column', gap: 4,
                      maxHeight: 280, overflowY: 'auto',
                      paddingRight: 4,
                    }}>
                      {groupFiles.map((file: GroupFile) => {
                        const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
                        const isCode = ['py', 'js', 'ts', 'tsx', 'css', 'html', 'json', 'yaml', 'sql', 'md'].includes(ext)
                        const isDoc = ['doc', 'docx', 'pdf', 'txt'].includes(ext)
                        const iconColor = isCode ? '#10b981' : isDoc ? '#f59e0b' : '#8c8c8c'
                        return (
                          <div
                            key={file.name}
                            style={{
                              display: 'flex', alignItems: 'center', gap: 10,
                              padding: '8px 10px', borderRadius: 6, cursor: 'default',
                              transition: 'background 0.18s ease',
                              flexShrink: 0,
                            }}
                            onMouseEnter={(e) => {
                              (e.currentTarget as HTMLDivElement).style.background = '#e6f4ff'
                            }}
                            onMouseLeave={(e) => {
                              (e.currentTarget as HTMLDivElement).style.background = 'transparent'
                            }}
                          >
                            <div style={{
                              width: 32, height: 32, borderRadius: 6,
                              background: isCode ? '#d1fae5' : isDoc ? '#fef3c7' : '#f0f0f0',
                              display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                            }}>
                              <FileOutlined style={{ color: iconColor, fontSize: 15 }} />
                            </div>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{
                                fontSize: 13, fontWeight: 500, color: '#1f2937',
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                              }}>
                                {file.name}
                              </div>
                              <div style={{ fontSize: 11, color: '#999', marginTop: 1 }}>
                                {formatFileSize(file.size)} · {new Date(file.modified_at).toLocaleDateString()}
                              </div>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </>
              )}
            </div>

            <Divider style={{ margin: '0' }} />
            <div style={{ padding: '12px 0' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>
                  成员 <span style={{ color: '#999', fontWeight: 400, fontSize: 13 }}>( {members.length + 1} )</span>
                </span>
                <div style={{ display: 'flex', gap: 4 }}>
                  {/* MT-06: 批量移除普通成员（保留群主），仅当有可移除成员时显示。 */}
                  {members.some((m) => m.agent_id !== group?.coordinator_id) && (
                    <Popconfirm
                      title="确认移除全部普通成员？群主保留。"
                      onConfirm={handleRemoveAllMembers}
                      okText="移除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                    >
                      <Button type="text" size="small" danger icon={<CloseCircleOutlined />}>
                        全部移除
                      </Button>
                    </Popconfirm>
                  )}
                  <Button
                    type="text"
                    size="small"
                    icon={<PlusOutlined />}
                    onClick={() => {
                      addMemberForm.resetFields()
                      setAddMemberOpen(true)
                    }}
                  >
                    添加
                  </Button>
                </div>
              </div>

              {membersLoading ? (
                <div style={{ textAlign: 'center', padding: 20 }}>
                  <Spin size="small" />
                </div>
              ) : (
                <List
                  size="small"
                  dataSource={drawerMembers}
                  renderItem={(item: DrawerMemberItem) => (
                    <List.Item
                      style={{ padding: '8px 0' }}
                      actions={
                        !item.isCoordinator
                          ? [
                              <Popconfirm
                                key="remove"
                                title="确认移除该成员？"
                                onConfirm={() => handleRemoveMember(item.id)}
                                okText="确认"
                                cancelText="取消"
                              >
                                <Button
                                  type="text"
                                  danger
                                  size="small"
                                  icon={<CloseCircleOutlined />}
                                />
                              </Popconfirm>,
                            ]
                          : undefined
                      }
                    >
                      <List.Item.Meta
                        avatar={
                          <Avatar
                            size="small"
                            icon={item.isCoordinator ? <PushpinOutlined /> : <RobotOutlined />}
                            style={{ background: item.isCoordinator ? '#722ed1' : '#1677ff', fontSize: 12 }}
                          />
                        }
                        title={
                          <span style={{ fontSize: 13 }}>
                            {getMemberDisplayName(item)}
                            {item.isCoordinator && (
                              <Tag color="purple" style={{ marginLeft: 4, fontSize: 10, lineHeight: '14px', padding: '0 4px' }}>
                                群主
                              </Tag>
                            )}
                          </span>
                        }
                        description={
                          <span style={{ fontSize: 11, color: '#999' }}>{item.agent_role}</span>
                        }
                      />
                    </List.Item>
                  )}
                />
              )}
            </div>

            {/* MT-05: 成员能力概况聚合展示。skillNameMap/mcpNameMap 由组件内聚加载。 */}
            <Divider style={{ margin: '0' }} />
            <MemberCapabilityOverview
              members={drawerMembers}
              agents={agents}
            />

            <Divider style={{ margin: '0' }} />
            <div style={{ padding: '16px 0' }}>
              <Button
                block
                icon={<EditOutlined />}
                onClick={handleOpenGroupSettings}
                style={{ marginBottom: 8 }}
              >
                编辑群信息
              </Button>
              <Popconfirm
                title="确定要清空该群组的聊天记录吗？此操作不可恢复。"
                onConfirm={handleClearMessages}
                okText="清空"
                okButtonProps={{ danger: true }}
                cancelText="取消"
              >
                <Button block style={{ marginBottom: 8 }}>
                  清空聊天记录
                </Button>
              </Popconfirm>
              <Popconfirm
                title="确定要删除该群组吗？此操作不可恢复。"
                onConfirm={handleDeleteGroup}
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
              >
                <Button block danger icon={<DeleteOutlined />}>
                  删除群组
                </Button>
              </Popconfirm>
            </div>
          </div>
        )}
      </Drawer>

      {/* ── 添加成员 Modal ── */}
      <Modal
        open={addMemberOpen}
        title="添加群成员"
        onCancel={() => {
          setAddMemberOpen(false)
          addMemberForm.resetFields()
        }}
        onOk={() => addMemberForm.submit()}
        destroyOnClose
      >
        <Form form={addMemberForm} layout="vertical" onFinish={handleAddMember}>
          {/* MT-06: 候选项只含未入群智能体（availableAgents 已排除群成员+群主）。 */}
          <Form.Item
            name="agent_id"
            label="选择智能体"
            rules={[{ required: true, message: '请选择要添加的智能体' }]}
          >
            <Select
              placeholder={availableAgents.length === 0 ? '没有可添加的智能体（全部已入群）' : '选择要添加的智能体'}
              options={availableAgents.map((a) => ({ value: a.id, label: `${a.name} (${a.role})` }))}
              notFoundContent={availableAgents.length === 0 ? '所有智能体已在本群或尚无智能体' : undefined}
              showSearch
              optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item name="alias" label="别名（可选）">
            <Input placeholder='群内的称呼，如"前端大神"' />
          </Form.Item>
        </Form>
      </Modal>

      {/* ── 群设置（从抽屉触发） ── */}
      <Modal
        open={groupSettingsOpen}
        title="编辑群信息"
        onCancel={() => setGroupSettingsOpen(false)}
        onOk={() => groupSettingsForm.submit()}
      >
        <Form form={groupSettingsForm} layout="vertical" onFinish={handleUpdateGroup}>
          <Form.Item name="name" label="群组名称">
            <Input />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
          {/* MT-06: 群主候选 = 现有成员（含当前群主）。换 Leader 要求新群主已是成员
              （后端 update_group 校验：非成员设群主 → 409），前端候选项限制为成员集。 */}
          <Form.Item
            name="coordinator_id"
            label="群主"
            tooltip="群主从现有成员中选择。更换群主不影响成员关系，原群主降为普通成员。"
          >
            <Select
              placeholder="选择群主（须为现有成员）"
              options={drawerMembers.map((m) => ({
                value: m.agent_id,
                label: `${m.agent_name}${m.isCoordinator ? '（当前群主）' : ''}`,
              }))}
            />
          </Form.Item>
          {/* MT-03: Leader 指挥策略写入 group.config.leader_strategy。 */}
          <Form.Item
            name="leader_strategy"
            label="Leader 指挥策略"
            tooltip="给群主的指挥要求，会作为硬约束注入群主决策提示词。如：注重代码质量，每步必须自测通过再交付；后端先行，前端在后。"
          >
            <Input.TextArea
              rows={3}
              placeholder="给群主的指挥要求（可选）。如：注重代码质量，每步必须自测通过再交付"
              maxLength={500}
              showCount
            />
          </Form.Item>
          {/* 协作模式 Segmented（Path C 后单聊是独立 ConversationEntity 不经此 Drawer，
              故此组件只在群聊场景渲染——group 存在即群聊，无需 single_chat 守卫）。
              中心化：群主主导，supervisor 子图拆计划派工。
              去中心化：纯 swarm，裸消息群主当首发（swarm default_active_agent），@群主合法 handoff。
              切换触发后端 recompile_group 重编译群图（做法 A 图级二选一）。 */}
            <Form.Item
              name="collaboration_mode"
              label="协作模式"
              tooltip="中心化：群主主导，supervisor 子图拆计划派工。去中心化：纯 swarm，裸消息群主当首发，@群主合法 handoff。切换后图重编译。"
            >
              <Segmented
                options={[
                  { label: '中心化', value: 'centralized' },
                  { label: '去中心化', value: 'decentralized' },
                ]}
              />
            </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
