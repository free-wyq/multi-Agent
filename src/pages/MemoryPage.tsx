/**
 * MemoryPage：L2 长期记忆管理页（PRD 记忆模块 · 任务17c）。
 *
 * 数据源：``memoryApi``（GET/POST/PUT/DELETE + enable/disable + search）——后端
 * ``/api/memory`` CRUD + FTS5 全文检索（任务17b）。本组件纯展示 + 管理：
 *
 *  - 顶部工具条：scope Segmented 筛选（全部/global/agent/conversation）+ enabled
 *    Select（全部/启用/禁用）+ keyword Input 实时 LIKE 过滤 + 「新增记忆」按钮 +
 *    Reload 刷新；
 *  - 记忆列表：卡片式，按 importance DESC 排序——每卡展示 content（多行可展开）+
 *    scope Tag + scope_ref（agent 名/会话 id）+ importance 滑条（可就地调）+
 *    enabled Switch + access_count/last_accessed_at「最近用过」+ 编辑/删除；
 *  - 新增/编辑 Modal：content 文本域 + scope Select + scope_ref Select（agent 列表）
 *    + importance 滑条 + enabled Switch；
 *  - 检索测试条：keyword 走 list 的 LIKE（管理用），独立 search 输入走 FTS5 bm25 排序
 *    （调试检索召回用，命中后 access_count 自增）。
 *
 * 与 McpPage/SkillPage 同款管理页骨架（列表 + 创建 Modal + enable/disable + 删除），
 * 区别在 scope 三档筛选 + importance 就地调节 + FTS5 检索调试入口。根容器
 * height:100%+overflowY:auto，塞进 SettingsModal 右侧内容区自适应。
 *
 * 设计文档：docs/memory-module-design.md（分层模型 / schema / 生命周期 / 检索方案）。
 * L2 与 L1 会话上下文物理隔离——此处只管 L2 持久记忆的 CRUD，不碰 L1。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
} from 'antd'
import {
  PlusOutlined,
  ReloadOutlined,
  DeleteOutlined,
  EditOutlined,
  SearchOutlined,
  DatabaseOutlined,
} from '@ant-design/icons'
import {
  agentApi,
  memoryApi,
  type AgentDefinition,
  type Memory,
  type MemoryCreatePayload,
  type MemoryScope,
  type MemoryUpdatePayload,
} from '../services/api'

/** scope → Tag 颜色 + 中文标签（与设计 §4.3 对齐，全应用一致）。 */
const SCOPE_META: Record<MemoryScope, { color: string; label: string; desc: string }> = {
  global: { color: 'green', label: '全局', desc: '所有会话都注入（用户画像）' },
  agent: { color: 'geekblue', label: '智能体', desc: '仅该智能体的会话注入' },
  conversation: { color: 'purple', label: '会话', desc: '仅该会话注入（跨轮次）' },
}

/** 顶部 scope 筛选 Segmented 选项（含「全部」）。 */
const SCOPE_FILTER_OPTIONS: { label: React.ReactNode; value: 'all' | MemoryScope }[] = [
  { label: <span>全部</span>, value: 'all' },
  { label: <span>全局</span>, value: 'global' },
  { label: <span>智能体</span>, value: 'agent' },
  { label: <span>会话</span>, value: 'conversation' },
]

/** enabled 筛选 Select 选项。 */
const ENABLED_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '仅启用', value: 'true' },
  { label: '仅禁用', value: 'false' },
]

