import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Collapse,
  Divider,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { EditOutlined, LockOutlined, RobotOutlined } from '@ant-design/icons'
import {
  agentApi,
  mcpApi,
  skillApi,
  type AgentDefinition,
  type McpConnection,
  type Skill,
} from '../services/api'

const { Paragraph } = Typography

/**
 * 内建能力清单（系统预置、所有执行任务的智能体均自带、只读展示）。
 *
 * 对应后端 `engine/tools.py` 的 `tools_for_group` 返回的 5 个 @tool 框架内工具——
 * 每个 worker 跑任务时 `ChatOpenAI.bind_tools` 自动绑定，与群组工作区闭包绑定，
 * 用户不可增删（区别于「已挂载技能」「已挂载 MCP」两类可挂载资源）。
 *
 * 这里只做展示：让用户知道智能体开箱即得这些能力，无需也无法配置。desc 取自
 * 各 @tool 的 docstring（摘最关键一句），保持与后端实际工具语义一致。
 */
const BUILTIN_TOOLS: { name: string; desc: string }[] = [
  { name: 'read_file', desc: '读取工作区文件（UTF-8，截断 8KB）' },
  { name: 'write_file', desc: '创建/覆盖工作区文件（自动建父目录）' },
  { name: 'edit_file', desc: '精确字符串替换（old_text→new_text）' },
  { name: 'list_dir', desc: '列出目录条目（最多 200 项）' },
  { name: 'run_command', desc: '工作区内执行 shell 命令（超时 30s）' },
]

interface AgentDetailPanelProps {
  /**
   * 待展示的智能体列表（handler 已解析）。
   * - 长度 === 1 → 完整聚合详情卡片（system_prompt 摘要 + mounted_skills/mcp + allowed/denied
   *   tools + model + max_turns），即 SC-09 「打开 AgentDetailPanel 聚合卡片」的主路径。
   * - 长度 > 1 → 紧凑名册列表（name/role + 挂载计数），提示用 `/agent <名称>` 查看详情。
   * - 长度 === 0 → Empty 占位（无匹配智能体）。
   */
  agents: AgentDefinition[]
  /**
   * AD-02 编辑入口：agent 更新后的回调。slash handler 渲染本面板时无法回刷全局 agents，
   * 故本面板内部维护 `local` 副本——更新成功后用最新 AgentDefinition 替换 local 副本，
   * 卡片立即反映新配置，无需外部刷新。可选（AgentPage 复用本面板时可不传，自行 fetchAgents）。
   */
  onUpdated?: (agent: AgentDefinition) => void
}

/**
 * 截断长文本为概要（system_prompt 可能很长，详情卡只展示前 N 字 + Tooltip 全文）。
 * 保留换行：先去首尾空白，超长截断加省略号；空内容返回空串（调用方据此判空不渲染）。
 */
function summarize(text: string | null | undefined, max = 200): string {
  if (!text) return ''
  const t = text.trim()
  if (t.length <= max) return t
  return t.slice(0, max) + '…'
}

