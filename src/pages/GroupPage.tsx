import { useEffect, useRef, useState, useCallback, type ReactNode } from 'react'
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
  FileOutlined,
  FolderOpenOutlined,
  DownOutlined,
  RightOutlined,
  BulbOutlined,
  ToolOutlined,
  ApiOutlined,
} from '@ant-design/icons'
import { agentApi, groupApi,
  messageApi,
  skillApi,
  mcpApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
  type GroupFile,
  type Message,
  type Skill,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import PlanConfirmCard from '../components/PlanConfirmCard'
import StopTaskButton from '../components/StopTaskButton'
import './GroupPage.css'

const { Text } = Typography

/** 获取智能体角色主题色 */
function getAgentColor(id: string, agents: AgentDefinition[]): string {
  const ROLE_COLORS: Record<string, string> = {
    '后端开发工程师': '#6366f1',
    '前端开发工程师': '#06b6d4',
    '测试工程师': '#f59e0b',
    'DevOps 工程师': '#10b981',
    '产品经理': '#f43f5e',
    '自定义': '#8b5cf6',
  }
  const agent = agents.find((a) => a.id === id)
  return agent ? (ROLE_COLORS[agent.role] ?? '#8b5cf6') : '#722ed1'
}

/** 聊天气泡头像 */
function ChatAvatar({ id, agents }: { id: string; agents: AgentDefinition[] }) {
  /* 基于 id 哈希生成随机延迟和周期，让每个智能体呼吸节奏不同 */
  const hash = id.split('').reduce((a, c) => a + c.charCodeAt(0), 0)
  const ringDelay = (hash % 3000)
  const ringDuration = 2500 + (hash % 7) * 200
  const bobDelay = (hash >> 4) % 4000
  const bobDuration = 3000 + (hash >> 3) % 5 * 300

  if (id === 'user') {
    return (
      <div className="chat-avatar chat-avatar--user">
        <UserOutlined style={{ fontSize: 16, color: '#1677ff' }} />
      </div>
    )
  }
  const color = id === 'coordinator' || id === 'broadcast' ? '#722ed1' : getAgentColor(id, agents)
  return (
    <div className="chat-avatar" style={{ borderColor: color }}>
      <img
        src="/robot-avatar.png"
        alt=""
        className="chat-avatar-img"
        style={{ animationDelay: `${bobDelay}ms`, animationDuration: `${bobDuration}ms` }}
      />
      <span
        className="chat-avatar-ring"
        style={{ borderColor: color, animationDelay: `${ringDelay}ms`, animationDuration: `${ringDuration}ms` }}
      />
    </div>
  )
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

/** 格式化文件大小 */
function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(2)} MB`
  return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/** MT-05: Drawer 内成员条目（含 isCoordinator 标记，群主也入列展示能力）。 */
interface DrawerMemberItem extends GroupMember {
  isCoordinator?: boolean
}

/**
 * MT-05: 成员能力概况（技能/工具）聚合展示组件。
 *
 * 把群主 + 成员各自的能力栈归类汇总成几行（图标 + 标题 + Tag 列表），让用户在
 * 群信息抽屉一眼看到团队整体能力盘，而不必逐个点开 AgentPage 查：
 *  - 角色技能：agent.skills + agent.extra_skills（去重，角色身份自带的能力）
 *  - 已挂载技能：agent.mounted_skills（技能 id，经 skillNameMap 解析为可读技能名）
 *  - 可用工具：agent.allowed_tools（工具白名单，AG-05）
 *  - 禁用工具：agent.denied_tools（工具黑名单，前缀「禁:」与可用区分）
 *  - MCP 工具源：agent.mounted_mcp（MCP 连接 id，经 mcpNameMap 解析为可读连接名）
 *
 * 聚合规则：跨成员去重（同一技能多人挂载只显示一次，反映「团队级」能力盘）。
 * 空能力的类别不渲染该行；全部为空时显示占位「暂无能力配置」。
 */
function MemberCapabilityOverview({
  members,
  agents,
  skillNameMap,
  mcpNameMap,
}: {
  members: DrawerMemberItem[]
  agents: AgentDefinition[]
  skillNameMap: Record<string, string>
  mcpNameMap: Record<string, string>
}) {
  const memberAgentIds = new Set(members.map((m) => m.agent_id))
  const rosterAgents = agents.filter((a) => memberAgentIds.has(a.id))

  const roleSkills = Array.from(
    new Set(rosterAgents.flatMap((a) => [...(a.skills ?? []), ...(a.extra_skills ?? [])])),
  )
  const mountedSkillNames = Array.from(
    new Set(rosterAgents.flatMap((a) => a.mounted_skills ?? [])),
  ).map((id) => skillNameMap[id] ?? id)
  const allowedTools = Array.from(
    new Set(rosterAgents.flatMap((a) => a.allowed_tools ?? [])),
  )
  const deniedTools = Array.from(
    new Set(rosterAgents.flatMap((a) => a.denied_tools ?? [])),
  )
  const mountedMcpNames = Array.from(
    new Set(rosterAgents.flatMap((a) => a.mounted_mcp ?? [])),
  ).map((id) => mcpNameMap[id] ?? id)

  const sections: Array<{
    key: string
    icon: ReactNode
    title: string
    items: string[]
    color: string
    tagColor: 'purple' | 'geekblue' | 'green' | 'red' | 'orange'
    prefix?: string
  }> = [
    { key: 'role', icon: <BulbOutlined />, title: '角色技能', items: roleSkills, color: '#722ed1', tagColor: 'purple' as const },
    { key: 'mounted', icon: <ToolOutlined />, title: '已挂载技能', items: mountedSkillNames, color: '#1677ff', tagColor: 'geekblue' as const },
    { key: 'allowed', icon: <ApiOutlined />, title: '可用工具', items: allowedTools, color: '#52c41a', tagColor: 'green' as const },
    { key: 'denied', icon: <ToolOutlined />, title: '禁用工具', items: deniedTools, color: '#ff4d4f', tagColor: 'red' as const, prefix: '禁:' },
    { key: 'mcp', icon: <ApiOutlined />, title: 'MCP 工具源', items: mountedMcpNames, color: '#fa8c16', tagColor: 'orange' as const },
  ].filter((s) => s.items.length > 0)

  return (
    <div style={{ padding: '12px 0' }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
        <ApiOutlined style={{ color: '#1677ff' }} />
        成员能力概况
      </div>
      {sections.length === 0 ? (
        <div style={{ fontSize: 12, color: '#b0b0b0', background: '#f5f5f5', padding: '8px 12px', borderRadius: 4 }}>
          暂无能力配置
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {sections.map((sec) => (
            <div key={sec.key}>
              <div style={{ fontSize: 12, color: '#666', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: sec.color }}>{sec.icon}</span>
                {sec.title}
                <span style={{ color: '#bbb' }}>({sec.items.length})</span>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, paddingLeft: 18 }}>
                {sec.items.map((s, i) => (
                  <Tag key={`${sec.key}-${s}-${i}`} color={sec.tagColor} style={{ margin: 0, fontSize: 11, lineHeight: '18px', padding: '0 6px' }}>
                    {sec.prefix ? `${sec.prefix}${s}` : s}
                  </Tag>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function GroupPage() {
  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  // MT-05: 技能 id → name 映射，用于成员能力概况解析 mounted_skills 为可读名。
  const [skillNameMap, setSkillNameMap] = useState<Record<string, string>>({})
  // MT-05: MCP 连接 id → name 映射，用于成员能力概况解析 mounted_mcp 为可读连接名。
  const [mcpNameMap, setMcpNameMap] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [createForm] = Form.useForm()

  // ── 聊天状态 ──
  // WS-04：群组 id 改由 BusEventContext（App 顶层 provider）持有，本页消费共享 WS 状态。
  // chatGroupId 来自全局 active group，setChatGroupId 切全局聚焦群组（provider 重订阅 WS）。
  const { groupId: chatGroupId, setGroupId: setChatGroupId, logs, plan, agentStatuses } =
    useBusEventContext()
  const [chatMessages, setChatMessages] = useState<Message[]>([])
  const [chatLoading, setChatLoading] = useState(false)
  const [sending, setSending] = useState(false)
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
  // MT-04: 自动生成团队名称/描述的 loading 态（LLM 调用耗时数秒，禁用按钮防重复点击）
  const [genNameLoading, setGenNameLoading] = useState(false)

  // ── 群共享文件 ──
  const [groupFiles, setGroupFiles] = useState<GroupFile[]>([])
  const [filesLoading, setFilesLoading] = useState(false)
  const [filesExpanded, setFilesExpanded] = useState(true)

  // ── @mention 自动补全 ──
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState('')
  const [mentionIndex, setMentionIndex] = useState(0)
  const inputRef = useRef<InputRef | null>(null)
  const [inputCursor, setInputCursor] = useState(0)

  const chatGroup = groups.find((g) => g.id === chatGroupId)

  // WS-04：logs/plan/agentStatuses 来自 BusEventContext（全应用共享一条 WS），不再本地订阅。
  // （原 `const { logs, plan, agentStatuses } = useBusEvent(chatGroupId)` 已上移到 context 解构。）

  // PL-11：当前群组中正在 executing 的智能体（用于群聊头部展示停止按钮）。
  // 只取第一个 executing 且有 current_task_id 的——群聊页聚焦对话流，停止入口给最显眼的一个
  // 执行中智能体即可，多 worker 并行执行时在监控页有逐 worker 停止按钮。
  const executingAgent = chatGroupId
    ? Object.values(agentStatuses).find(
        (a) => a.status === 'executing' && a.current_task_id,
      )
    : undefined

  // 计划含 pending 步骤 → 展示计划确认卡片（M12-PL02）
  const showPlanCard =
    !!chatGroupId &&
    !!plan &&
    plan.length > 0 &&
    plan.some((s) => s.status === 'pending')

  // 新消息追加到末尾（跳过用户自己发的，已由乐观更新处理）
  useEffect(() => {
    if (logs.length === 0) return
    const lastLog = logs[logs.length - 1]
    if (lastLog.agentId === 'user') return
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
      // MT-05: 并行拉群组/智能体/技能/MCP，技能与 MCP 建 id→name 映射供成员能力概况
      // 解析 mounted_skills / mounted_mcp（agent 挂载的技能/MCP id）为可读名。
      const [gData, aData, skillList, mcpList] = await Promise.all([
        groupApi.list(),
        agentApi.list(),
        skillApi.list(),
        mcpApi.list(),
      ])
      setGroups(gData)
      setAgents(aData)
      const sm: Record<string, string> = {}
      skillList.forEach((s: Skill) => { sm[s.id] = s.name })
      setSkillNameMap(sm)
      const mm: Record<string, string> = {}
      mcpList.forEach((c) => { mm[c.id] = c.name })
      setMcpNameMap(mm)
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

  // 切换群组时加载消息、成员和文件
  useEffect(() => {
    if (chatGroupId) {
      loadMessages(chatGroupId)
      loadMembers(chatGroupId)
      loadGroupFiles(chatGroupId)
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

  const loadGroupFiles = async (groupId: string) => {
    setFilesLoading(true)
    try {
      const data = await groupApi.listFiles(groupId)
      setGroupFiles(data)
    } catch {
      setGroupFiles([])
    } finally {
      setFilesLoading(false)
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

  // MT-04: 根据已选群主 + 成员自动生成团队名称和描述，回填表单供用户审核编辑。
  // 选了群主或成员才允许触发（至少有一个 roster 上下文 LLM 才有生成依据）；
  // 生成后回填 name/description（不覆盖用户已填的非空值会被 .then 后直接 set 覆盖，
  // 故这里直接覆盖——「自动生成」语义就是让用户拿到建议后自行微调）。
  const handleGenerateNameDesc = async () => {
    const values = createForm.getFieldsValue(true)
    const coordinatorId = values.coordinator_id as string | undefined
    const memberIds = (values.members as string[]) ?? []
    if (!coordinatorId && memberIds.length === 0) {
      message.warning('请先选择群主或成员，再自动生成')
      return
    }
    setGenNameLoading(true)
    try {
      const result = await groupApi.generateNameDesc(coordinatorId, memberIds)
      createForm.setFieldsValue({
        name: result.name,
        description: result.description,
      })
      message.success('已生成团队名称和描述，可按需修改')
    } catch (e) {
      message.error(e instanceof Error ? e.message : '生成失败')
    } finally {
      setGenNameLoading(false)
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
    setSending(true)
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
      const sent = await messageApi.send({
        group_id: chatGroupId,
        sender_id: 'user',
        receiver_id: 'broadcast',
        type: 'user_input',
        content,
      })
      // ✅ 乐观更新替换：如果 WS 已经插入过真实消息，只删 temp；否则替换
      setChatMessages((prev) => {
        const alreadyExists = prev.some((m) => m.id === sent.id)
        if (alreadyExists) return prev.filter((m) => m.id !== tempId)
        return prev.map((m) => (m.id === tempId ? sent : m))
      })
    } catch {
      setChatMessages((prev) => prev.filter((m) => m.id !== tempId))
      setChatInput(content)
      message.error('发送失败')
    } finally {
      setSending(false)
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
    if (e.key === 'Enter') {
      // @ 补全开着且有候选 → 选择候选，不发送
      if (mentionOpen && mentionCandidates.length > 0) {
        e.preventDefault()
        const candidate = mentionCandidates[mentionIndex]
        if (candidate) insertMention(candidate)
        return
      }
      // 否则直接发送（替代 onPressEnter，避免 onPressEnter 在 onKeyDown 之前触发的时序问题）
      e.preventDefault()
      handleSendMessage()
      return
    }
    if (!mentionOpen) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setMentionIndex((idx) => (idx + 1) % mentionCandidates.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setMentionIndex((idx) => (idx - 1 + mentionCandidates.length) % mentionCandidates.length)
    } else if (e.key === 'Escape') {
      setMentionOpen(false)
    }
  }

  // ── 群成员管理 ──

  const handleAddMember = async (values: Record<string, unknown>) => {
    if (!chatGroupId) return
    try {
      const agentId = values.agent_id as string
      // MT-06: 防止添加已入群成员（含群主）——uq_group_agent 唯一约束在后端兜底，
      // 前端先校验给出更友好的提示，避免触发 500/409 后才报错。已加入的 agent
      // 从 availableAgents 选项里已排除，但保留此防御（防御性编程，与后端约束呼应）。
      const alreadyIn = members.some((m) => m.agent_id === agentId)
        || chatGroup?.coordinator_id === agentId
      if (alreadyIn) {
        message.warning('该智能体已在群组中')
        return
      }
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
      // MT-06: 群主不可移除——drawerMembers 把群主标 isCoordinator=true，渲染时不
      // 显示移除按钮（见 List.Item actions），这里再做一道防御：若误传群主 member id
      // 则拒绝并提示。移除群主应走「编辑群信息」改 coordinator_id，不是删成员。
      const target = members.find((m) => m.id === memberId)
      if (target && chatGroup?.coordinator_id === target.agent_id) {
        message.warning('不能移除群主，请在「编辑群信息」中更换群主')
        return
      }
      await groupApi.removeMember(chatGroupId, memberId)
      message.success('移除成功')
      loadMembers(chatGroupId)
    } catch {
      message.error('移除失败')
    }
  }

  // MT-06: Leader 指令快捷修改——抽屉内「Leader 指挥策略」展示块的「修改指挥策略」
  // 链接点击后，直接调群设置 Modal 预填当前策略（复用 MT-03 的 handleOpenGroupSettings，
  // 预填 leader_strategy），用户改完点保存走 handleUpdateGroup 写 config.leader_strategy。
  // 不做内联编辑——策略是多行文本，Modal 的 TextArea + showCount 比内联输入更可控，
  // 且复用现有群设置表单避免重复实现一套校验/提交逻辑（最稳妥最易维护）。
  const handleEditLeaderStrategy = () => {
    handleOpenGroupSettings()
  }

  // MT-06: 批量移除成员——群成员管理有时需清空一批成员再重组，逐个点 Popconfirm
  // 太繁琐。提供「移除全部普通成员」入口（保留群主），用 Popconfirm 二次确认防误删。
  const handleRemoveAllMembers = async () => {
    if (!chatGroupId) return
    try {
      const removable = members.filter(
        (m) => m.agent_id !== chatGroup?.coordinator_id,
      )
      await Promise.all(
        removable.map((m) => groupApi.removeMember(chatGroupId, m.id)),
      )
      message.success(`已移除 ${removable.length} 个成员`)
      loadMembers(chatGroupId)
    } catch {
      message.error('移除失败')
    }
  }

  // ── 群设置 ──

  const handleOpenGroupSettings = () => {
    if (!chatGroup) return
    // MT-03: 预填 Leader 指挥策略（group.config.leader_strategy，未设为空串）。
    const strategy =
      (chatGroup.config?.leader_strategy as string | undefined) ?? ''
    groupSettingsForm.setFieldsValue({
      name: chatGroup.name,
      description: chatGroup.description,
      coordinator_id: chatGroup.coordinator_id,
      leader_strategy: strategy,
    })
    setGroupSettingsOpen(true)
  }

  const handleUpdateGroup = async (values: Record<string, unknown>) => {
    if (!chatGroupId) return
    try {
      // MT-03: Leader 指挥策略写入 group.config.leader_strategy。后端 update_group
      // 对 config 做 key 级 merge（不整体替换），故这里把当前群已有 config 与新的
      // leader_strategy 合并后整体传 config——保留共存键（如 auto_confirm），仅覆盖
      // leader_strategy。trim 后空串也写入（语义：清空策略，coordinator 不再注入）。
      const strategy = (values.leader_strategy as string | undefined)?.trim() ?? ''
      const mergedConfig: Record<string, unknown> = {
        ...(chatGroup?.config ?? {}),
        leader_strategy: strategy,
      }
      await groupApi.update(chatGroupId, {
        name: values.name as string | undefined,
        description: values.description as string | undefined,
        coordinator_id: values.coordinator_id as string | undefined,
        config: mergedConfig,
      })
      message.success('更新成功')
      setGroupSettingsOpen(false)
      fetchData()
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

  const handleClearMessages = async () => {
    if (!chatGroupId) return
    try {
      await messageApi.clearByGroup(chatGroupId)
      message.success('聊天记录已清空')
      loadMessages(chatGroupId)
    } catch {
      message.error('清空失败')
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
    ...members
      .filter((m) => m.agent_id !== chatGroup?.coordinator_id)
      .map((m) => ({ ...m, isCoordinator: false })),
  ]

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>
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
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
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
                <div className="group-avatar-list-wrap" style={{ flexShrink: 0 }}>
                  <img
                    src="/group-avatar.png"
                    alt=""
                    className="group-avatar-list"
                  />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
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
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {/* PL-11：群聊头部停止按钮——有智能体正在 executing 且有 current_task_id 时展示 */}
              {executingAgent && chatGroupId && (
                <StopTaskButton
                  taskId={executingAgent.current_task_id!}
                  groupId={chatGroupId}
                  agentName={executingAgent.name}
                />
              )}
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
          {/* M12-PL02 计划确认卡片：plan 含 pending 步骤时展示于消息列表顶部 */}
          {showPlanCard && plan && chatGroupId && (
            <PlanConfirmCard groupId={chatGroupId} plan={plan} />
          )}
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
            chatMessages.map((msg) => {
              const isUser = msg.sender_id === 'user'
              return (
                <div
                  key={msg.id}
                  className="chat-msg"
                  style={{ flexDirection: isUser ? 'row-reverse' : 'row' }}
                >
                  {/* 头像 */}
                  <ChatAvatar id={msg.sender_id} agents={agents} />

                  {/* 消息气泡 */}
                  <div className="chat-bubble-wrap">
                    <div className={`chat-sender-name ${isUser ? 'chat-sender-name--right' : ''}`}>
                      <SenderName id={msg.sender_id} agents={agents} />
                    </div>
                    <div className={`chat-bubble ${isUser ? 'chat-bubble--self' : 'chat-bubble--other'}`}>
                      {isUser ? (
                        msg.content
                      ) : (
                        <HighlightMessage content={msg.content} members={members} />
                      )}
                    </div>
                    <div className={`chat-timestamp ${isUser ? 'chat-timestamp--right' : ''}`}>
                      {new Date(msg.created_at).toLocaleTimeString()}
                    </div>
                  </div>
                </div>
              )
            })
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

            {/* MT-03: Leader 指挥策略展示（group.config.leader_strategy）。
                让用户在群信息抽屉即可看到当前策略，点「修改」跳群设置 Modal 编辑。 */}
            <div style={{ padding: '12px 0' }}>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                <BulbOutlined style={{ color: '#722ed1' }} />
                Leader 指挥策略
              </div>
              <div
                style={{
                  fontSize: 13,
                  color: (chatGroup.config?.leader_strategy as string | undefined)
                    ? '#333'
                    : '#b0b0b0',
                  background: (chatGroup.config?.leader_strategy as string | undefined)
                    ? '#f6f0ff'
                    : '#f5f5f5',
                  border: (chatGroup.config?.leader_strategy as string | undefined)
                    ? '1px solid #d3adf7'
                    : '1px solid transparent',
                  padding: '8px 12px',
                  borderRadius: 4,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {(chatGroup.config?.leader_strategy as string | undefined)?.trim() || '未设置指挥策略'}
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
                  {/* 左侧蓝色竖条 accent line */}
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
                  {/* MT-06: 批量移除普通成员（保留群主），仅当有可移除成员时显示。
                      避免空群时还显一个无意义的「全部移除」按钮。 */}
                  {members.some((m) => m.agent_id !== chatGroup?.coordinator_id) && (
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

            {/* MT-05: 成员能力概况（技能/工具）聚合展示。把群主+成员各自的能力栈
                归类汇总——角色技能（skills+extra_skills）/ 已挂载技能（mounted_skills，
                解析为可读名）/ 工具权限（allowed_tools+denied_tools）/ MCP 工具
                （mounted_mcp 连接 id）。让用户在群信息抽屉一眼看到团队整体能力盘,
                不必逐个点开 AgentPage 查。空能力有占位,不渲染空块。 */}
            <Divider style={{ margin: '0' }} />
            <MemberCapabilityOverview
              members={drawerMembers}
              agents={agents}
              skillNameMap={skillNameMap}
              mcpNameMap={mcpNameMap}
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
          {/* MT-04: 自动生成团队名称和描述。选完群主/成员后点此按钮，后端 LLM
              据 roster 生成项目向团队名 + 一句话描述回填 name/description。 */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
            <Button
              type="link"
              icon={<BulbOutlined />}
              loading={genNameLoading}
              onClick={handleGenerateNameDesc}
              style={{ padding: '0 0' }}
            >
              自动生成名称和描述
            </Button>
          </div>
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
          {/* MT-06: 候选项只含未入群智能体（availableAgents 已排除群成员+群主），
              避免重复入群触发后端唯一约束。空时提示先去智能体页创建。 */}
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
              （后端 update_group 校验：非成员设群主 → 409），前端候选项限制为成员集
              让用户只能从已在群的人里选，与后端约束呼应，避免提交后才报错。 */}
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
          {/* MT-03: Leader 指挥策略写入 group.config.leader_strategy，
              coordinator 的 node_llm_decide 会读它注入 prompt（build_coordinator_prompt
              的「群主指挥策略（务必遵守）」段），影响 Leader 的拆解/派工决策。
              MT-06: 抽屉「修改指挥策略」链接也打开此表单预填策略，保存即写 config.leader_strategy
              （key 级 merge，保留 auto_confirm 等共存键）。 */}
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
        </Form>
      </Modal>
    </div>
  )
}
