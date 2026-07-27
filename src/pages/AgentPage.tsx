import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import {
  Button,
  Card,
  Modal,
  Form,
  Input,
  Select,
  Tag,
  message,
  Popconfirm,
  Tooltip,
  Segmented,
  Space,
  Drawer,
  Alert,
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
  BulbOutlined,
  AppstoreOutlined,
  UserAddOutlined,
  SearchOutlined,
  MessageOutlined,
} from '@ant-design/icons'
import {
  agentApi,
  skillApi,
  systemApi,
  conversationApi,
  type AgentDefinition,
  type AgentTemplate,
  type Skill,
  type Conversation,
  type GroupMember,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import ChatPanel from '../components/ChatPanel'
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
  executing: { label: '工作中', color: '#F26522', dot: 'processing' },
  offline: { label: '离线', color: '#d9d9d9', dot: 'default' },
}

export default function AgentPage() {
  // 全局 groupId setter：体验试聊要把全局 groupId 切到 transient 会话 id，
  // ChatPanel（从 BusEventContext 读 chatGroupId）才订阅对的 WS 通道 + 发消息落对会话。
  const { setGroupId } = useBusEventContext()
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [skillNameMap, setSkillNameMap] = useState<Record<string, string>>({})
  const [agentStatusMap, setAgentStatusMap] = useState<Record<string, AgentStatus>>({})
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AgentDefinition | null>(null)
  const [form] = Form.useForm()
  const [search, setSearch] = useState('')
  const [roleFilter, setRoleFilter] = useState<string>('全部')
  /* AG-01 自然语言生成智能体 */
  const [genOpen, setGenOpen] = useState(false)
  const [genDesc, setGenDesc] = useState('')
  const [generating, setGenerating] = useState(false)
  /* AG-11 角色模板广场 */
  const [templates, setTemplates] = useState<AgentTemplate[]>([])
  const [tplCategory, setTplCategory] = useState<string>('全部')
  const [tplLoading, setTplLoading] = useState(false)
  /* AG-12 雇佣中模板 id 集合（每卡独立 loading，雇佣是 DB create 无 LLM 调用，
   * 通常秒级完成；用 Set 而非单 bool 以支持多卡各自独立的 loading 态，避免
   * 一卡雇佣时全卡按钮齐刷刷转圈）。 */
  const [hiringTplIds, setHiringTplIds] = useState<Set<string>>(new Set())

  /* 体验/管理 tab 切换：体验=点 agent 卡片开临时试聊面板（transient 会话，不进侧栏）；
   * 管理=现有 CRUD（新建/编辑/删除 Modal + 角色模板广场）。 */
  const [tab, setTab] = useState<'experience' | 'manage'>('experience')
  /* 体验对话面板：trialAgent=被试聊的 agent；trialConversation=该 agent 的 transient 会话；
   * trialMembers=占位成员（单聊走 route_direct_message，无需真成员，但 ChatPanel 要 GroupMember[]）。 */
  const [trialAgent, setTrialAgent] = useState<AgentDefinition | null>(null)
  const [trialConversation, setTrialConversation] = useState<Conversation | null>(null)
  const [trialCreating, setTrialCreating] = useState(false)
  const [finalizing, setFinalizing] = useState(false)

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
      // SA-04：单次拉全所有群组所有 agent 状态（GET /api/status 一次返回
      // {group_id: AgentStatusInfo[]}），合并成 {agentId: status}。替代此前
      // 「groupApi.list() + 逐群组 systemApi.listStatus(g.id)」的 N+1 轮询——
      // 群组越多请求数越多，单次拉全消除 N 倍往返。无引擎群组不在返回 dict 中，
      // 缺失即视为该群组无 agent / 全 offline，不影响合并。
      const statusMap: Record<string, AgentStatus> = {}
      try {
        const allStatus = await systemApi.listAllStatus()
        Object.values(allStatus).forEach((list) => {
          list.forEach((s) => {
            statusMap[s.id] = (s.status as AgentStatus) || 'offline'
          })
        })
      } catch {
        /* 状态聚合拉取失败静默（后端未启动 / 无引擎时不影响 agent 列表展示） */
      }
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

  /* AG-11: 拉取预设角色模板（catalog 静态常量，后端恒可用）。
   * 全部拉到前端，分类筛选在前端做（catalog 仅 10 条，全量内存筛选比每点 Tab
   * 发一次请求更快且离线可用）。tplCategory='全部' 时显示全部，否则精确匹配分类。 */
  const fetchTemplates = async () => {
    setTplLoading(true)
    try {
      const list = await agentApi.listTemplates()
      setTemplates(list)
    } catch {
      message.error('获取角色模板失败')
    } finally {
      setTplLoading(false)
    }
  }

  const tplCategories = useMemo(() => {
    const seen: string[] = []
    templates.forEach((t) => {
      if (!seen.includes(t.category)) seen.push(t.category)
    })
    return seen
  }, [templates])

  const filteredTemplates = useMemo(() => {
    if (tplCategory === '全部') return templates
    return templates.filter((t) => t.category === tplCategory)
  }, [templates, tplCategory])

  const handleCreateOrUpdate = async (values: Record<string, unknown>) => {
    try {
      /* 后端要求 system_prompt 必填，非自定义角色自动填充默认 prompt */
      const system_prompt = (values.system_prompt as string) || ROLE_PROMPTS[values.role as string] || ''
      /* PRD AG-08: icon_emoji 留空回退角色派生主题 emoji，保证卡片头像永不空。
       * description/slug 直通，slug 留空则后端 None（向后兼容旧 agent）。 */
      const derivedEmoji = (values.icon_emoji as string | undefined)?.trim()
      const icon_emoji = derivedEmoji || getRoleTheme(values.role as string).emoji
      const payload = {
        ...values,
        system_prompt,
        extra_skills: (values.extra_skills as string[]) ?? [],
        icon_emoji,
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
      slug: agent.slug ?? undefined,
      description: agent.description ?? undefined,
      icon_emoji: agent.icon_emoji ?? undefined,
    })
    setModalOpen(true)
  }

  /* 派生过滤：先 search 模糊匹配（name/role/description/skills/extra_skills 任一包含即中），
   * 再 roleFilter 精确过滤（'全部' 不过滤）。两者皆空时返回全量 agents。 */
  const filteredAgents = useMemo(() => {
    const kw = search.trim().toLowerCase()
    const matchKw = (agent: AgentDefinition) => {
      if (!kw) return true
      if (agent.name?.toLowerCase().includes(kw)) return true
      if (agent.role?.toLowerCase().includes(kw)) return true
      if (agent.description?.toLowerCase().includes(kw)) return true
      if ((agent.skills ?? []).some((s) => s.toLowerCase().includes(kw))) return true
      if ((agent.extra_skills ?? []).some((s) => s.toLowerCase().includes(kw))) return true
      return false
    }
    const matchRole = (agent: AgentDefinition) =>
      roleFilter === '全部' || agent.role === roleFilter
    return agents.filter((a) => matchKw(a) && matchRole(a))
  }, [agents, search, roleFilter])

  /* AG-01: 自然语言生成完整智能体配置。
   * 调 agentApi.generate(description) → 后端 LLM 生成 name/role/system_prompt/
   * skills/extra_skills/description 落库返回 AgentDefinition。生成成功后 fetchAgents
   * 刷新列表，新生成的 agent 卡片立即可见。
   * generating 串行锁防重复点击（LLM 调用耗时数秒，期间禁用按钮+loading 态）。 */
  const handleGenerate = async () => {
    const desc = genDesc.trim()
    if (!desc) {
      message.warning('请描述你想要的智能体')
      return
    }
    setGenerating(true)
    try {
      const agent = await agentApi.generate(desc)
      message.success(`已生成智能体「${agent.name}」`)
      setGenOpen(false)
      setGenDesc('')
      fetchAgents()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '生成失败')
    } finally {
      setGenerating(false)
    }
  }

  /* AG-11: 角色模板广场展示侧栏开关。展开时懒拉取模板（首开才请求，
   * 后续开关用已有 state 不重复请求——catalog 静态不变）。 */
  const [tplPanelOpen, setTplPanelOpen] = useState(false)
  const toggleTemplates = (open: boolean) => {
    setTplPanelOpen(open)
    if (open && templates.length === 0) {
      fetchTemplates()
    }
  }

  /* AG-12: 雇佣预设角色模板创建员工。
   * 调 agentApi.hireTemplate(tpl.template_id) → 后端 get_template 解析 catalog 全配置
   * → AgentCreatePayload → crud.create_agent 落库 → 返回 AgentDefinition（原样落库，
   * name 用模板名）。成功后 fetchAgents 刷新员工列表，新员工卡片立即出现在网格。
   *
   * 不弹改名 Modal——直接用模板名落库是最常见路径（用户在广场看中一个角色想立即加入
   * 团队，改名是可选需求非主流程）；雇佣后如需改名可点员工卡编辑（AG-06），编辑入口已
   * 在卡片底部常驻。省去 Modal 让雇佣一键完成（与 AG-01 生成成功的体验一致：生成后
   * 直接刷新列表，要改用编辑）。
   *
   * hiringTplIds 跟踪每卡 loading（Set 支持多卡独立态），loading 中按钮 disabled 防重复
   * 点击。catch 兜底 message.error（后端未知 template_id→404 / DB 异常都会经 http 抛 Error）。 */
  const handleHireTemplate = async (tpl: AgentTemplate) => {
    setHiringTplIds((prev) => new Set(prev).add(tpl.template_id))
    try {
      const agent = await agentApi.hireTemplate(tpl.template_id)
      message.success(`已雇佣「${agent.name}」加入团队`)
      fetchAgents()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '雇佣失败')
    } finally {
      setHiringTplIds((prev) => {
        const next = new Set(prev)
        next.delete(tpl.template_id)
        return next
      })
    }
  }

  const roleValue = Form.useWatch('role', form)

  /* 开体验对话：为指定 agent 创建一个 transient 会话（后端 transient=1，不进侧栏），
   * 拉到 trialConversation 后开 Drawer。ChatPanel 读 trialConversation 当 group
   * （形状兼容：coordinator_id 镜像 agent_id）。
   *
   * 串台注意：ChatPanel 内部从 BusEventContext 读全局 groupId 作 chatGroupId（WS 订阅 +
   * 消息收发 key）。广场页试聊时全局 groupId 可能停在旧会话，需把全局 groupId 设到
   * trialConversation.id，ChatPanel 才订阅对的 WS 通道、发消息才落到试聊会话。
   * 代价：全局 state 被体验面板占用，但 transient 会话不在侧栏列表（list 过滤），
   * 侧栏不高亮，关闭体验面板后用户点侧栏别的会话即恢复，demo 阶段可接受。 */
  const openTrial = async (agent: AgentDefinition) => {
    setTrialCreating(true)
    try {
      const conv = await conversationApi.create({ agent_id: agent.id, transient: 1 })
      setTrialConversation(conv)
      setTrialAgent(agent)
      setGroupId(conv.id)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '开启体验对话失败')
    } finally {
      setTrialCreating(false)
    }
  }

  const closeTrial = () => {
    setTrialAgent(null)
    setTrialConversation(null)
  }

  /* 体验转正：transient 1→0，会话落进左侧【会话】列表。转正后切到对话视图。
   * 注意：转正后不在此处切 groupId（切了会跳出广场页体验面板），由用户自己点侧栏
   * 新转正的会话进入。这里只刷新后端 + 提示 + 关体验面板。 */
  const finalizeTrial = async () => {
    if (!trialConversation) return
    setFinalizing(true)
    try {
      await conversationApi.finalize(trialConversation.id)
      message.success('已转为正式会话，可在左侧【会话】继续对话')
      closeTrial()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '转正失败')
    } finally {
      setFinalizing(false)
    }
  }

  // 体验面板的占位成员（ChatPanel 需要 GroupMember[]，单聊 route_direct_message 不用，
  // 但 props 类型要求；传空数组即可，@mention 候选为空不影响单聊）。
  const trialMembers: GroupMember[] = []

  return (
    <div
      className="agent-page"
      style={{ height: '100%', minHeight: 0, overflowY: 'auto', padding: 20, background: 'var(--surface-main)' }}
    >
      {/* ── 顶部横幅 ── */}
      <div className="agent-hero">
        <div className="agent-hero-content">
          <div className="agent-hero-text">
            <h1>智能体广场</h1>
            <p>体验各角色智能体的对话能力，或管理你的 AI 智能体团队</p>
          </div>
          {/* 体验/管理 tab 切换（右上角）。体验=试聊不进侧栏；管理=CRUD。 */}
          <Segmented
            value={tab}
            onChange={(v) => setTab(v as 'experience' | 'manage')}
            options={[
              { label: '体验', value: 'experience' },
              { label: '管理', value: 'manage' },
            ]}
          />
        </div>
      </div>

      {/* ── AG-11 角色模板广场（仅管理 tab 显示） ── */}
      {tab === 'manage' && tplPanelOpen && (
        <div className="agent-templates-panel">
          <div className="agent-templates-header">
            <div className="agent-templates-title">
              <AppstoreOutlined />
              <h3>角色模板广场</h3>
              <span className="agent-templates-subtitle">
                选择预设角色，一键雇佣加入团队
              </span>
            </div>
            <Segmented
              value={tplCategory}
              onChange={(val) => setTplCategory(val as string)}
              options={['全部', ...tplCategories].map((c) => ({ label: c, value: c }))}
              size="small"
            />
          </div>

          <div className="agent-templates-grid">
            {tplLoading ? (
              <div className="agent-templates-empty">加载中…</div>
            ) : filteredTemplates.length === 0 ? (
              <div className="agent-templates-empty">暂无模板</div>
            ) : (
              filteredTemplates.map((tpl) => {
                const tplSkills = Array.from(
                  new Set([...(tpl.skills ?? []), ...(tpl.extra_skills ?? [])]),
                )
                return (
                  <div key={tpl.template_id} className="agent-template-card">
                    <div className="agent-template-card-top">
                      <span className="agent-template-emoji">{tpl.icon_emoji}</span>
                      <div className="agent-template-meta">
                        <h4 className="agent-template-name">{tpl.name}</h4>
                        <span className="agent-template-role">{tpl.role}</span>
                      </div>
                      <Tag className="agent-template-cat-tag">{tpl.category}</Tag>
                    </div>
                    <p className="agent-template-desc" title={tpl.description}>
                      {tpl.description}
                    </p>
                    <div className="agent-template-skills">
                      {tplSkills.length > 0 ? (
                        tplSkills.map((s) => (
                          <Tag key={s} className="agent-template-skill-tag">
                            {s}
                          </Tag>
                        ))
                      ) : (
                        <span className="agent-no-skills">暂无技能</span>
                      )}
                    </div>
                    {/* AG-12 雇佣按钮：调 hireTemplate 落库为本地员工，成功后 fetchAgents 刷新。
                     * loading 中 disabled 防重复点击（hiringTplIds Set 跟踪每卡独立 loading）。 */}
                    <Button
                      block
                      size="small"
                      icon={<UserAddOutlined />}
                      className="agent-template-hire-btn"
                      loading={hiringTplIds.has(tpl.template_id)}
                      onClick={() => handleHireTemplate(tpl)}
                    >
                      雇佣
                    </Button>
                  </div>
                )
              })
            )}
          </div>
        </div>
      )}

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
        <>
        {/* ── 工具栏（搜索 + 角色筛选 + 操作）──
            搜索/角色筛选 两个 tab 共用（找 agent 试聊/管理都要）；「自然语言生成」+
            「角色模板广场」仅管理 tab（是 CRUD 操作）。 */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 12 }}>
          <Input
            placeholder="搜索智能体名称 / 角色 / 技能"
            allowClear
            prefix={<SearchOutlined />}
            style={{ width: 260 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Space>
            <Segmented
              size="small"
              value={roleFilter}
              onChange={(val) => setRoleFilter(val as string)}
              options={['全部', ...ROLES].map((c) => ({ label: c, value: c }))}
            />
            {tab === 'manage' && (
              <>
                <Button icon={<BulbOutlined />} onClick={() => setGenOpen(true)}>
                  自然语言生成
                </Button>
                <Button
                  icon={<AppstoreOutlined />}
                  type={tplPanelOpen ? 'primary' : 'default'}
                  onClick={() => toggleTemplates(!tplPanelOpen)}
                >
                  角色模板广场
                </Button>
              </>
            )}
          </Space>
        </div>

        {/* ── 卡片网格 ── */}
        <div className="agent-grid">
          {filteredAgents.map((agent) => {
            const theme = getRoleTheme(agent.role)
            const status = agentStatusMap[agent.id] ?? 'offline'
            const statusInfo = STATUS_MAP[status]
            // AG-05: 卡片「技能」合并核心 skills + extra_skills（去重，核心在前），
            // 让员工列表完整展示能力栈。skills 是角色核心技能，extra_skills 是附加能力，
            // 合并展示比只显 extra_skills 更完整。两者皆空时显「暂无技能」占位。
            const allSkills = Array.from(
              new Set([...(agent.skills ?? []), ...(agent.extra_skills ?? [])]),
            )

            return (
              <Card
                key={agent.id}
                hoverable
                variant="outlined"
                className={`agent-card--antd ${status === 'executing' ? 'agent-card--working' : ''}`}
                style={{ '--agent-color': theme.color, '--agent-gradient': theme.gradient } as CSSProperties}
                styles={{ body: { padding: 16 } }}
                cover={
                  <div className="agent-card-cover">
                    <div className="agent-card-cover-banner" style={{ background: theme.gradient }} />
                    <span className="agent-template-emoji" aria-hidden>{agent.icon_emoji || theme.emoji}</span>
                  </div>
                }
                actions={
                  tab === 'experience'
                    ? [
                        <Button
                          key="trial"
                          type="primary"
                          size="small"
                          icon={<MessageOutlined />}
                          loading={trialCreating && trialAgent?.id === agent.id}
                          onClick={() => openTrial(agent)}
                        >
                          试聊
                        </Button>,
                      ]
                    : [
                        <Tooltip title="编辑" key="edit">
                          <Button type="text" icon={<EditOutlined />} onClick={() => openEdit(agent)}>编辑</Button>
                        </Tooltip>,
                        <Popconfirm
                          key="delete"
                          title="确认删除该智能体?"
                          description="删除后不可恢复"
                          onConfirm={() => handleDelete(agent.id)}
                          okText="删除"
                          cancelText="取消"
                          okButtonProps={{ danger: true }}
                        >
                          <Tooltip title="删除">
                            <Button type="text" danger icon={<DeleteOutlined />}>删除</Button>
                          </Tooltip>
                        </Popconfirm>,
                      ]
                }
              >
                {/* 名称行 + 状态点 */}
                <div className="agent-template-card-top">
                  <div className="agent-template-meta">
                    <h4 className="agent-template-name" title={agent.name}>{agent.name}</h4>
                    {/* AG-08: 角色旁 slug 副标题小字；留空不占位避免空隙 */}
                    <span className="agent-template-role">
                      {agent.role}
                      {agent.slug ? <span className="agent-template-slug"> · {agent.slug}</span> : null}
                    </span>
                  </div>
                  <Tooltip title={statusInfo.label}>
                    <span className={`agent-card-status-dot agent-card-status-dot--${status}`}>
                      {status === 'executing' && <ThunderboltOutlined />}
                      {status === 'idle' && <ClockCircleOutlined />}
                      {status === 'offline' && <EyeOutlined />}
                    </span>
                  </Tooltip>
                </div>

                {/* 描述:复用 .agent-template-desc(2行截断) */}
                {agent.description ? (
                  <p className="agent-template-desc" title={agent.description}>{agent.description}</p>
                ) : (
                  <p className="agent-template-desc agent-card-desc--empty">暂无描述</p>
                )}

                {/* 技能标签:复用 .agent-template-skills / .agent-template-skill-tag */}
                <div className="agent-template-skills">
                  {allSkills.length > 0 ? (
                    allSkills.map((s) => (
                      <Tag key={s} className="agent-template-skill-tag">{s}</Tag>
                    ))
                  ) : (
                    <span className="agent-no-skills">暂无技能</span>
                  )}
                </div>

                {/* 工具权限行已移除：allowed_tools/denied_tools 死代码已删（2026-07-27）。
                    工具门控由受控工具池 + skill-sandbox denylist 负责，不再落库展示。 */}

                {/* 已挂载技能行:保留原样 */}
                {agent.mounted_skills && agent.mounted_skills.length > 0 && (
                  <div className="agent-card-meta-line">
                    <span className="agent-card-meta-label">已挂载</span>
                    {agent.mounted_skills.map((sid) => (
                      <Tag key={sid} color="geekblue" className="agent-skill-tag">
                        {skillNameMap[sid] ?? sid}
                      </Tag>
                    ))}
                  </div>
                )}
              </Card>
            )
          })}

          {/* 新建占位卡（仅管理 tab 显示，体验 tab 不新建） */}
          {tab === 'manage' && (
            <div className="agent-card agent-card--new" onClick={openCreate}>
              <PlusOutlined className="agent-card-new-icon" />
              <span>新建智能体</span>
            </div>
          )}
        </div>
        </>
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
          {/* PRD AG-08: 与模板对齐的三个身份字段。slug=英文 URL 安全 id；description=一句话职责
              定位（之前表单漏掉，此处补齐）；icon_emoji=头像 emoji，留空回退角色派生主题 emoji。 */}
          <Form.Item
            name="slug"
            label="英文名（slug）"
            tooltip="URL 安全的英文标识符，雇佣模板时自动继承。留空则不设置。"
          >
            <Input placeholder="如：frontend-dev" autoComplete="off" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="一句话介绍该智能体的职责定位" />
          </Form.Item>
          <Form.Item
            name="icon_emoji"
            label="头像 emoji"
            tooltip="留空则按角色自动派生（如前端=💻、后端=🔧、测试=🐛）。"
          >
            <Input placeholder="如：🦊（留空回退角色派生）" autoComplete="off" maxLength={8} />
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

      {/* ── AG-01 自然语言生成弹窗 ── */}
      <Modal
        open={genOpen}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <BulbOutlined />
            自然语言生成智能体
          </div>
        }
        onCancel={() => {
          setGenOpen(false)
          setGenDesc('')
        }}
        onOk={handleGenerate}
        confirmLoading={generating}
        okText="生成"
        width={520}
        destroyOnClose
      >
        <div style={{ marginTop: 16 }}>
          <p style={{ color: '#666', marginBottom: 12 }}>
            用自然语言描述你想要的智能体角色定位，AI 将自动生成名称、角色、提示词与技能。
          </p>
          <Input.TextArea
            value={genDesc}
            onChange={(e) => setGenDesc(e.target.value)}
            rows={4}
            placeholder="如：一个负责数据清洗和报表生成的数据分析师，熟悉 Python pandas 和 PostgreSQL"
            disabled={generating}
            onPressEnter={(e) => {
              if (e.ctrlKey || e.metaKey) {
                e.preventDefault()
                handleGenerate()
              }
            }}
          />
          <p style={{ color: '#999', fontSize: 12, marginTop: 8, marginBottom: 0 }}>
            提示：Ctrl/⌘ + Enter 快速生成。生成耗时约数秒，请稍候。
          </p>
        </div>
      </Modal>

      {/* ── 体验对话抽屉（点「试聊」打开，transient 会话，不进侧栏） ──
          复用 ChatPanel（hideHeader，标题区自画）：传 trialConversation 当 group
          （形状兼容，coordinator_id 镜像 agent_id）。Drawer 宽 720，遮住广场右侧
          让用户聚焦试聊。关闭即清（transient 会话留库但 list_conversations 过滤掉
          不进侧栏；用户可选「转为正式会话」落进左侧）。 */}
      <Drawer
        open={!!trialConversation}
        onClose={closeTrial}
        width={720}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <MessageOutlined style={{ color: 'var(--brand, #F26522)' }} />
            <span>体验对话 · {trialAgent?.name ?? '智能体'}</span>
          </div>
        }
        styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column', height: '100%' } }}
        destroyOnClose
      >
        {trialConversation && (
          <>
            <Alert
              type="info"
              showIcon
              message="这是体验对话，关闭后不保留在左侧【会话】"
              description="点「转为正式会话」可保存到左侧继续对话。"
              style={{ borderRadius: 0, flexShrink: 0 }}
              action={
                <Button
                  size="small"
                  type="primary"
                  loading={finalizing}
                  onClick={finalizeTrial}
                >
                  转为正式会话
                </Button>
              }
            />
            <div style={{ flex: 1, minHeight: 0 }}>
              <ChatPanel
                group={trialConversation}
                agents={agents}
                members={trialMembers}
                hideHeader
              />
            </div>
          </>
        )}
      </Drawer>
    </div>
  )
}
