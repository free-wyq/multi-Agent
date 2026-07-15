import { useEffect, useState, useMemo, useCallback, useRef } from 'react'
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
  Upload,
  Collapse,
  Typography,
} from 'antd'

import {
  PlusOutlined,
  LinkOutlined,
  DeleteOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  UploadOutlined,
  AppstoreOutlined,
  ShopOutlined,
  ReloadOutlined,
  DownloadOutlined,
  EyeOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons'
import {
  skillApi,
  agentApi,
  type Skill,
  type AgentDefinition,
  type SkillMarketEntry,
  type SkillRunEvent,
} from '../services/api'

/* source → Tag 颜色 */
const SOURCE_COLOR: Record<string, string> = {
  builtin: 'green',
  custom: 'blue',
  market: 'orange',
}

/* 市场 hub → Tag 颜色（catalog 内置 / remote 远程 Hub） */
const HUB_COLOR: Record<string, string> = {
  catalog: 'green',
  remote: 'blue',
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
  const [uploading, setUploading] = useState(false)

  // SK-10 技能市场 Tab
  const [tabKey, setTabKey] = useState<'mine' | 'market'>('mine')
  const [marketEntries, setMarketEntries] = useState<SkillMarketEntry[]>([])
  const [marketLoading, setMarketLoading] = useState(false)
  const [marketSearch, setMarketSearch] = useState('')
  // 市场是否已加载过（懒加载：切到市场 Tab 才拉，避免首屏多余请求）
  const [marketLoaded, setMarketLoaded] = useState(false)
  // 市场技能详情预览 Modal
  const [previewEntry, setPreviewEntry] = useState<SkillMarketEntry | null>(null)
  // 已安装 entry_id 集合（按 content/name 判重，避免重复安装，SK-12 实现后改为真实安装）
  const [installingIds, setInstallingIds] = useState<Set<string>>(new Set())

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

  /* ── SK-10 技能市场搜索 ──
   * 市场搜索交给后端 search_market（catalog + remote overlay），前端只传 q。
   * 懒加载：首次切到市场 Tab 才拉一次（空 q 返回全部 catalog）；
   * 用户输入关键词后点搜索/回车再按需重拉（与本地 Tab 的「实时过滤」不同，
   * 市场搜索走 HTTP，实时过滤会打太多请求；用显式搜索更克制）。 */
  const fetchMarket = useCallback(
    async (q: string = marketSearch) => {
      setMarketLoading(true)
      try {
        const entries = await skillApi.searchMarket(q.trim(), 50)
        setMarketEntries(entries)
        setMarketLoaded(true)
      } catch (e) {
        message.error(e instanceof Error ? e.message : '搜索技能市场失败')
      } finally {
        setMarketLoading(false)
      }
    },
    [marketSearch],
  )

  // 切到市场 Tab 时懒加载一次（仅未加载过时）
  useEffect(() => {
    if (tabKey === 'market' && !marketLoaded) {
      fetchMarket('')
    }
  }, [tabKey, marketLoaded, fetchMarket])

  /* 本地已安装技能的「指纹」集合，用于市场卡片判断是否已安装（避免重复安装）。
   * 市场条目与本地 Skill 无共享 id（entry_id vs skill id），用 name 做近似判重——
   * 本地存在同名 Skill 即视为已安装（catalog 技能名是中文，碰撞概率极低）。
   * SK-12 真实安装落地后 source=market 的本地 skill 会出现在列表里，name 判重即
   * 命中；且安装按钮在 Popconfirm 确认后调 installMarket，已安装时 disabled 拦截。 */
  const installedFingerprints = useMemo(() => {
    const set = new Set<string>()
    skills.forEach((s) => {
      const key = (s.name || '').trim().toLowerCase()
      if (key) set.add(key)
    })
    return set
  }, [skills])

  const isMarketInstalled = useCallback(
    (entry: SkillMarketEntry) => {
      // 用 name 判重：本地 Skill 有同名即视为已安装（catalog 技能名是中文，碰撞概率极低）
      return installedFingerprints.has((entry.name || '').trim().toLowerCase())
    },
    [installedFingerprints],
  )

  /* SK-12 一键安装市场技能：调真实 installMarket 端点，按 entry_id 在后端解析
   * 全文落库（catalog 自带 content / remote 按 source_url best-effort 拉取），
   * source 标 market。前端只传 entry_id——content 真源在后端 skill_hub，避免
   * 前端持全文与服务端漂移，且 remote 条目前端拿不到 content 必须 后端按 id 拉取。
   * 安装成功后 fetchAll 刷新「我的技能」列表，isMarketInstalled 据新 skills 重新
   * 计算，市场卡片按钮自动转为「已安装」disabled。 */
  const handleInstall = async (entry: SkillMarketEntry) => {
    setInstallingIds((prev) => new Set(prev).add(entry.entry_id))
    try {
      await skillApi.installMarket(entry.entry_id)
      message.success(`已安装市场技能「${entry.name}」`)
      await fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '安装失败')
    } finally {
      setInstallingIds((prev) => {
        const next = new Set(prev)
        next.delete(entry.entry_id)
        return next
      })
    }
  }

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

  /* ── 运行可执行技能（阶段四·task39）──
   * 仅 requires_tools 非空的技能可运行。点击 → skillApi.run(id) 流式展示执行过程
   * （CodeBuddy 气泡：reasoning + token + 产物卡）。runOpen 持当前运行技能 + 累积
   * 的事件文本（token 逐字拼接成正文、think 进推理折叠区、tool_* 进工具调用区）。
   * runDone 持最终 done 事件（ok + output_path + products）。
   */
  const [runOpen, setRunOpen] = useState(false)
  const [runSkillId, setRunSkillId] = useState<string | null>(null)
  const [runTokenText, setRunTokenText] = useState('')
  const [runThinkText, setRunThinkText] = useState('')
  const [runTools, setRunTools] = useState<{ name: string; output: string }[]>([])
  const [runDone, setRunDone] = useState<SkillRunEvent | null>(null)
  const [runLoading, setRunLoading] = useState(false)
  const runStopRef = useRef<(() => void) | null>(null)

  const handleRun = (skill: Skill) => {
    if (!(skill.requires_tools && skill.requires_tools.length > 0)) return
    setRunSkillId(skill.id)
    setRunTokenText('')
    setRunThinkText('')
    setRunTools([])
    setRunDone(null)
    setRunLoading(true)
    setRunOpen(true)
    // 中止上一轮（若有）
    runStopRef.current?.()
    runStopRef.current = skillApi.run(skill.id, (ev: SkillRunEvent) => {
      if (ev.kind === 'token') {
        setRunTokenText((prev) => prev + (ev.content ?? ''))
      } else if (ev.kind === 'think') {
        setRunThinkText((prev) => prev + (ev.content ?? ''))
      } else if (ev.kind === 'tool_start') {
        const name = (ev.data && typeof ev.data === 'object' && 'name' in ev.data)
          ? String(ev.data.name) : 'tool'
        setRunTools((prev) => [...prev, { name, output: '' }])
      } else if (ev.kind === 'tool_end') {
        const name = (ev.data && typeof ev.data === 'object' && 'name' in ev.data)
          ? String(ev.data.name) : 'tool'
        const out = (ev.data && typeof ev.data === 'object' && 'output' in ev.data)
          ? String(ev.data.output) : (ev.content ?? '')
        setRunTools((prev) => {
          const copy = [...prev]
          // 找最后一个同名工具，填上输出
          for (let i = copy.length - 1; i >= 0; i--) {
            if (copy[i].name === name && copy[i].output === '') {
              copy[i] = { name, output: out }
              break
            }
          }
          return copy
        })
      } else if (ev.kind === 'done') {
        setRunDone(ev)
        setRunLoading(false)
      } else if (ev.kind === 'log' && ev.content) {
        // log 事件（开始/完成/错误）轻提示，避免与正文区冲突
        if (ev.content.includes('错误') || ev.content.includes('失败')) {
          message.warning(ev.content)
        }
      }
    })
  }

  const handleRunClose = () => {
    runStopRef.current?.()
    runStopRef.current = null
    setRunOpen(false)
    setRunLoading(false)
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

  /* ── SK-05 上传 SKILL.md ──
   * rc-upload 的 beforeUpload 返回 false 拦截自动上传，由 customRequest 接管。
   * 只接受 .md/.markdown 文本；校验通过后调 skillApi.upload，成功刷新列表。
   * name/description 不强制用户填——后端 name 缺省回退文件 stem，description 可空。
   * tags 留空走后端缺省 []，不要求用户在上传时分类。 */
  const handleUpload = async (file: File) => {
    const lower = file.name.toLowerCase()
    if (!lower.endsWith('.md') && !lower.endsWith('.markdown')) {
      message.error('仅支持上传 .md / .markdown 技能文档文件')
      return Upload.LIST_IGNORE
    }
    setUploading(true)
    try {
      const skill = await skillApi.upload(file)
      message.success(`已上传技能「${skill.name}」`)
      await fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '上传失败')
    } finally {
      setUploading(false)
    }
    // 返回 false 阻止 antd Upload 的默认 action 上传行为（我们已自行 fetch）
    return false
  }

  const agentOptions = agents.map((a) => ({ value: a.id, label: a.name }))

  return (
    <div
      style={{
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 20,
        background: 'var(--surface-main)',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* L4-02：迁 /skills 全屏路由，根容器加 height:100%+overflowY:auto 接通高度链。
          原 SH-05 降级为抽屉 Tab 时移除了页级 h2，全屏路由下保留 Tabs 直接展示。 */}

      <Tabs
        activeKey={tabKey}
        onChange={(k) => setTabKey(k as 'mine' | 'market')}
        items={[
          {
            key: 'mine',
            label: (
              <span>
                <AppstoreOutlined style={{ marginRight: 4 }} />
                我的技能
              </span>
            ),
            children: (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                  <span style={{ color: '#999', fontSize: 13 }}>
                    已入库 {skills.length} 个技能，可挂载到智能体
                  </span>
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
                    <Upload
                      accept=".md,.markdown"
                      showUploadList={false}
                      beforeUpload={handleUpload}
                      disabled={uploading}
                    >
                      <Button icon={<UploadOutlined />} loading={uploading}>
                        上传 SKILL.md
                      </Button>
                    </Upload>
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
                            ...(skill.requires_tools && skill.requires_tools.length > 0
                              ? [
                                  <Tooltip title="运行此技能（带受控工具+沙箱）" key="run">
                                    <Button
                                      type="text"
                                      icon={<PlayCircleOutlined />}
                                      onClick={() => handleRun(skill)}
                                    >
                                      运行
                                    </Button>
                                  </Tooltip>,
                                ]
                              : []),
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
              </>
            ),
          },
          {
            key: 'market',
            label: (
              <span>
                <ShopOutlined style={{ marginRight: 4 }} />
                技能市场
              </span>
            ),
            children: (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                  <span style={{ color: '#999', fontSize: 13 }}>
                    浏览并一键安装市场技能（内置市场 + 远程 Hub 叠加）
                  </span>
                  <Space>
                    <Input
                      placeholder="搜索市场技能名称 / 描述 / 标签"
                      prefix={<SearchOutlined />}
                      allowClear
                      style={{ width: 300 }}
                      value={marketSearch}
                      onChange={(e) => setMarketSearch(e.target.value)}
                      onPressEnter={() => fetchMarket()}
                    />
                    <Button
                      icon={<SearchOutlined />}
                      onClick={() => fetchMarket()}
                      loading={marketLoading}
                    >
                      搜索
                    </Button>
                    <Tooltip title="重新拉取市场技能">
                      <Button
                        icon={<ReloadOutlined />}
                        onClick={() => fetchMarket(marketSearch)}
                        disabled={marketLoading}
                      />
                    </Tooltip>
                  </Space>
                </div>

                <Spin spinning={marketLoading}>
                  {marketEntries.length === 0 && !marketLoading ? (
                    <Empty description={marketSearch ? '市场没有匹配的技能' : '市场暂无可选技能'} />
                  ) : (
                    <Space wrap>
                      {marketEntries.map((entry) => {
                        const installed = isMarketInstalled(entry)
                        const installing = installingIds.has(entry.entry_id)
                        return (
                          <Card
                            key={entry.entry_id}
                            title={
                              <Space>
                                <span>{entry.name}</span>
                                <Tag color={HUB_COLOR[entry.hub] ?? 'default'}>
                                  {entry.hub === 'catalog' ? '内置市场' : entry.hub}
                                </Tag>
                                {entry.version && (
                                  <span style={{ color: '#999', fontSize: 12 }}>v{entry.version}</span>
                                )}
                              </Space>
                            }
                            style={{ width: 300 }}
                            actions={[
                              <Tooltip
                                title={entry.content ? '查看技能文档全文' : '该技能暂无文档预览'}
                                key="preview"
                              >
                                <Button
                                  type="text"
                                  icon={<EyeOutlined />}
                                  disabled={!entry.content}
                                  onClick={() => setPreviewEntry(entry)}
                                >
                                  预览
                                </Button>
                              </Tooltip>,
                              <Popconfirm
                                key="install"
                                title="确认安装该市场技能？"
                                description="安装后出现在「我的技能」中，可挂载到智能体"
                                onConfirm={() => handleInstall(entry)}
                                okText="安装"
                                cancelText="取消"
                                disabled={installed || installing}
                              >
                                <Button
                                  type="text"
                                  icon={<DownloadOutlined />}
                                  loading={installing}
                                  disabled={installed}
                                >
                                  {installed ? '已安装' : '安装'}
                                </Button>
                              </Popconfirm>,
                            ]}
                          >
                            <p style={{ minHeight: 40, color: '#666' }}>
                              {entry.description || '暂无描述'}
                            </p>

                            {entry.tags && entry.tags.length > 0 && (
                              <div style={{ marginBottom: 8 }}>
                                <Space wrap>
                                  {entry.tags.map((t) => (
                                    <Tag key={t}>{t}</Tag>
                                  ))}
                                </Space>
                              </div>
                            )}

                            <div style={{ fontSize: 12, color: '#999' }}>
                              {entry.author && <div>来源：{entry.author}</div>}
                              {entry.source_url && (
                                <Tooltip title={entry.source_url}>
                                  <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    <LinkOutlined style={{ marginRight: 4 }} />
                                    {entry.source_url}
                                  </div>
                                </Tooltip>
                              )}
                              {!entry.author && !entry.source_url && <span>市场技能 · 待安装</span>}
                            </div>
                          </Card>
                        )
                      })}
                    </Space>
                  )}
                </Spin>
              </>
            ),
          },
        ]}
      />

      {/* ── 市场技能预览 Modal ── */}
      <Modal
        open={!!previewEntry}
        title={previewEntry?.name ?? '技能文档'}
        onCancel={() => setPreviewEntry(null)}
        footer={
          previewEntry ? (
            <Space>
              <Button onClick={() => setPreviewEntry(null)}>关闭</Button>
              <Button
                type="primary"
                icon={<DownloadOutlined />}
                loading={installingIds.has(previewEntry.entry_id)}
                disabled={isMarketInstalled(previewEntry)}
                onClick={() => handleInstall(previewEntry)}
              >
                {isMarketInstalled(previewEntry) ? '已安装' : '安装到我的技能'}
              </Button>
            </Space>
          ) : null
        }
        width={640}
      >
        {previewEntry?.content ? (
          <pre style={{ maxHeight: 480, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', background: '#fafafa', padding: 12, borderRadius: 6, fontSize: 13, margin: 0 }}>
            {previewEntry.content}
          </pre>
        ) : (
          <Empty description="该市场技能暂无文档全文（远程技能需安装后拉取）" />
        )}
      </Modal>

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

      {/* 运行技能 Modal（阶段四·task39）：CodeBuddy 气泡模式展示执行过程 */}
      <Modal
        open={runOpen}
        title={
          <Space>
            <PlayCircleOutlined />
            <span>运行技能：{skills.find((s) => s.id === runSkillId)?.name ?? runSkillId}</span>
            {runLoading && <Tag color="processing">运行中</Tag>}
            {runDone && (runDone.ok ? <Tag color="success">完成</Tag> : <Tag color="error">失败</Tag>)}
          </Space>
        }
        width={680}
        footer={[
          <Button key="close" onClick={handleRunClose}>
            {runLoading ? '后台继续' : '关闭'}
          </Button>,
        ]}
        onCancel={handleRunClose}
      >
        <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
          {/* 工具调用区 */}
          {runTools.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>工具调用</div>
              {runTools.map((t, i) => (
                <div
                  key={i}
                  style={{
                    background: 'var(--surface-elevated, #fafafa)',
                    border: '1px solid #f0f0f0',
                    borderRadius: 6,
                    padding: '6px 10px',
                    marginBottom: 4,
                    fontSize: 12,
                  }}
                >
                  <Typography.Text strong style={{ fontSize: 12 }}>🔧 {t.name}</Typography.Text>
                  {t.output && (
                    <pre style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap', fontSize: 11, color: '#666' }}>
                      {t.output.slice(0, 500)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* 推理折叠区 */}
          {runThinkText && (
            <Collapse
              size="small"
              style={{ marginBottom: 12 }}
              defaultActiveKey={['think']}
              items={[
                {
                  key: 'think',
                  label: '思考过程',
                  children: (
                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: '#888' }}>
                      {runThinkText}
                    </Typography.Paragraph>
                  ),
                },
              ]}
            />
          )}

          {/* 正文区（token 流式拼接） */}
          {runTokenText && (
            <div
              style={{
                background: 'var(--surface-elevated, #fafafa)',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
                padding: 12,
                whiteSpace: 'pre-wrap',
                fontSize: 13,
              }}
            >
              {runTokenText}
            </div>
          )}

          {/* 产物卡 */}
          {runDone && runDone.ok && runDone.products && runDone.products.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>产物</div>
              {runDone.products.map((p) => (
                <Tag key={p} color="green" style={{ marginBottom: 4 }}>📦 {p}</Tag>
              ))}
            </div>
          )}

          {/* 运行中提示 */}
          {runLoading && !runTokenText && !runThinkText && runTools.length === 0 && (
            <div style={{ textAlign: 'center', color: '#999', padding: 20 }}>
              <Spin /> <span style={{ marginLeft: 8 }}>技能运行中…</span>
            </div>
          )}

          {/* 失败提示 */}
          {runDone && !runDone.ok && runDone.error && (
            <div style={{ marginTop: 12, color: '#cf1322', fontSize: 13 }}>
              ❌ 运行失败：{runDone.error}
            </div>
          )}
        </div>
      </Modal>
    </div>
  )
}
