import { useEffect, useState } from 'react'
import {
  Button,
  Modal,
  Form,
  Input,
  Select,
  Tag,
  message,
  Popconfirm,
  Tooltip,
  Badge,
} from 'antd'
import {
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  CodeOutlined,
  LaptopOutlined,
  BugOutlined,
  CloudServerOutlined,
  ProductOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  ClockCircleOutlined,
  EyeOutlined,
} from '@ant-design/icons'
import { agentApi, skillApi, systemApi, type AgentDefinition, type Skill } from '../services/api'
import './AgentPage.css'

const ROLES = [
  '后端开发工程师',
  '前端开发工程师',
  '测试工程师',
  'DevOps 工程师',
  '产品经理',
  '自定义',
]

/* 角色 → 默认 system_prompt */
const ROLE_PROMPTS: Record<string, string> = {
  '后端开发工程师': '你是一名经验丰富的后端开发工程师，擅长 Python、数据库设计、API 开发和系统架构。请严格按照需求完成开发任务，代码规范、注释清晰。',
  '前端开发工程师': '你是一名专业的前端开发工程师，擅长 React、TypeScript、CSS 和交互设计。请按照设计稿和需求完成前端开发，注重用户体验和代码质量。',
  '测试工程师': '你是一名细致的测试工程师，擅长编写测试用例、发现边界问题和回归测试。请全面覆盖功能测试和异常场景，确保产品质量。',
  'DevOps 工程师': '你是一名 DevOps 工程师，擅长 Docker、CI/CD、云部署和基础设施自动化。请确保部署流程稳定、可重复、安全。',
  '产品经理': '你是一名产品经理，擅长需求分析、用户故事编写和优先级排序。请清晰定义需求，确保团队理解一致。',
  '自定义': '',
}

/* 角色 → 图标 + 主题色 + 渐变 */
const ROLE_THEME: Record<string, {
  icon: React.ReactNode
  color: string
  gradient: string
  tagColor: string
  emoji: string
}> = {
  '后端开发工程师': {
    icon: <CodeOutlined />,
    color: '#6366f1',
    gradient: 'linear-gradient(135deg, #6366f1 0%, #818cf8 100%)',
    tagColor: 'purple',
    emoji: '🔧',
  },
  '前端开发工程师': {
    icon: <LaptopOutlined />,
    color: '#06b6d4',
    gradient: 'linear-gradient(135deg, #06b6d4 0%, #67e8f9 100%)',
    tagColor: 'cyan',
    emoji: '💻',
  },
  '测试工程师': {
    icon: <BugOutlined />,
    color: '#f59e0b',
    gradient: 'linear-gradient(135deg, #f59e0b 0%, #fcd34d 100%)',
    tagColor: 'orange',
    emoji: '🐛',
  },
  'DevOps 工程师': {
    icon: <CloudServerOutlined />,
    color: '#10b981',
    gradient: 'linear-gradient(135deg, #10b981 0%, #6ee7b7 100%)',
    tagColor: 'green',
    emoji: '🚀',
  },
  '产品经理': {
    icon: <ProductOutlined />,
    color: '#f43f5e',
    gradient: 'linear-gradient(135deg, #f43f5e 0%, #fb7185 100%)',
    tagColor: 'red',
    emoji: '📋',
  },
  '自定义': {
    icon: <RobotOutlined />,
    color: '#8b5cf6',
    gradient: 'linear-gradient(135deg, #8b5cf6 0%, #c4b5fd 100%)',
    tagColor: 'volcano',
    emoji: '🤖',
  },
}

function getRoleTheme(role: string) {
  return ROLE_THEME[role] ?? ROLE_THEME['自定义']
}

/* Agent 状态：从后端 GET /api/status 拉取（M11 黑盒透明化，替代哈希 mock） */
type AgentStatus = 'idle' | 'executing' | 'offline'

const STATUS_MAP: Record<AgentStatus, { label: string; color: string; dot: string }> = {
  idle: { label: '空闲', color: '#52c41a', dot: 'success' },
  executing: { label: '工作中', color: '#1677ff', dot: 'processing' },
  offline: { label: '离线', color: '#d9d9d9', dot: 'default' },
}

