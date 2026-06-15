import { useEffect, useRef, useState } from 'react'
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
} from 'antd'
import {
  PlusOutlined,
  SendOutlined,
  UserOutlined,
  RobotOutlined,
} from '@ant-design/icons'
import {
  agentApi,
  groupApi,
  messageApi,
  type AgentDefinition,
  type Group,
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
  const [sending, setSending] = useState(false)
  const [chatInput, setChatInput] = useState('')
  const chatEndRef = useRef<HTMLDivElement>(null)

  // WebSocket 实时消息
  const chatGroup = groups.find((g) => g.id === chatGroupId)
  const { logs } = useWebSocket(chatGroupId)

  // WebSocket 新消息追加到末尾
  useEffect(() => {
    if (logs.length === 0) return
    const lastLog = logs[logs.length - 1]
    setChatMessages((prev) => {
      // 用 id 去重：避免乐观追加的消息和 WS 推送的重复
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

  // 切换群组时加载消息
  useEffect(() => {
    if (chatGroupId) {
      loadMessages(chatGroupId)
    }
  }, [chatGroupId])

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

    // 乐观追加：立即在聊天区显示用户消息
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
      // HTTP 已返回，用户消息已通过乐观追加显示
      // coordinator 的回复会通过 WS 推送，由 useEffect(logs) 自动追加
    } catch {
      // 发送失败，移除乐观消息并恢复输入框
      setChatMessages((prev) => prev.filter((m) => m.id !== tempId))
      setChatInput(content)
      message.error('发送失败')
    }
  }

  // ── 渲染 ──

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 112px)', overflow: 'hidden' }}>
      {/* ── 左侧群组栏 ── */}
      <div
        style={{
          width: 280,
          flexShrink: 0,
          borderRight: '1px solid #f0f0f0',
          display: 'flex',
          flexDirection: 'column',
          background: '#fff',
        }}
      >
        {/* 搜索/新建栏 */}
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

      {/* ── 右侧对话区 ── */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          background: '#fafafa',
          overflow: 'hidden',
        }}
      >
        {/* 聊天头部 */}
        {chatGroup && (
          <div
            style={{
              padding: '12px 20px',
              borderBottom: '1px solid #f0f0f0',
              background: '#fff',
              flexShrink: 0,
            }}
          >
            <Text strong style={{ fontSize: 15 }}>{chatGroup.name}</Text>
            {chatGroup.description && (
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 13 }}>
                — {chatGroup.description}
              </Text>
            )}
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
                    {msg.content || (
                      <Text type="secondary" italic>（{msg.type}）</Text>
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
          <div style={{ padding: '12px 16px', borderTop: '1px solid #f0f0f0', background: '#fff', flexShrink: 0 }}>
            <div style={{ display: 'flex', gap: 8 }}>
              <Input
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onPressEnter={handleSendMessage}
                placeholder="输入消息..."
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

      {/* 新建群组 */}
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
          <Form.Item name="coordinator_id" label="群主">
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
    </div>
  )
}