/** 表单值类型（新增/编辑 Modal 用）。scope_ref 在 agent 档是 agent_id。 */
interface MemoryFormValues {
  content: string
  scope: MemoryScope
  scope_ref: string
  importance: number
  enabled: boolean
}

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [loading, setLoading] = useState(false)

  // 筛选 state
  const [scopeFilter, setScopeFilter] = useState<'all' | MemoryScope>('all')
  const [enabledFilter, setEnabledFilter] = useState<'all' | 'true' | 'false'>('all')
  const [keyword, setKeyword] = useState('')

  // 新增/编辑 Modal
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Memory | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<MemoryFormValues>()
  const formScope = Form.useWatch('scope', form) ?? 'global'

  // 检索调试（FTS5 bm25 排序，区别于 keyword 的 LIKE）
  const [searchQuery, setSearchQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchHits, setSearchHits] = useState<Memory[]>([])

  // 就地调 importance 的 id（防抖，单卡 loading）
  const [importanceEditingId, setImportanceEditingId] = useState<string | null>(null)

  // enable/disable 切换中 id
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set())

  // agent id → name 映射（scope_ref 显示用）
  const agentNameMap = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of agents) m.set(a.id, a.name)
    return m
  }, [agents])

  /** 拉取记忆列表（按当前筛选）。keyword 为空时不传 keyword 参数（后端返全部）。 */
  const fetchMemories = useCallback(async () => {
    setLoading(true)
    try {
      const list = await memoryApi.list({
        ...(scopeFilter !== 'all' ? { scope: scopeFilter } : {}),
        ...(enabledFilter !== 'all' ? { enabled: enabledFilter === 'true' } : {}),
        ...(keyword.trim() ? { keyword: keyword.trim() } : {}),
      })
      setMemories(list)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '获取记忆列表失败')
    } finally {
      setLoading(false)
    }
  }, [scopeFilter, enabledFilter, keyword])

  // 首屏拉 agents（scope_ref 下拉选项）+ memories
  useEffect(() => {
    agentApi
      .list()
      .then(setAgents)
      .catch(() => {
        /* agents 拉取失败不阻塞记忆列表展示，scope_ref 下拉为空即可 */
      })
    fetchMemories()
  }, [fetchMemories])

  // scope_ref 展示文案：agent 档显示 agent 名，conversation 档显示会话 id 截断，global 档空。
  const scopeRefLabel = (m: Memory): string => {
    if (m.scope === 'global' || !m.scope_ref) return ''
    if (m.scope === 'agent') return agentNameMap.get(m.scope_ref) ?? m.scope_ref
    // conversation
    return m.scope_ref
  }

  // ── 新增/编辑 Modal handlers ──

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({
      scope: 'global',
      scope_ref: '',
      importance: 1.0,
      enabled: true,
    })
    setFormOpen(true)
  }

  const openEdit = (m: Memory) => {
    setEditing(m)
    form.resetFields()
    form.setFieldsValue({
      content: m.content,
      scope: m.scope,
      scope_ref: m.scope_ref,
      importance: m.importance,
      enabled: m.enabled,
    })
    setFormOpen(true)
  }

  const handleSave = async () => {
    let values: MemoryFormValues
    try {
      values = await form.validateFields()
    } catch {
      return // 字段校验失败，Form 已标红
    }
    // scope=agent/conversation 必须带 scope_ref（后端 400 守卫，前端先拦避免一次往返）
    if (values.scope !== 'global' && !values.scope_ref.trim()) {
      message.error(`scope=${values.scope} 必须选择对应的${values.scope === 'agent' ? '智能体' : '会话'}`)
      return
    }
    setSaving(true)
    try {
      if (editing) {
        const payload: MemoryUpdatePayload = {
          content: values.content,
          scope: values.scope,
          scope_ref: values.scope === 'global' ? '' : values.scope_ref,
          importance: values.importance,
          enabled: values.enabled,
        }
        await memoryApi.update(editing.id, payload)
        message.success('记忆已更新')
      } else {
        const payload: MemoryCreatePayload = {
          content: values.content,
          scope: values.scope,
          scope_ref: values.scope === 'global' ? '' : values.scope_ref,
          importance: values.importance,
          enabled: values.enabled,
        }
        await memoryApi.create(payload)
        message.success('记忆已新增')
      }
      setFormOpen(false)
      setEditing(null)
      await fetchMemories()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // ── enable/disable 软删除切换 ──
  const handleToggleEnabled = async (m: Memory) => {
    setTogglingIds((prev) => new Set(prev).add(m.id))
    try {
      const next = m.enabled ? await memoryApi.disable(m.id) : await memoryApi.enable(m.id)
      if (next) {
        setMemories((prev) => prev.map((it) => (it.id === m.id ? next : it)))
        message.success(`已${next.enabled ? '启用' : '禁用'}该记忆`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败')
    } finally {
      setTogglingIds((prev) => {
        const n = new Set(prev)
        n.delete(m.id)
        return n
      })
    }
  }

  // ── 就地调 importance（滑条松手时 PUT）──
  const handleImportanceChange = async (m: Memory, next: number) => {
    setImportanceEditingId(m.id)
    try {
      await memoryApi.update(m.id, { importance: next })
      setMemories((prev) =>
        prev.map((it) => (it.id === m.id ? { ...it, importance: next } : it)),
      )
    } catch (e) {
      message.error(e instanceof Error ? e.message : '更新重要性失败')
    } finally {
      setImportanceEditingId(null)
    }
  }

  // ── 删除（硬删除，连 FTS5 sidecar）──
  const handleDelete = async (m: Memory) => {
    try {
      await memoryApi.remove(m.id)
      message.success('已删除该记忆')
      setMemories((prev) => prev.filter((it) => it.id !== m.id))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  // ── FTS5 检索调试（bm25 排序，区别于 keyword 的 LIKE）──
  const handleSearch = async () => {
    const q = searchQuery.trim()
    if (!q) {
      setSearchHits([])
      return
    }
    setSearching(true)
    try {
      const resp = await memoryApi.search(q, { top_k: 10 })
      setSearchHits(resp.results.map((r) => r.memory))
    } catch (e) {
      message.error(e instanceof Error ? e.message : '检索失败')
    } finally {
      setSearching(false)
    }
  }

  return (
    <div
      style={{
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 16,
      }}
    >
      {/* 顶部工具条：筛选 + 新增 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
          marginBottom: 12,
        }}
      >
        <Space wrap>
          <Segmented
            options={SCOPE_FILTER_OPTIONS}
            value={scopeFilter}
            onChange={(v) => setScopeFilter(v as 'all' | MemoryScope)}
          />
          <Select
            style={{ width: 110 }}
            value={enabledFilter}
            onChange={setEnabledFilter}
            options={ENABLED_OPTIONS}
          />
          <Input
            allowClear
            placeholder="关键字过滤（LIKE）"
            style={{ width: 200 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onPressEnter={fetchMemories}
            prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
          />
        </Space>
        <Space>
          <Tooltip title="重新拉取列表">
            <Button icon={<ReloadOutlined />} onClick={fetchMemories} disabled={loading} />
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增记忆
          </Button>
        </Space>
      </div>

      {/* FTS5 检索调试条：独立于 keyword LIKE，走 bm25+importance 排序 */}
      <Card size="small" style={{ marginBottom: 12, borderColor: '#ffd591' }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input
            placeholder="FTS5 全文检索（bm25 排序，调试注入召回用）"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onPressEnter={handleSearch}
            prefix={<DatabaseOutlined style={{ color: '#fa8c16' }} />}
          />
          <Button type="primary" onClick={handleSearch} loading={searching}>
            检索
          </Button>
        </Space.Compact>
        {searchHits.length > 0 && (
          <div style={{ marginTop: 8, fontSize: 12, color: '#999' }}>
            命中 {searchHits.length} 条（按 bm25+importance 排序，已触发 access_count 自增）：
            {searchHits.slice(0, 3).map((h, i) => (
              <Tag key={h.id} style={{ marginInlineStart: 4 }}>
                {i + 1}. {h.content.length > 24 ? h.content.slice(0, 24) + '…' : h.content}
              </Tag>
            ))}
            {searchHits.length > 3 && <Tag>+{searchHits.length - 3}</Tag>}
          </div>
        )}
      </Card>

      {/* 列表 */}
      <Spin spinning={loading}>
        {memories.length === 0 && !loading ? (
          <Empty description="暂无记忆，点击「新增记忆」录入用户画像 / 偏好 / 长期事实" />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {memories.map((m) => {
              const meta = SCOPE_META[m.scope]
              return (
                <Card
                  key={m.id}
                  size="small"
                  style={{
                    opacity: m.enabled ? 1 : 0.6,
                    borderColor: m.enabled ? '#f0f0f0' : '#d9d9d9',
                  }}
                >
                  {/* 第一行：scope Tag + scope_ref + enabled Switch + 编辑/删除 */}
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'flex-start',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Space wrap size={4}>
                        <Tag color={meta.color} style={{ margin: 0 }}>{meta.label}</Tag>
                        {scopeRefLabel(m) && (
                          <Tag style={{ margin: 0 }}>
                            {m.scope === 'agent' ? '👤 ' : '💬 '}
                            {scopeRefLabel(m)}
                          </Tag>
                        )}
                        {!m.enabled && <Tag color="default">已禁用</Tag>}
                        {m.access_count > 0 && (
                          <Tooltip title={m.last_accessed_at ? `最近命中: ${m.last_accessed_at}` : '曾被检索命中'}>
                            <Tag color="orange" style={{ margin: 0 }}>
                              命中 {m.access_count} 次
                            </Tag>
                          </Tooltip>
                        )}
                      </Space>
                    </div>
                    <Space size={4}>
                      <Tooltip title={m.enabled ? '禁用（软删除，不被检索命中）' : '启用（恢复检索）'}>
                        <Switch
                          size="small"
                          checked={m.enabled}
                          loading={togglingIds.has(m.id)}
                          onChange={() => handleToggleEnabled(m)}
                        />
                      </Tooltip>
                      <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(m)} />
                      <Popconfirm
                        title="确认删除该记忆？"
                        description="硬删除，连同检索索引一并清除，不可恢复。"
                        onConfirm={() => handleDelete(m)}
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                      >
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </Space>
                  </div>

                  {/* 第二行：content 正文（保留换行，长内容自然撑高） */}
                  <div
                    style={{
                      marginTop: 8,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      fontSize: 14,
                      lineHeight: 1.6,
                      color: m.enabled ? 'rgba(0,0,0,0.88)' : '#999',
                    }}
                  >
                    {m.content}
                  </div>

                  {/* 第三行：importance 滑条（就地调）+ 时间戳 */}
                  <div
                    style={{
                      marginTop: 8,
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: 12,
                      flexWrap: 'wrap',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 200 }}>
                      <span style={{ fontSize: 12, color: '#999', flexShrink: 0 }}>重要性</span>
                      <Slider
                        style={{ flex: 1, minWidth: 120, margin: 0 }}
                        min={0}
                        max={1}
                        step={0.1}
                        value={m.importance}
                        disabled={!!importanceEditingId && importanceEditingId !== m.id}
                        onChange={(v) =>
                          setMemories((prev) =>
                            prev.map((it) => (it.id === m.id ? { ...it, importance: v } : it)),
                          )
                        }
                        onAfterChange={(v) => handleImportanceChange(m, v)}
                        tooltip={{ formatter: (v) => v?.toFixed(1) }}
                      />
                      <span style={{ fontSize: 12, color: '#666', width: 28 }}>
                        {m.importance.toFixed(1)}
                      </span>
                    </div>
                    <span style={{ fontSize: 11, color: '#bbb' }}>
                      创建 {m.created_at ? m.created_at.slice(0, 16).replace('T', ' ') : '—'}
                    </span>
                  </div>
                </Card>
              )
            })}
          </div>
        )}
      </Spin>

      {/* 新增/编辑 Modal */}
      <Modal
        open={formOpen}
        title={editing ? '编辑记忆' : '新增记忆'}
        onCancel={() => {
          setFormOpen(false)
          setEditing(null)
        }}
        onOk={handleSave}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="content"
            label="记忆内容"
            rules={[{ required: true, message: '请输入记忆内容' }]}
            extra="自然语言陈述（如「用户是 Java 后端工程师，偏好简洁回复」）"
          >
            <Input.TextArea rows={4} placeholder="一条值得长期记住的事实 / 偏好 / 结论" />
          </Form.Item>
          <Form.Item label="作用域" required>
            <Space wrap>
              <Form.Item name="scope" noStyle rules={[{ required: true }]}>
                <Select
                  style={{ width: 140 }}
                  options={(Object.keys(SCOPE_META) as MemoryScope[]).map((s) => ({
                    value: s,
                    label: `${SCOPE_META[s].label}（${s}）`,
                  }))}
                />
              </Form.Item>
              {formScope !== 'global' && (
                <Form.Item
                  name="scope_ref"
                  noStyle
                  rules={[
                    {
                      required: true,
                      message: `${formScope === 'agent' ? '智能体' : '会话'}作用域必须选择对应对象`,
                    },
                  ]}
                >
                  {formScope === 'agent' ? (
                    <Select
                      style={{ width: 240 }}
                      placeholder="选择智能体"
                      options={agents.map((a) => ({ value: a.id, label: a.name }))}
                      notFoundContent={agents.length === 0 ? <Spin size="small" /> : '无可用智能体'}
                    />
                  ) : (
                    <Input style={{ width: 240 }} placeholder="会话 id（conversation_id）" />
                  )}
                </Form.Item>
              )}
            </Space>
            <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
              {formScope ? SCOPE_META[formScope].desc : ''}
            </div>
          </Form.Item>
          <Form.Item label={`重要性（importance）`} name="importance">
            <Slider min={0} max={1} step={0.1} tooltip={{ formatter: (v) => v?.toFixed(1) }} />
          </Form.Item>
          <Form.Item label="启用" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
