import { useEffect, useState, useMemo, useCallback } from 'react'
import {
  Card,
  Button,
  Modal,
  Select,
  message,
  Space,
  Tag,
  Popconfirm,
  Spin,
  Empty,
  Input,
  Form,
  Tabs,
  Tooltip,
} from 'antd'
import {
  PlusOutlined,
  LinkOutlined,
  DeleteOutlined,
  SearchOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { skillApi, agentApi, type Skill, type AgentDefinition } from '../services/api'

/* source → Tag 颜色 */
const SOURCE_COLOR: Record<string, string> = {
  builtin: 'green',
  custom: 'blue',
  market: 'orange',
}

export default function SkillPage() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')

  // 挂载 Modal
  const [mountOpen, setMountOpen] = useState(false)
  const [activeSkill, setActiveSkill] = useState<Skill | null>(null)
  const [mountAgentIds, setMountAgentIds] = useState<string[]>([])
  const [mountLoading, setMountLoading] = useState(false)

  // 创建 Modal
  const [createOpen, setCreateOpen] = useState(false)
  const [createTab, setCreateTab] = useState<'manual' | 'generate'>('manual')
  const [createForm] = Form.useForm()
  const [genForm] = Form.useForm()
  const [genLoading, setGenLoading] = useState(false)

  // agent id → name 映射
  const agentNameMap = useMemo(() => {
    const m = new Map<string, string>()
    agents.forEach((a) => m.set(a.id, a.name))
    return m
  }, [agents])

  const fetchAll = useCallback(async () => {
    setLoading(true)
    try {
      const [skillList, agentList] = await Promise.all([
        skillApi.list(),
        agentApi.list(),
      ])
      setSkills(skillList)
      setAgents(agentList)
    } catch {
      message.error('获取技能列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  // 前端搜索过滤
  const filteredSkills = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return skills
    return skills.filter((s) => {
      return (
        s.name.toLowerCase().includes(q) ||
        (s.description ?? '').toLowerCase().includes(q) ||
        (s.tags ?? []).some((t) => t.toLowerCase().includes(q))
      )
    })
  }, [skills, search])

  /* ── 挂载/卸载 ── */
  const openMount = (skill: Skill) => {
    setActiveSkill(skill)
    setMountAgentIds(skill.mounted_to ?? [])
    setMountOpen(true)
  }

  const handleMount = async () => {
    if (!activeSkill) return
    setMountLoading(true)
    try {
      const prev = new Set(activeSkill.mounted_to ?? [])
      const next = new Set(mountAgentIds)
      const skillId = activeSkill.id

      // 新增的调 mount
      for (const aid of mountAgentIds) {
        if (!prev.has(aid)) {
          await skillApi.mount(skillId, aid)
        }
      }
      // 移除的调 unmount
      for (const aid of Array.from(prev)) {
        if (!next.has(aid)) {
          await skillApi.unmount(skillId, aid)
        }
      }
      message.success(`已更新「${activeSkill.name}」的挂载关系`)
      setMountOpen(false)
      setActiveSkill(null)
      await fetchAll()
    } catch {
      message.error('更新挂载关系失败')
    } finally {
      setMountLoading(false)
    }
  }

  /* ── 删除 ── */
  const handleDelete = async (skill: Skill) => {
    try {
      await skillApi.delete(skill.id)
      message.success(`已删除「${skill.name}」`)
      await fetchAll()
    } catch {
      message.error('删除失败')
    }
  }

  /* ── 手动创建 ── */
  const handleManualCreate = async (values: {
    name: string
    description?: string
    content?: string
    tags?: string[]
  }) => {
    try {
      await skillApi.create({
        name: values.name,
        description: values.description,
        content: values.content,
        source: 'custom',
        tags: values.tags ?? [],
      })
      message.success('技能创建成功')
      createForm.resetFields()
      setCreateOpen(false)
      await fetchAll()
    } catch {
      message.error('创建失败')
    }
  }

  /* ── 自然语言生成 ── */
  const handleGenerate = async () => {
    try {
      const values = await genForm.validateFields()
      setGenLoading(true)
      await skillApi.generate(values.description)
      message.success('技能生成成功')
      genForm.resetFields()
      setCreateOpen(false)
      await fetchAll()
    } catch {
      message.error('生成失败')
    } finally {
      setGenLoading(false)
    }
  }

  const agentOptions = agents.map((a) => ({ value: a.id, label: a.name }))

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>技能市场</h2>
        <Space>
          <Input
            placeholder="搜索技能名称 / 描述 / 标签"
            prefix={<SearchOutlined />}
            allowClear
            style={{ width: 260 }}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setCreateTab('manual')
              setCreateOpen(true)
            }}
          >
            提交技能
          </Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {filteredSkills.length === 0 && !loading ? (
          <Empty description={search ? '没有匹配的技能' : '暂无技能'} />
        ) : (
          <Space wrap>
            {filteredSkills.map((skill) => (
              <Card
                key={skill.id}
                title={
                  <Space>
                    <span>{skill.name}</span>
                    <Tag color={SOURCE_COLOR[skill.source] ?? 'default'}>
                      {skill.source}
                    </Tag>
                  </Space>
                }
                style={{ width: 300 }}
                actions={[
                  <Tooltip title="挂载到智能体" key="mount">
                    <Button
                      type="text"
                      icon={<LinkOutlined />}
                      onClick={() => openMount(skill)}
                    >
                      挂载
                    </Button>
                  </Tooltip>,
                  <Popconfirm
                    key="delete"
                    title="确认删除该技能？"
                    description="删除后不可恢复"
                    onConfirm={() => handleDelete(skill)}
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                  >
                    <Button type="text" danger icon={<DeleteOutlined />}>
                      删除
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <p style={{ minHeight: 40, color: '#666' }}>
                  {skill.description || '暂无描述'}
                </p>

                {skill.tags && skill.tags.length > 0 && (
                  <div style={{ marginBottom: 8 }}>
                    <Space wrap>
                      {skill.tags.map((t) => (
                        <Tag key={t}>{t}</Tag>
                      ))}
                    </Space>
                  </div>
                )}

                <div>
                  {(skill.mounted_to ?? []).length === 0 ? (
                    <span style={{ color: '#999', fontSize: 12 }}>未挂载到任何智能体</span>
                  ) : (
                    <>
                      <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>
                        已挂载 ({skill.mounted_to.length}):
                      </div>
                      <Space wrap>
                        {skill.mounted_to.map((id) => (
                          <Tag key={id} color="blue">
                            {agentNameMap.get(id) ?? id}
                          </Tag>
                        ))}
                      </Space>
                    </>
                  )}
                </div>
              </Card>
            ))}
          </Space>
        )}
      </Spin>

      {/* ── 挂载 Modal ── */}
      <Modal
        open={mountOpen}
        title={`挂载技能 —— ${activeSkill?.name ?? ''}`}
        onCancel={() => {
          setMountOpen(false)
          setActiveSkill(null)
        }}
        onOk={handleMount}
        confirmLoading={mountLoading}
        okText="保存"
        cancelText="取消"
      >
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="选择要挂载的智能体"
          value={mountAgentIds}
          onChange={setMountAgentIds}
          options={agentOptions}
        />
      </Modal>

      {/* ── 创建技能 Modal ── */}
      <Modal
        open={createOpen}
        title="提交技能"
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
          genForm.resetFields()
        }}
        footer={null}
        destroyOnClose
        width={560}
      >
        <Tabs
          activeKey={createTab}
          onChange={(k) => setCreateTab(k as 'manual' | 'generate')}
          items={[
            {
              key: 'manual',
              label: '手动创建',
              children: (
                <Form
                  form={createForm}
                  layout="vertical"
                  onFinish={handleManualCreate}
                  style={{ marginTop: 8 }}
                >
                  <Form.Item
                    name="name"
                    label="技能名称"
                    rules={[{ required: true, message: '请输入技能名称' }]}
                  >
                    <Input placeholder="如：Python 开发" autoComplete="off" />
                  </Form.Item>
                  <Form.Item name="description" label="描述">
                    <Input.TextArea
                      rows={2}
                      placeholder="技能的简要描述"
                    />
                  </Form.Item>
                  <Form.Item name="content" label="技能内容">
                    <Input.TextArea
                      rows={4}
                      placeholder="技能的详细内容（提示词 / 文档）"
                    />
                  </Form.Item>
                  <Form.Item name="tags" label="标签">
                    <Select
                      mode="tags"
                      placeholder="输入标签后回车"
                      tokenSeparators={[',']}
                      allowClear
                    />
                  </Form.Item>
                  <div style={{ textAlign: 'right' }}>
                    <Space>
                      <Button onClick={() => setCreateOpen(false)}>取消</Button>
                      <Button type="primary" htmlType="submit">创建</Button>
                    </Space>
                  </div>
                </Form>
              ),
            },
            {
              key: 'generate',
              label: (
                <Space>
                  <ThunderboltOutlined />
                  自然语言生成
                </Space>
              ),
              children: (
                <Form
                  form={genForm}
                  layout="vertical"
                  style={{ marginTop: 8 }}
                >
                  <Form.Item
                    name="description"
                    label="技能描述"
                    rules={[{ required: true, message: '请输入技能描述' }]}
                    extra="用自然语言描述你想要的技能，LLM 将自动生成技能文档"
                  >
                    <Input.TextArea
                      rows={5}
                      placeholder="如：帮我生成一个用于 PostgreSQL 慢查询分析的技能，能自动识别全表扫描并给出索引建议"
                    />
                  </Form.Item>
                  <div style={{ textAlign: 'right' }}>
                    <Space>
                      <Button onClick={() => setCreateOpen(false)}>取消</Button>
                      <Button
                        type="primary"
                        loading={genLoading}
                        onClick={handleGenerate}
                      >
                        {genLoading ? '生成中...' : '生成技能'}
                      </Button>
                    </Space>
                  </div>
                </Form>
              ),
            },
          ]}
        />
      </Modal>
    </div>
  )
}