/**
 * SC-09 `/agent` 结果卡片 + AD-01 只读聚合详情 + AD-02 编辑入口。
 *
 * 数据来自 `agentApi.list()`（前端按 name/id 解析到目标 agent）——一个智能体的全部配置聚合
 * 展示：身份（name/role/description）+ system_prompt 摘要 + 核心技能 skills/extra_skills +
 * 已挂载技能 mounted_skills + 已挂载 MCP mounted_mcp + 工具权限 allowed/denied_tools +
 * 运行参数 model/max_turns。让用户在聊天里 /agent 快速回看某智能体的完整配置画像，
 * 不必跳 AgentPage 翻卡片。
 *
 * AD-02 编辑入口（单 agent 模式）：
 *  - 卡片右上角「编辑」按钮打开 Modal，内含：
 *    ① 已挂载技能（Select multiple + options 来自 skillApi.list 未挂载项）——
 *       选中→POST /api/skills/{id}/mount，取消选中→POST /api/skills/{id}/unmount，
 *       每次变更实时调 skillApi.mount/unmount，返回最新 AgentDefinition 同步 local。
 *    ② 已挂载 MCP（同上，options 来自 mcpApi.list 未挂载项）——mcpApi.mount/unmount。
 *    ③ 身份与行为（name / role / description / system_prompt）+ 运行参数（model /
 *       max_turns / allowed_tools / denied_tools）——底部「保存」调
 *       agentApi.update(id, {...})，成功后同步 local。
 *  - 设计原则：mount/unmount 是即时生效的细粒度操作（每次 Select 变更即调 api），
 *    身份+运行参数是批量保存（Modal 底部「保存」按钮）——与 AgentPage 编辑 Modal 一致风格。
 *
 * 设计：
 *  - 两种模式：单 agent → 完整聚合详情（+ 编辑入口）；多 agent → 紧凑名册列表（无编辑）。
 *  - 详情按「身份 → 角色描述 → 核心技能 → 已挂载技能 → 已挂载 MCP → 工具权限 → 运行参数」
 *    分段，每段标题灰底小标签，值用 Tag 行或文本，空段不渲染（避免空标签行噪音）。
 *  - system_prompt 只展示前 200 字摘要 + Tooltip 全文（system_prompt 可能数百字，全展开撑爆卡片）。
 *  - 紫边卡片 #d3adf7——智能体是「系统配置」类（与 ModelCard 紫边呼应），区别于查看类蓝边。
 *
 * 与 AgentPage 区别：AgentPage 是管理页（CRUD + 编辑 Modal）；/agent 卡片是聊天流内
 * 聚合详情快照 + 内联编辑入口（AD-02：mount/unmount + model/tools 更新），编辑能力与
 * AgentPage 互补——聊天里看到 agent 详情即可就地微调，不必跳页。
 */