export default function AgentPage() {
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [skillNameMap, setSkillNameMap] = useState<Record<string, string>>({})
  const [agentStatusMap, setAgentStatusMap] = useState<Record<string, AgentStatus>>({})
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AgentDefinition | null>(null)
  const [form] = Form.useForm()
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  const fetchAgents = async () => {
    setLoading(true)
    try {
      const [agentList, skillList] = await Promise.all([
        agentApi.list(),
        skillApi.list(),
      ])
      setAgents(agentList)
      // 技能 id → name 映射，用于展示已挂载技能名
      const m: Record<string, string> = {}
      skillList.forEach((s: Skill) => { m[s.id] = s.name })
      setSkillNameMap(m)
      // 拉取每个群组的 agent 状态，合并成 {agentId: status}
      // （状态接口按群组维度返回，遍历所有群组收集）
      const { groupApi } = await import('../services/api')
      const groups = await groupApi.list()
      const statusMap: Record<string, AgentStatus> = {}
      await Promise.all(
        groups.map(async (g) => {
          try {
            const list = await systemApi.listStatus(g.id)
            list.forEach((s) => {
              statusMap[s.id] = (s.status as AgentStatus) || 'offline'
            })
          } catch {
            /* 群组状态拉取失败静默 */
          }
        }),
      )
      setAgentStatusMap(statusMap)
    } catch {
      message.error('获取智能体列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAgents()
  }, [])

  const handleCreateOrUpdate = async (values: Record<string, unknown>) => {
    try {
      /* 后端要求 system_prompt 必填，非自定义角色自动填充默认 prompt */
      const system_prompt = (values.system_prompt as string) || ROLE_PROMPTS[values.role as string] || ''
      const payload = {
        ...values,
        system_prompt,
        extra_skills: (values.extra_skills as string[]) ?? [],
      }
      if (editing) {
        await agentApi.update(editing.id, payload as Parameters<typeof agentApi.update>[1])
        message.success('更新成功')
      } else {
        await agentApi.create(payload as unknown as Parameters<typeof agentApi.create>[0])
        message.success('创建成功')
      }
      setModalOpen(false)
      setEditing(null)
      form.resetFields()
      fetchAgents()
    } catch {
      message.error('操作失败')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await agentApi.delete(id)
      message.success('删除成功')
      fetchAgents()
    } catch {
      message.error('删除失败')
    }
  }

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (agent: AgentDefinition) => {
    setEditing(agent)
    form.setFieldsValue({
      name: agent.name,
      role: agent.role,
      extra_skills: agent.extra_skills ?? [],
      system_prompt: agent.system_prompt,
    })
    setModalOpen(true)
  }

  const roleValue = Form.useWatch('role', form)

  return (
    <div className="agent-page">
      {/* ── 顶部横幅 ── */}
      <div className="agent-hero">
        <div className="agent-hero-content">
          <div className="agent-hero-text">
            <h1>智能体管理</h1>
            <p>创建和管理你的 AI 智能体团队，每个智能体都有专属角色和技能</p>
          </div>
          <Button
            type="primary"
            size="large"
            icon={<PlusOutlined />}
            onClick={openCreate}
            className="agent-hero-btn"
          >
            新建智能体
          </Button>
        </div>
      </div>

      {/* ── 空状态 ── */}
      {agents.length === 0 && !loading ? (
        <div className="agent-empty">
          <div className="agent-empty-icon">
            <RobotOutlined />
          </div>
          <h3>还没有智能体</h3>
          <p>创建你的第一个 AI 智能体，开始团队协作之旅</p>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate} size="large">
            立即创建
          </Button>
        </div>
      ) : (
        /* ── 卡片网格 ── */
        <div className="agent-grid">
          {agents.map((agent) => {
            const theme = getRoleTheme(agent.role)
            const status = agentStatusMap[agent.id] ?? 'offline'
            const statusInfo = STATUS_MAP[status]
            const isHovered = hoveredId === agent.id

            return (
              <div
                key={agent.id}
                className={`agent-card ${isHovered ? 'agent-card--hovered' : ''} ${status === 'executing' ? 'agent-card--working' : ''}`}
                onMouseEnter={() => setHoveredId(agent.id)}
                onMouseLeave={() => setHoveredId(null)}
              >
                {/* 顶部渐变条 */}
                <div className="agent-card-banner" style={{ background: theme.gradient }} />

                {/* 卡片内容 */}
                <div className="agent-card-body">
                  {/* 头像 + 状态 */}
                  <div className="agent-card-header">
                    <div className="agent-card-avatar">
                      <img src="/robot-avatar.png" alt={agent.name} className="agent-card-avatar-img"
                        style={{
                          animationDelay: `${(agent.id.charCodeAt(0) * 37) % 4000}ms`,
                          animationDuration: `${3000 + (agent.id.charCodeAt(1) ?? 0) % 5 * 300}ms`,
                        }}
                      />
                      <span className="agent-card-avatar-ring" style={{
                        borderColor: theme.color,
                        color: theme.color,
                        animationDelay: `${(agent.id.charCodeAt(0) * 53) % 3000}ms`,
                        animationDuration: `${2500 + (agent.id.charCodeAt(2) ?? 0) % 7 * 200}ms`,
                      }} />
                    </div>
                    <Badge status={statusInfo.dot as any} text={null} className="agent-card-status-badge" />
                  </div>

                  {/* 名称 + 状态文字 */}
                  <div className="agent-card-name-row">
                    <h3 className="agent-card-name">{agent.name}</h3>
                    <Tooltip title={statusInfo.label}>
                      <span className="agent-card-status" style={{ color: statusInfo.color }}>
                        {status === 'executing' && <ThunderboltOutlined style={{ marginRight: 4 }} />}
                        {status === 'idle' && <ClockCircleOutlined style={{ marginRight: 4 }} />}
                        {status === 'offline' && <EyeOutlined style={{ marginRight: 4 }} />}
                        {statusInfo.label}
                      </span>
                    </Tooltip>
                  </div>

                  {/* 角色 */}
                  <div className="agent-card-role" style={{ color: theme.color }}>
                    {theme.icon}
                    <span>{agent.role}</span>
                  </div>

                  {/* 技能标签 */}
                  <div className="agent-card-skills">
                    {agent.extra_skills && agent.extra_skills.length > 0 ? (
                      agent.extra_skills.map((s) => (
                        <Tag key={s} color={theme.tagColor} className="agent-skill-tag">
                          {s}
                        </Tag>
                      ))
                    ) : (
                      <span className="agent-no-skills">暂无技能</span>
                    )}
                  </div>

                  {/* 已挂载技能（来自技能市场 mount） */}
                  {agent.mounted_skills && agent.mounted_skills.length > 0 && (
                    <div className="agent-card-skills" style={{ marginTop: 6 }}>
                      <span style={{ fontSize: 12, color: '#999', marginRight: 4 }}>已挂载:</span>
                      {agent.mounted_skills.map((sid) => (
                        <Tag key={sid} color="geekblue" className="agent-skill-tag">
                          {skillNameMap[sid] ?? sid}
                        </Tag>
                      ))}
                    </div>
                  )}

                  {/* 底部操作 */}
                  <div className="agent-card-actions">
                    <Tooltip title="编辑">
                      <Button
                        type="text"
                        icon={<EditOutlined />}
                        onClick={() => openEdit(agent)}
                        className="agent-action-btn"
                      />
                    </Tooltip>
                    <Popconfirm
                      title="确认删除该智能体？"
                      description="删除后不可恢复"
                      onConfirm={() => handleDelete(agent.id)}
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Tooltip title="删除">
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          className="agent-action-btn"
                        />
                      </Tooltip>
                    </Popconfirm>
                  </div>
                </div>
              </div>
            )
          })}

          {/* 新建占位卡 */}
          <div className="agent-card agent-card--new" onClick={openCreate}>
            <PlusOutlined className="agent-card-new-icon" />
            <span>新建智能体</span>
          </div>
        </div>
      )}

      {/* ── 新建/编辑弹窗 ── */}
      <Modal
        open={modalOpen}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {editing ? <EditOutlined /> : <PlusOutlined />}
            {editing ? '编辑智能体' : '新建智能体'}
          </div>
        }
        onCancel={() => {
          setModalOpen(false)
          setEditing(null)
          form.resetFields()
        }}
        onOk={() => form.submit()}
        destroyOnClose
        okText={editing ? '保存' : '创建'}
        width={520}
      >
        <Form form={form} layout="vertical" onFinish={handleCreateOrUpdate} style={{ marginTop: 16 }}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入智能体名称' }]}
          >
            <Input placeholder="如：前端开发小新" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="role"
            label="角色"
            rules={[{ required: true, message: '请选择角色' }]}
          >
            <Select
              placeholder="选择角色"
              options={ROLES.map((r) => ({
                value: r,
                label: (
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    {getRoleTheme(r).icon}
                    {r}
                  </span>
                ),
              }))}
            />
          </Form.Item>
          {roleValue === '自定义' && (
            <Form.Item name="system_prompt" label="角色描述" rules={[{ required: true, message: '请输入角色描述' }]}>
              <Input.TextArea rows={3} placeholder="输入自定义角色描述（将作为 system prompt）" />
            </Form.Item>
          )}
          <Form.Item name="extra_skills" label="额外技能">
            <Select
              mode="tags"
              placeholder="输入技能名称后回车"
              tokenSeparators={[',']}
              allowClear
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
