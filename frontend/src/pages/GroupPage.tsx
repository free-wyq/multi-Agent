import { useEffect, useRef, useState, useCallback } from 'react'
import {
  Button,
  Modal,
  Form,
  Input,
  Select,
  message,
  Empty,
  Spin,
  Typography,
  Popconfirm,
  Tooltip,
  List,
  Tag,
  Drawer,
  Avatar,
  Divider,
  type InputRef,
} from 'antd'
import {
  PlusOutlined,
  SendOutlined,
  UserOutlined,
  RobotOutlined,
  SettingOutlined,
  DeleteOutlined,
  CloseCircleOutlined,
  EditOutlined,
  PushpinOutlined,
  TeamOutlined,
} from '@ant-design/icons'
import {
  agentApi,
  groupApi,
  messageApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
  type Message,
} from '../services/api'
import { useWebSocket } from '../hooks/useWebSocket'

const { Text } = Typography

/** 获取发送者头像/图标 */
function SenderIcon({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  if (id === 'user') return <UserOutlined style={{ fontSize: 20, color: '#1677ff' }} />
  if (id === 'coordinator' || id === 'broadcast') return <RobotOutlined style={{ fontSize: 20, color: '#722ed1' }} />
  const agent = agents.find((a) => a.id === id)
  return agent ? <RobotOutlined style={{ fontSize: 20, color: '#1677ff' }} /> : <UserOutlined style={{ fontSize: 20, color: '#999' }} />
}

/** 获取发送者显示名 */
function SenderName({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  if (id === 'user') return '用户'
  if (id === 'coordinator') return '群主(协调者)'
  if (id === 'broadcast') return '系统广播'
  const agent = agents.find((a) => a.id === id)
  return agent?.name ?? id.slice(0, 8) + '...'
}

/** 高亮 @mention 的消息内容 */
function HighlightMessage({ content, members }: { content: string | null; members: GroupMember[] }) {
  if (!content) return <Text type="secondary" italic>（空消息）</Text>
  const parts = content.split(/(@[^\s,，.。!！?？:：;；\n]+)/g)
  return (
    <span>
      {parts.map((part, i) => {
        if (part.startsWith('@')) {
          const name = part.slice(1)
          const isMember = members.some((m) => m.agent_name === name || m.alias === name)
          if (isMember) {
            return <Tag key={i} color="blue" style={{ margin: 0, padding: '0 4px', lineHeight: '18px' }}>{part}</Tag>
          }
        }
        return <span key={i}>{part}</span>
      })}
    </span>
  )
}

/** 获取成员显示名 */
function getMemberDisplayName(member: GroupMember) {
  return member.alias || member.agent_name
}

export default function GroupPage() {
  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [loading, setLoading] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [createForm] = Form.useForm()

  // ── 聊天状态 ──
  const [chatGroupId, setChatGroupId] = useState<string | null>(null)
  const [chatMessages, setChatMessages] = useState<Message[]>([])
  const [chatLoading, setChatLoading] = useState(false)
  const [sending] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const chatEndRef = useRef<HTMLDivElement>(null)

  // ── 群成员与群管理 ──
  const [members, setMembers] = useState<GroupMember[]>([])
  const [membersLoading, setMembersLoading] = useState(false)
  const [addMemberOpen, setAddMemberOpen] = useState(false)
  const [addMemberForm] = Form.useForm()
  const [groupSettingsOpen, setGroupSettingsOpen] = useState(false)
  const [groupSettingsForm] = Form.useForm()
  const [drawerOpen, setDrawerOpen] = useState(false)

  // ── @mention 自动补全 ──
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState('')
  const [mentionIndex, setMentionIndex] = useState(0)
  const inputRef = useRef<InputRef | null>(null)
  const [inputCursor, setInputCursor] = useState(0)

  const chatGroup = groups.find((g) => g.id === chatGroupId)

  // WebSocket 实时消息
  const { logs } = useWebSocket(chatGroupId)

  // WebSocket 新消息追加到末尾
  useEffect(() => {
    if (logs.length === 0) return
    const lastLog = logs[logs.length - 1]
    setChatMessages((prev) => {
      const wsMsgId = lastLog.id || `ws-${lastLog.timestamp}`
      if (prev.some((m) => m.id === wsMsgId)) return prev
      return [...prev, {
        id: wsMsgId,
        group_id: chatGroupId || '',
        task_id: lastLog.taskId || null,
        sender_id: lastLog.agentId,
        receiver_id: 'broadcast',
        type: 'log',
        content: lastLog.message,
        data: null,
        created_at: new Date(lastLog.timestamp).toISOString(),
      }]
    })
  }, [logs, chatGroupId])

  // 滚动到底部
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  const fetchData = async () => {
    setLoading(true)
    try {
      const [gData, aData] = await Promise.all([groupApi.list(), agentApi.list()])
      setGroups(gData)
      setAgents(aData)
      if (!chatGroupId && gData.length > 0) {
        setChatGroupId(gData[0].id)
      }
    } catch {
      message.error('获取数据失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [])

  // 切换群组时加载消息和成员
  useEffect(() => {
    if (chatGroupId) {
      loadMessages(chatGroupId)
      loadMembers(chatGroupId)
      setDrawerOpen(false)
    }
  }, [chatGroupId])

  const loadMembers = async (groupId: string) => {
    setMembersLoading(true)
    try {
      const data = await groupApi.listMembers(groupId)
      setMembers(data)
    } catch {
      setMembers([])
    } finally {
      setMembersLoading(false)
    }
  }

  // 群主信息
  const coordinatorAgent = chatGroup
    ? agents.find((a) => a.id === chatGroup.coordinator_id)
    : null

  const handleCreate = async (values: Record<string, unknown>) => {
    try {
      const group = await groupApi.create({
        name: values.name as string,
        coordinator_id: values.coordinator_id as string | undefined,
        description: values.description as string | undefined,
      })
      const selected: string[] = (values.members as string[]) ?? []
      await Promise.all(
        selected.map((agentId) => groupApi.addMember(group.id, agentId)),
      )
      message.success('创建成功')
      setCreateOpen(false)
      createForm.resetFields()
      fetchData()
    } catch {
      message.error('创建失败')
    }
  }

  // ── 聊天功能 ──

  const loadMessages = async (groupId: string) => {
    setChatLoading(true)
    try {
      const data = await messageApi.listByGroup(groupId)
      setChatMessages(data.reverse())
    } catch {
      setChatMessages([])
    } finally {
      setChatLoading(false)
    }
  }

  const handleSendMessage = async () => {
    if (!chatInput.trim() || !chatGroupId || sending) return
    const content = chatInput.trim()
    setChatInput('')
    setMentionOpen(false)

    const tempId = `temp-${Date.now()}`
    const optimisticMsg: Message = {
      id: tempId,
      group_id: chatGroupId,
      task_id: null,
      sender_id: 'user',
      receiver_id: 'broadcast',
      type: 'user_input',
      content,
      data: null,
      created_at: new Date().toISOString(),
    }
    setChatMessages((prev) => [...prev, optimisticMsg])

    try {
      await messageApi.send({
        group_id: chatGroupId,
        sender_id: 'user',
        receiver_id: 'broadcast',
        type: 'user_input',
        content,
      })
    } catch {
      setChatMessages((prev) => prev.filter((m) => m.id !== tempId))
      setChatInput(content)
      message.error('发送失败')
    }
  }

  // ── @mention 自动补全 ──
  const mentionCandidates = members.filter((m) =>
    getMemberDisplayName(m).toLowerCase().includes(mentionQuery.toLowerCase()),
  )

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value
    const cursor = e.target.selectionStart ?? value.length
    setChatInput(value)
    setInputCursor(cursor)

    // 检测光标前是否有未闭合的 @
    const beforeCursor = value.slice(0, cursor)
    const atMatch = beforeCursor.match(/@([^\s]*)$/)
    if (atMatch) {
      setMentionQuery(atMatch[1])
      setMentionOpen(true)
      setMentionIndex(0)
    } else {
      setMentionOpen(false)
    }
  }

  const insertMention = useCallback((member: GroupMember) => {
    const name = getMemberDisplayName(member)
    const beforeCursor = chatInput.slice(0, inputCursor)
    const afterCursor = chatInput.slice(inputCursor)
    const atIndex = beforeCursor.lastIndexOf('@')
    if (atIndex === -1) return

    const newValue = beforeCursor.slice(0, atIndex) + `@${name} ` + afterCursor
    setChatInput(newValue)
    setMentionOpen(false)

    // 恢复光标到插入后位置
    setTimeout(() => {
      const newCursor = atIndex + name.length + 2
      inputRef.current?.input?.setSelectionRange?.(newCursor, newCursor)
      inputRef.current?.focus()
    }, 0)
  }, [chatInput, inputCursor])

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!mentionOpen) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setMentionIndex((idx) => (idx + 1) % mentionCandidates.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setMentionIndex((idx) => (idx - 1 + mentionCandidates.length) % mentionCandidates.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const candidate = mentionCandidates[mentionIndex]
      if (candidate) insertMention(candidate)
    } else if (e.key === 'Escape') {
      setMentionOpen(false)
    }
  }

  // ── 群成员管理 ──

  const handleAddMember = async (values: Record<string, unknown>) => {
    if (!chatGroupId) return
    try {
      const agentId = values.agent_id as string
      await groupApi.addMember(chatGroupId, agentId, (values.alias as string) || undefined)
      message.success('添加成功')
      setAddMemberOpen(false)
      addMemberForm.resetFields()
      loadMembers(chatGroupId)
    } catch {
      message.error('添加失败')
    }
  }

  const handleRemoveMember = async (memberId: string) => {
    if (!chatGroupId) return
    try {
      await groupApi.removeMember(chatGroupId, memberId)
      message.success('移除成功')
      loadMembers(chatGroupId)
    } catch {
      message.error('移除失败')
    }
  }

  // ── 群设置 ──

  const handleOpenGroupSettings = () => {
    if (!chatGroup) return
    groupSettingsForm.setFieldsValue({
      name: chatGroup.name,
      description: chatGroup.description,
      coordinator_id: chatGroup.coordinator_id,
    })
    setGroupSettingsOpen(true)
  }

  const handleUpdateGroup = async (values: Record<string, unknown>) => {
    if (!chatGroupId) return
    try {
      await groupApi.update(chatGroupId, {
        name: values.name as string | undefined,
        description: values.description as string | undefined,
        coordinator_id: values.coordinator_id as string | undefined,
      })
      message.success('更新成功')
      setGroupSettingsOpen(false)
      fetchData()
    } catch {
      message.error('更新失败')
    }
  }

  const handleDeleteGroup = async () => {
    if (!chatGroupId) return
    try {
      await groupApi.delete(chatGroupId)
      message.success('删除成功')
      setChatGroupId(null)
      setDrawerOpen(false)
      fetchData()
    } catch {
      message.error('删除失败')
    }
  }

  // 已加入的成员 agent_id 集合
  const existingMemberAgentIds = new Set(members.map((m) => m.agent_id))
  const availableAgents = agents.filter((a) => !existingMemberAgentIds.has(a.id))

  // Drawer 内用的完整成员数据源（含群主）
  const drawerMembers = [
    ...(coordinatorAgent
      ? [{
          id: 'coordinator',
          agent_id: coordinatorAgent.id,
          group_id: chatGroupId || '',
          alias: null,
          joined_at: '',
          agent_name: coordinatorAgent.name,
          agent_role: coordinatorAgent.role,
          isCoordinator: true,
        }]
      : []),
    ...members.map((m) => ({ ...m, isCoordinator: false })),
  ]

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 112px)', overflow: 'hidden' }}>
      {/* ── 左侧群组栏 ── */}
      <div
        style={{
          width: 240,
          flexShrink: 0,
          borderRight: '1px solid #f0f0f0',
          display: 'flex',
          flexDirection: 'column',
          background: '#fff',
        }}
      >
        {/* 新建栏 */}
        <div
          style={{
            padding: '8px 12px',
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            flexShrink: 0,
          }}
        >
          <Button
            type="text"
            icon={<PlusOutlined />}
            size="small"
            onClick={() => setCreateOpen(true)}
          >
            新建群组
          </Button>
        </div>

        {/* 群组列表 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '4px 8px' }}>
          {loading ? (
            <div style={{ textAlign: 'center', padding: 20 }}>
              <Spin size="small" />
            </div>
          ) : groups.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无群组"
              style={{ margin: '12px 0' }}
            />
          ) : (
            groups.map((g) => (
              <div
                key={g.id}
                onClick={() => setChatGroupId(g.id)}
                style={{
                  padding: '10px 12px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: chatGroupId === g.id ? '#e6f4ff' : 'transparent',
                  transition: 'background 0.2s',
                  marginBottom: 2,
                }}
                onMouseEnter={(e) => {
                  if (chatGroupId !== g.id)
                    (e.currentTarget as HTMLDivElement).style.background = '#f5f5f5'
                }}
                onMouseLeave={(e) => {
                  if (chatGroupId !== g.id)
                    (e.currentTarget as HTMLDivElement).style.background = 'transparent'
                }}
              >
                <div
                  style={{
                    fontWeight: chatGroupId === g.id ? 600 : 400,
                    fontSize: 14,
                    color: chatGroupId === g.id ? '#1677ff' : '#333',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {g.name}
                </div>
                {g.description && (
                  <div
                    style={{
                      fontSize: 12,
                      color: '#999',
                      marginTop: 2,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {g.description}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>

      {/* ── 中间对话区 ── */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          background: '#fafafa',
          overflow: 'hidden',
        }}
      >
        {/* 聊天头部 — 钉钉风格：标题 + 人数，右侧群信息按钮 */}
        {chatGroup && (
          <div
            style={{
              padding: '12px 20px',
              borderBottom: '1px solid #f0f0f0',
              background: '#fff',
              flexShrink: 0,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, overflow: 'hidden' }}>
              <Text strong style={{ fontSize: 15, flexShrink: 0 }}>
                {chatGroup.name}
              </Text>
              <Text type="secondary" style={{ fontSize: 13, flexShrink: 0 }}>
                ( {members.length + 1} )
              </Text>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <Tooltip title="群信息">
                <Button
                  type={drawerOpen ? 'primary' : 'text'}
                  icon={<SettingOutlined />}
                  size="small"
                  onClick={() => setDrawerOpen(!drawerOpen)}
                />
              </Tooltip>
            </div>
          </div>
        )}

        {/* 消息列表 */}
        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            padding: '16px 20px',
          }}
        >
          {!chatGroupId ? (
            <div style={{ textAlign: 'center', padding: 60 }}>
              <Empty description="请在左侧选择一个群组开始对话" />
            </div>
          ) : chatLoading ? (
            <div style={{ textAlign: 'center', padding: 40 }}>
              <Spin />
            </div>
          ) : chatMessages.length === 0 ? (
            <Empty description="暂无消息，开始对话吧" />
          ) : (
            chatMessages.map((msg) => (
              <div
                key={msg.id}
                style={{
                  display: 'flex',
                  gap: 10,
                  marginBottom: 16,
                  flexDirection: msg.sender_id === 'user' ? 'row-reverse' : 'row',
                }}
              >
                {/* 头像 */}
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: '50%',
                    background: msg.sender_id === 'user' ? '#e6f4ff' : '#f9f0ff',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  <SenderIcon id={msg.sender_id} agents={agents} />
                </div>

                {/* 消息气泡 */}
                <div style={{ maxWidth: '70%' }}>
                  <div
                    style={{
                      fontSize: 12,
                      color: '#999',
                      marginBottom: 2,
                      textAlign: msg.sender_id === 'user' ? 'right' : 'left',
                    }}
                  >
                    <SenderName id={msg.sender_id} agents={agents} />
                  </div>
                  <div
                    style={{
                      padding: '8px 12px',
                      borderRadius: 12,
                      background: msg.sender_id === 'user' ? '#1677ff' : '#f0f0f0',
                      color: msg.sender_id === 'user' ? '#fff' : '#333',
                      fontSize: 14,
                      lineHeight: 1.5,
                      wordBreak: 'break-word',
                    }}
                  >
                    {msg.sender_id === 'user' ? (
                      msg.content
                    ) : (
                      <HighlightMessage content={msg.content} members={members} />
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: '#bbb',
                      marginTop: 2,
                      textAlign: msg.sender_id === 'user' ? 'right' : 'left',
                    }}
                  >
                    {new Date(msg.created_at).toLocaleTimeString()}
                  </div>
                </div>
              </div>
            ))
          )}
          <div ref={chatEndRef} />
        </div>

        {/* 输入框 */}
        {chatGroupId && (
          <div style={{ padding: '12px 16px', borderTop: '1px solid #f0f0f0', background: '#fff', flexShrink: 0, position: 'relative' }}>
            {/* @mention 下拉列表 */}
            {mentionOpen && mentionCandidates.length > 0 && (
              <div
                style={{
                  position: 'absolute',
                  bottom: '100%',
                  left: 16,
                  marginBottom: 4,
                  background: '#fff',
                  border: '1px solid #f0f0f0',
                  borderRadius: 6,
                  boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
                  zIndex: 100,
                  maxHeight: 200,
                  overflowY: 'auto',
                  width: 220,
                }}
              >
                {mentionCandidates.map((m, idx) => (
                  <div
                    key={m.id}
                    onClick={() => insertMention(m)}
                    style={{
                      padding: '8px 12px',
                      cursor: 'pointer',
                      background: idx === mentionIndex ? '#e6f4ff' : '#fff',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <RobotOutlined style={{ color: '#1677ff' }} />
                    <div>
                      <div style={{ fontSize: 13 }}>{getMemberDisplayName(m)}</div>
                      <div style={{ fontSize: 11, color: '#999' }}>{m.agent_role}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <Input
                ref={inputRef}
                value={chatInput}
                onChange={handleInputChange}
                onKeyDown={handleInputKeyDown}
                onPressEnter={handleSendMessage}
                placeholder="输入消息... 使用 @ 点名成员"
                disabled={sending}
                size="large"
              />
              <Button
                type="primary"
                icon={<SendOutlined />}
                onClick={handleSendMessage}
                loading={sending}
                size="large"
              >
                发送
              </Button>
            </div>
          </div>
        )}
      </div>

      {/* ── 钉钉风格右侧抽屉：群信息 ── */}
      <Drawer
        title="群信息"
        placement="right"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={320}
        bodyStyle={{ padding: '0' }}
      >
        {chatGroup && (
          <div style={{ padding: '16px 16px 0' }}>
            {/* 群信息头部 */}
            <div style={{ textAlign: 'center', padding: '12px 0 20px' }}>
              <Avatar
                size={64}
                icon={<TeamOutlined />}
                shape="square"
                style={{ background: '#1677ff', borderRadius: 8, fontSize: 28 }}
              />
              <div style={{ fontSize: 16, fontWeight: 600, marginTop: 12 }}>
                {chatGroup.name}
              </div>
              <Text type="secondary" style={{ fontSize: 13, display: 'block', marginTop: 4 }}>
                {chatGroup.description || '暂无描述'}
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

            {/* 成员列表 */}
            <div style={{ padding: '12px 0' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>
                  成员 <span style={{ color: '#999', fontWeight: 400, fontSize: 13 }}>( {members.length + 1} )</span>
                </span>
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

              {membersLoading ? (
                <div style={{ textAlign: 'center', padding: 20 }}>
                  <Spin size="small" />
                </div>
              ) : (
                <List
                  size="small"
                  dataSource={drawerMembers}
                  renderItem={(item: GroupMember & { isCoordinator?: boolean }) => (
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

            <Divider style={{ margin: '0' }} />

            {/* 群管理操作 */}
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

      {/* ── 新建群组 ── */}
      <Modal
        open={createOpen}
        title="新建群组"
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={() => createForm.submit()}
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="name"
            label="群组名称"
            rules={[{ required: true, message: '请输入群组名称' }]}
          >
            <Input placeholder="如：商城订单项目" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="群组的用途描述（可选）" />
          </Form.Item>
          <Form.Item
            name="coordinator_id"
            label="群主"
            rules={[{ required: true, message: '请选择群主' }]}
          >
            <Select
              placeholder="选择群主智能体（必选）"
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item name="members" label="成员">
            <Select
              mode="multiple"
              placeholder="选择群组成员"
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* ── 添加成员 ── */}
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
          <Form.Item
            name="agent_id"
            label="选择智能体"
            rules={[{ required: true, message: '请选择要添加的智能体' }]}
          >
            <Select
              placeholder="选择要添加的智能体"
              options={availableAgents.map((a) => ({ value: a.id, label: `${a.name} (${a.role})` }))}
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
          <Form.Item name="coordinator_id" label="群主">
            <Select
              placeholder="选择群主"
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