export default function AgentDetailPanel({ agents, onUpdated }: AgentDetailPanelProps) {
  // AD-02：local 副本——mount/unmount/update 后立即反映新配置，无需外部刷新。
  // agents 是 handler 传入的快照，local 以它为初值；编辑操作只更新 local[0]。
  const [local, setLocal] = useState<AgentDefinition[]>(agents)

  // agents 变化（用户再次 /agent 查别的 agent）时重置 local 为新快照。
  useEffect(() => {
    setLocal(agents)
  }, [agents])

  // 多 agent：紧凑名册列表
  if (local.length > 1) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12, borderColor: '#d3adf7' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#722ed1' }} />
            <Tag color="purple" style={{ margin: 0 }}>智能体名册</Tag>
            <span style={{ fontSize: 13, color: '#666' }}>共 {local.length} 个 · 用 /agent 名称 查看详情</span>
          </span>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {local.map((a) => {
            const skillCount = (a.mounted_skills?.length ?? 0) + (a.mounted_mcp?.length ?? 0)
            return (
              <div
                key={a.id}
                style={{
                  padding: '8px 10px',
                  background: '#fafafa',
                  borderRadius: 4,
                  border: '1px solid #f0f0f0',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, fontSize: 13, color: '#333' }}>{a.name}</span>
                  <Tag color="purple" style={{ margin: 0 }}>{a.role}</Tag>
                  {skillCount > 0 && (
                    <Tag style={{ margin: 0 }}>挂载 {skillCount}</Tag>
                  )}
                </div>
                {a.description && (
                  <div
                    style={{
                      fontSize: 12,
                      color: '#999',
                      marginTop: 4,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {a.description}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </Card>
    )
  }

  // 无 agent：占位
  const agent = local[0]
  if (!agent) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12, borderColor: '#d3adf7' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#722ed1' }} />
            <Tag color="purple" style={{ margin: 0 }}>智能体详情</Tag>
          </span>
        }
      >
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="未找到匹配的智能体"
          style={{ margin: '8px 0' }}
        />
      </Card>
    )
  }

  return <AgentDetailView agent={agent} onUpdated={(a) => { setLocal([a]); onUpdated?.(a) }} />
}

/**
 * 单 agent 完整聚合详情卡片 + AD-02 编辑入口。
 * 拆出子组件：让 hooks（编辑 Modal 状态、options 拉取）只在单 agent 模式生效，
 * 多 agent 名册模式不触发无用 effect。
 */
function AgentDetailView({
  agent,
  onUpdated,
}: {
  agent: AgentDefinition
  onUpdated: (a: AgentDefinition) => void
}) {
  const sysSummary = summarize(agent.system_prompt, 200)
  const hasSys = sysSummary.length > 0
  const allSkills = [...(agent.skills ?? []), ...(agent.extra_skills ?? [])]
  const mountedSkills = agent.mounted_skills ?? []
  const mountedMcp = agent.mounted_mcp ?? []
  const allowed = agent.allowed_tools ?? []
  const denied = agent.denied_tools ?? []
  const hasTools = allowed.length > 0 || denied.length > 0
  const hasRuntime = agent.model != null || agent.max_turns != null

  /** 分段标题：灰底小标签，保持视觉一致。 */
  const Section = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 11, color: '#999', marginBottom: 4 }}>{title}</div>
      {children}
    </div>
  )

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#d3adf7' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <RobotOutlined style={{ color: '#722ed1' }} />
          <Tag color="purple" style={{ margin: 0 }}>{agent.role}</Tag>
          <span style={{ fontSize: 13, color: '#333', fontWeight: 600 }}>{agent.name}</span>
        </span>
      }
      extra={
        <AgentEditButton agent={agent} onUpdated={onUpdated} small />
      }
    >
      {/* 身份 */}
      {agent.description && (
        <Section title="定位">
          <span style={{ fontSize: 12, color: '#666' }}>{agent.description}</span>
        </Section>
      )}

      {/* 角色描述（system_prompt 摘要） */}
      {hasSys && (
        <Section title="角色描述（system_prompt 摘要）">
          <Tooltip title={agent.system_prompt}>
            <Paragraph
              style={{ margin: 0, fontSize: 12, color: '#555', whiteSpace: 'pre-wrap' }}
            >
              {sysSummary}
            </Paragraph>
          </Tooltip>
        </Section>
      )}

      {/* 核心技能 */}
      {allSkills.length > 0 && (
        <Section title="核心技能">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {allSkills.map((s) => (
              <Tag key={s} color="purple" style={{ margin: 0 }}>{s}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 已挂载技能 */}
      {mountedSkills.length > 0 && (
        <Section title="已挂载技能">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {mountedSkills.map((sid) => (
              <Tag key={sid} color="geekblue" style={{ margin: 0 }}>{sid}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 已挂载 MCP */}
      {mountedMcp.length > 0 && (
        <Section title="已挂载 MCP">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {mountedMcp.map((mid) => (
              <Tag key={mid} color="magenta" style={{ margin: 0 }}>{mid}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 工具权限 */}
      {hasTools && (
        <Section title="工具权限">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {allowed.map((t) => (
              <Tag key={t} color="green" style={{ margin: 0 }}>{t}</Tag>
            ))}
            {denied.map((t) => (
              <Tag key={t} color="red" style={{ margin: 0 }}>禁:{t}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 运行参数 */}
      {hasRuntime && (
        <Section title="运行参数">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
            {agent.model && (
              <span style={{ color: '#666' }}>
                模型: <Tag color="blue" style={{ margin: 0 }}>{agent.model}</Tag>
              </span>
            )}
            {agent.max_turns != null && (
              <span style={{ color: '#666' }}>
                最大轮次: <Tag style={{ margin: 0 }}>{agent.max_turns}</Tag>
              </span>
            )}
          </div>
        </Section>
      )}

      {/* 空配置兜底：啥都没有时给个提示，避免卡片只剩标题 */}
      {!agent.description && !hasSys && allSkills.length === 0 &&
        mountedSkills.length === 0 && mountedMcp.length === 0 && !hasTools && !hasRuntime && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="该智能体无额外配置"
          style={{ margin: '8px 0' }}
        />
      )}
    </Card>
  )
}

/**
 * AD-02 编辑按钮 + Modal：挂载/卸载技能（skillApi）、挂载/卸载 MCP（mcpApi）、
 * 更新 身份字段 + model/tools（agentApi.update）。
 *
 * 触发：单 agent 详情卡片右上角「编辑」按钮（small 卡片用 size="small" 文本按钮）。
 * Modal 四段：
 *  1. 已挂载技能——Select multiple，options = skillApi.list 中未挂载项；onChange 时
 *     diff 出新增/移除的 skill id，分别调 skillApi.mount/unmount（即时生效，每次变更即调）。
 *  2. 已挂载 MCP——同上，options = mcpApi.list 未挂载项；mcpApi.mount/unmount。
 *  3. 身份与行为——name（Input）/ role（Input）/ description（TextArea autoSize）/
 *     system_prompt（TextArea autoSize），底部「保存」调 agentApi.update。
 *  4. 运行参数 + 工具权限——model（Input）/ max_turns（Input type=number）/
 *     allowed_tools（Select tags）/ denied_tools（Select tags），底部「保存」调 agentApi.update。
 *     （3+4 同一次 payload 一起保存。）
 *
 * 数据流：mount/unmount 返回最新 AgentDefinition → onUpdated 同步父 local → 卡片立即刷新。
 * model/tools 保存后同样 onUpdated。Modal 内 options（skills/mcps 全量列表）懒加载：
 * 首次打开 Modal 才拉取（避免未编辑就发请求）。
 *
 * 导出（布局重构 2026-07-11）：单聊 ChatView 标题区复用本组件作为「智能体设置」入口——
 * 单聊时点 ⚙/编辑打开同一 Modal，挂载技能/MCP + 改 model/tools，与 AgentDetailPanel
 * 卡片里的编辑入口完全同源，避免重复实现一套单 agent 编辑 UI。
 */
export function AgentEditButton({
  agent,
  onUpdated,
  small,
}: {
  agent: AgentDefinition
  onUpdated: (a: AgentDefinition) => void
  small?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcps, setMcps] = useState<McpConnection[]>([])
  const [optsLoading, setOptsLoading] = useState(false)

  // Modal 表单态：身份段 name/role/description/system_prompt + 运行段 model/max_turns/allowed/denied
  const [nameVal, setNameVal] = useState(agent.name || '')
  const [roleVal, setRoleVal] = useState(agent.role || '')
  const [descVal, setDescVal] = useState(agent.description ?? '')
  const [sysPromptVal, setSysPromptVal] = useState(agent.system_prompt ?? '')
  const [modelVal, setModelVal] = useState(agent.model || '')
  const [maxTurns, setMaxTurns] = useState<number | null>(agent.max_turns ?? null)
  const [allowedTools, setAllowedTools] = useState<string[]>(agent.allowed_tools ?? [])
  const [deniedTools, setDeniedTools] = useState<string[]>(agent.denied_tools ?? [])
  const [saving, setSaving] = useState(false)

  // 运行参数段（model/max_turns/allowed/denied_tools）当前先隐藏：本阶段聚焦身份字段。
  // state 与 payload 保留（隐藏时仍以原值回传，不会误清空），需要时改 true 即恢复整段。
  const showRuntime = false

  // Modal 打开时懒拉取 skills/mcps 全量列表（供 Select options）。
  // 仅首次打开拉取一次（后续开 Modal 复用已加载 state，避免重复请求）。
  const loadOptions = async () => {
    if (optsLoading || (skills.length > 0 && mcps.length > 0)) return
    setOptsLoading(true)
    try {
      const [sk, mc] = await Promise.all([skillApi.list(), mcpApi.list()])
      setSkills(sk)
      setMcps(mc)
    } catch (e) {
      message.error(`加载可挂载资源失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setOptsLoading(false)
    }
  }

  const openModal = () => {
    // 每次打开同步表单初值为最新 agent（agent 可能已被上次编辑更新）
    setNameVal(agent.name || '')
    setRoleVal(agent.role || '')
    setDescVal(agent.description ?? '')
    setSysPromptVal(agent.system_prompt ?? '')
    setModelVal(agent.model || '')
    setMaxTurns(agent.max_turns ?? null)
    setAllowedTools(agent.allowed_tools ?? [])
    setDeniedTools(agent.denied_tools ?? [])
    void loadOptions()
    setOpen(true)
  }

  // ── 技能 mount/unmount（即时生效）──
  const handleSkillsChange = async (next: string[]) => {
    const prev = agent.mounted_skills ?? []
    const added = next.filter((id) => !prev.includes(id))
    const removed = prev.filter((id) => !next.includes(id))
    try {
      let latest: AgentDefinition = agent
      for (const id of added) {
        latest = await skillApi.mount(id, agent.id)
      }
      for (const id of removed) {
        latest = await skillApi.unmount(id, agent.id)
      }
      if (added.length || removed.length) onUpdated(latest)
    } catch (e) {
      message.error(`技能挂载变更失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // ── MCP mount/unmount（即时生效）──
  const handleMcpsChange = async (next: string[]) => {
    const prev = agent.mounted_mcp ?? []
    const added = next.filter((id) => !prev.includes(id))
    const removed = prev.filter((id) => !next.includes(id))
    if (!added.length && !removed.length) return
    try {
      let latest: AgentDefinition | null = agent
      for (const id of added) {
        latest = await mcpApi.mount(id, agent.id)
      }
      for (const id of removed) {
        latest = await mcpApi.unmount(id, agent.id)
      }
      if (latest) onUpdated(latest)
    } catch (e) {
      message.error(`MCP 挂载变更失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // ── 身份 + 运行参数批量保存（agentApi.update）──
  const handleSaveRuntime = async () => {
    if (!nameVal.trim()) {
      message.warning('名称不能为空')
      return
    }
    if (!roleVal.trim()) {
      message.warning('角色不能为空')
      return
    }
    setSaving(true)
    try {
      const payload = {
        name: nameVal.trim(),
        role: roleVal.trim(),
        description: descVal.trim(),
        system_prompt: sysPromptVal,
        model: modelVal,
        max_turns: maxTurns ?? 0,
        allowed_tools: allowedTools,
        denied_tools: deniedTools,
      }
      const updated = await agentApi.update(agent.id, payload)
      if (updated) {
        onUpdated(updated)
        message.success('已保存')
      }
      setOpen(false)
    } catch (e) {
      message.error(`更新失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  // Select options：已挂载项也保留（显示当前选中），未挂载项可新增选中
  const skillOptions = useMemo(() => {
    const mounted = agent.mounted_skills ?? []
    const mountedNotInList = mounted
      .filter((id) => !skills.some((s) => s.id === id))
      .map((id) => ({ id, name: id, description: null }))
    return [...skills, ...mountedNotInList]
  }, [skills, agent.mounted_skills])

  const mcpOptions = useMemo(() => {
    const mounted = agent.mounted_mcp ?? []
    const mountedNotInList = mounted
      .filter((id) => !mcps.some((m) => m.id === id))
      .map((id) => ({ id, name: id, transport: '—' as const }))
    return [...mcps, ...mountedNotInList]
  }, [mcps, agent.mounted_mcp])

  return (
    <>
      <Button
        size={small ? 'small' : 'middle'}
        type="text"
        icon={<EditOutlined />}
        onClick={openModal}
      >
        编辑
      </Button>
      <Modal
        title={`编辑智能体 · ${agent.name}`}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSaveRuntime}
        okText="保存"
        confirmLoading={saving}
        width={640}
        destroyOnHidden
      >
        {/* 身份与行为——点底部「保存」生效 */}
        <Divider plain style={{ marginTop: 0 }}>身份与行为</Divider>
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>名称</div>
          <Input
            value={nameVal}
            onChange={(e) => setNameVal(e.target.value)}
            placeholder="智能体名称"
          />
        </div>
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>角色</div>
          <Input
            value={roleVal}
            onChange={(e) => setRoleVal(e.target.value)}
            placeholder="如：后端开发工程师"
          />
        </div>
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>定位描述</div>
          <Input.TextArea
            value={descVal}
            onChange={(e) => setDescVal(e.target.value)}
            placeholder="一句话说明这个智能体的职责（可选）"
            autoSize={{ minRows: 1, maxRows: 3 }}
          />
        </div>
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>系统提示词（system_prompt）</div>
          <Input.TextArea
            value={sysPromptVal}
            onChange={(e) => setSysPromptVal(e.target.value)}
            placeholder="定义该智能体的行为准则、语气、约束。留空则用角色模板默认。"
            autoSize={{ minRows: 3, maxRows: 10 }}
          />
        </div>

        {/* 内建能力——系统预置工具，所有执行者自带，只读，折叠展示（BUILTIN_TOOLS） */}
        <Collapse
          ghost
          size="small"
          style={{ marginBottom: 16 }}
          items={[{
            key: 'builtin-tools',
            label: (
              <span style={{ fontSize: 12, color: '#999' }}>
                <LockOutlined style={{ marginRight: 4 }} />
                内建能力（系统预置 · 只读）
              </span>
            ),
            children: (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {BUILTIN_TOOLS.map((t) => (
                  <div
                    key={t.name}
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: 8,
                      fontSize: 12,
                      color: '#666',
                    }}
                  >
                    <Tag color="default" style={{ margin: 0, fontFamily: 'monospace' }}>{t.name}</Tag>
                    <span>{t.desc}</span>
                  </div>
                ))}
                <div style={{ fontSize: 11, color: '#bbb', marginTop: 2 }}>
                  执行任务时自动绑定到当前群组工作区，无需配置、不可增删。
                </div>
              </div>
            ),
          }]}
        />

        {/* 已挂载技能——即时 mount/unmount */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 6 }}>已挂载技能（即时生效）</div>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            placeholder="选择要挂载的技能"
            value={agent.mounted_skills ?? []}
            onChange={(v: string[]) => void handleSkillsChange(v)}
            options={skillOptions.map((s) => ({
              value: s.id,
              label: s.name + (s.description ? ` — ${s.description}` : ''),
            }))}
            loading={optsLoading}
            allowClear
          />
        </div>

        {/* 已挂载 MCP——即时 mount/unmount */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: '#999', marginBottom: 6 }}>已挂载 MCP（即时生效）</div>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            placeholder="选择要挂载的 MCP 连接"
            value={agent.mounted_mcp ?? []}
            onChange={(v: string[]) => void handleMcpsChange(v)}
            options={mcpOptions.map((m) => ({
              value: m.id,
              label: `${m.name} (${m.transport})`,
            }))}
            loading={optsLoading}
            allowClear
          />
        </div>

        {/* 运行参数段先隐藏（state 与 payload 保留，保存身份时不误改 model/tools；
            需要时把下面的 showRuntime 改 true 即可恢复）。 */}
        {showRuntime && (
          <>
            <Divider plain>运行参数</Divider>
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
              <div>
                <div style={{ fontSize: 12, marginBottom: 4 }}>模型</div>
                <Input
                  value={modelVal}
                  onChange={(e) => setModelVal(e.target.value)}
                  placeholder="留空用全局默认模型"
                />
              </div>
              <div>
                <div style={{ fontSize: 12, marginBottom: 4 }}>最大轮次（0 = 不限）</div>
                <Input
                  type="number"
                  value={maxTurns ?? ''}
                  onChange={(e) => {
                    const n = e.target.value === '' ? null : Number(e.target.value)
                    setMaxTurns(n != null && Number.isFinite(n) ? Math.max(0, Math.floor(n)) : null)
                  }}
                  placeholder="0"
                />
              </div>
              <div>
                <div style={{ fontSize: 12, marginBottom: 4 }}>工具白名单（allowed_tools）</div>
                <Select
                  mode="tags"
                  style={{ width: '100%' }}
                  placeholder="输入工具名回车添加"
                  value={allowedTools}
                  onChange={(v: string[]) => setAllowedTools(v)}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, marginBottom: 4 }}>工具黑名单（denied_tools）</div>
                <Select
                  mode="tags"
                  style={{ width: '100%' }}
                  placeholder="输入工具名回车添加"
                  value={deniedTools}
                  onChange={(v: string[]) => setDeniedTools(v)}
                />
              </div>
            </Space>
          </>
        )}
      </Modal>
    </>
  )
}
