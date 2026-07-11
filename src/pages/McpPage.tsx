import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
  Collapse,
} from 'antd'
import {
  PlusOutlined,
  ApiOutlined,
  DeleteOutlined,
  PoweroffOutlined,
  ToolOutlined,
  ReloadOutlined,
  CodeOutlined,
  CloudOutlined,
} from '@ant-design/icons'
import {
  mcpApi,
  type McpConnection,
  type McpConnectionCreatePayload,
  type McpToolInfo,
} from '../services/api'

/* transport → Tag 颜色 + 图标 */
const TRANSPORT_META: Record<
  string,
  { color: string; icon: React.ReactNode; label: string }
> = {
  stdio: { color: 'geekblue', icon: <CodeOutlined />, label: 'stdio' },
  sse: { color: 'purple', icon: <CloudOutlined />, label: 'sse' },
}

function transportMeta(transport: string) {
  return TRANSPORT_META[transport] ?? { color: 'default', icon: <ApiOutlined />, label: transport }
}

/* 拼接 stdio 启动命令预览：command + args.join(' ')，便于一眼看出会 spawn 什么。 */
function stdioCommandPreview(conn: McpConnection): string {
  const parts = [conn.command, ...(conn.args ?? [])].filter(Boolean)
  return parts.length ? parts.join(' ') : '（未配置命令）'
}

export default function McpPage() {
  const [connections, setConnections] = useState<McpConnection[]>([])
  const [loading, setLoading] = useState(false)
  /* 工具预览：按 mcp_id 缓存自省结果，展开 Collapse 时懒拉取（list_mcp_tools 会
   * 实际 spawn/连接 MCP server，开销大，按需触发而非首屏全拉）。 */
  const [toolsCache, setToolsCache] = useState<Record<string, McpToolInfo[]>>({})
  const [toolsLoadingIds, setToolsLoadingIds] = useState<Set<string>>(new Set())
  /* enable/disable 切换中 id（防重复点击，单卡独立 loading）。 */
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set())

  /* MC-02: 添加连接 Modal。
   * transport 切换显隐 stdio(command/args/env) vs sse(url/headers) 字段；
   * Modal 自带 Form 实例，提交时按 transport 校验必填项（stdio→command / sse→url），
   * 不相关字段不传（undefined 不经 JSON 序列化，后端 McpConnectionCreatePayload 全可选）。 */
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  /* 表单值类型：args/env/url/headers/headers 均为文本域 string，提交时再转
   * args→string[] / env→dict / headers→dict（McpConnectionCreatePayload 要求）。 */
  type CreateFormValues = {
    name: string
    transport: 'stdio' | 'sse'
    command?: string
    args?: string // 文本域：每行一个参数
    env?: string // 文本域：JSON 对象
    url?: string
    headers?: string // 文本域：JSON 对象
    enabled?: boolean
  }
  const [form] = Form.useForm<CreateFormValues>()
  const transport = Form.useWatch('transport', form) ?? 'stdio'

  const openCreate = () => {
    form.resetFields()
    // 默认 stdio + 启用；args/env/headers 文本域留空走后端缺省
    form.setFieldsValue({
      transport: 'stdio',
      enabled: true,
    })
    setCreateOpen(true)
  }

  /* 提交创建：按 transport 收集相关字段，调 mcpApi.create 落库。
   * stdio 必填 command（args/env 可选），sse 必填 url（headers 可选）；
   * 不相关字段显式 omit，避免 stdio 连接残留 url 字段污染 payload。
   * args 文本域按行拆成 string[]，env/headers 文本域 JSON.parse 成 dict。
   * 解析失败时 message.error 提示并 abort（JSON 格式错误非字段必填，Form 校验管不到）。 */
  const handleCreate = async () => {
    let values: CreateFormValues
    try {
      values = await form.validateFields()
    } catch {
      return // 字段校验失败，Form 已标红，不重复 message
    }
    setCreateLoading(true)
    try {
      const payload: McpConnectionCreatePayload = {
        name: values.name,
        transport: values.transport,
        enabled: values.enabled ?? true,
      }
      if (values.transport === 'stdio') {
        payload.command = values.command ?? ''
        // args 文本域按行拆分，去空行
        const args = (values.args ?? '')
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean)
        payload.args = args
        // env 文本域 JSON.parse
        const envRaw = (values.env ?? '').trim()
        if (envRaw) {
          try {
            const env = JSON.parse(envRaw)
            if (env && typeof env === 'object' && !Array.isArray(env)) {
              payload.env = env as Record<string, string>
            } else {
              message.error('环境变量必须是 JSON 对象')
              return
            }
          } catch {
            message.error('环境变量 JSON 格式错误')
            return
          }
        }
      } else {
        payload.url = values.url ?? ''
        const headersRaw = (values.headers ?? '').trim()
        if (headersRaw) {
          try {
            const headers = JSON.parse(headersRaw)
            if (headers && typeof headers === 'object' && !Array.isArray(headers)) {
              payload.headers = headers
            } else {
              message.error('请求头必须是 JSON 对象')
              return
            }
          } catch {
            message.error('请求头 JSON 格式错误')
            return
          }
        }
      }
      const conn = await mcpApi.create(payload)
      message.success(`已添加「${conn.name}」连接`)
      setCreateOpen(false)
      form.resetFields()
      await fetchConnections()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '创建失败')
    } finally {
      setCreateLoading(false)
    }
  }

  const fetchConnections = async () => {
    setLoading(true)
    try {
      const list = await mcpApi.list()
      setConnections(list)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '获取 MCP 连接列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchConnections()
  }, [])

  /* 拉取某连接暴露的工具列表（自省，展开 Collapse 时懒触发）。
   * 后端只加载 enabled 的连接，禁用连接返回空列表——禁用时展示「已禁用，无工具」。 */
  const fetchTools = async (conn: McpConnection) => {
    if (toolsCache[conn.id] || toolsLoadingIds.has(conn.id)) return
    setToolsLoadingIds((prev) => new Set(prev).add(conn.id))
    try {
      const tools = await mcpApi.tools(conn.id)
      setToolsCache((prev) => ({ ...prev, [conn.id]: tools }))
    } catch (e) {
      message.error(e instanceof Error ? e.message : `获取「${conn.name}」工具列表失败`)
    } finally {
      setToolsLoadingIds((prev) => {
        const next = new Set(prev)
        next.delete(conn.id)
        return next
      })
    }
  }

  /* MC-03: 启用/禁用切换。enabled → disable，disabled → enable。 */
  const handleToggleEnabled = async (conn: McpConnection) => {
    setTogglingIds((prev) => new Set(prev).add(conn.id))
    try {
      const next = conn.enabled
        ? await mcpApi.disable(conn.id)
        : await mcpApi.enable(conn.id)
      if (next) {
        setConnections((prev) =>
          prev.map((c) => (c.id === conn.id ? next : c)),
        )
        // 禁用后工具预览失效（后端只加载 enabled），清缓存让下次展开重拉
        if (!next.enabled) {
          setToolsCache((prev) => {
            const cp = { ...prev }
            delete cp[conn.id]
            return cp
          })
        }
        message.success(`已${next.enabled ? '启用' : '禁用'}「${next.name}」`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败')
    } finally {
      setTogglingIds((prev) => {
        const next = new Set(prev)
        next.delete(conn.id)
        return next
      })
    }
  }

  /* MC-04: 删除连接（后端级联从所有 agent.mounted_mcp 移除引用）。 */
  const handleDelete = async (conn: McpConnection) => {
    try {
      await mcpApi.delete(conn.id)
      message.success(`已删除「${conn.name}」`)
      setToolsCache((prev) => {
        const cp = { ...prev }
        delete cp[conn.id]
        return cp
      })
      fetchConnections()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  return (
    <div
      style={{
        maxWidth: 1200,
        margin: '0 auto',
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        padding: 16,
      }}
    >
      {/* L4-03：迁 /mcp 全屏路由，根容器加 height:100%+overflowY:auto 接通高度链。
          原 SH-05 降级为抽屉 Tab 时移除了页级 h2，全屏路由下保留 maxWidth 居中可读。 */}

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        <span style={{ color: '#999', fontSize: 13 }}>
          已配置 {connections.length} 个 MCP 连接，可挂载到智能体扩展工具能力
        </span>
        <Space>
          <Tooltip title="重新拉取连接列表">
            <Button icon={<ReloadOutlined />} onClick={fetchConnections} disabled={loading} />
          </Tooltip>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
          >
            添加连接
          </Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {connections.length === 0 && !loading ? (
          <Empty description="暂无 MCP 连接，点击「添加连接」配置外部工具源" />
        ) : (
          <Space wrap align="start">
            {connections.map((conn) => {
              const meta = transportMeta(conn.transport)
              const tools = toolsCache[conn.id]
              const toolsLoading = toolsLoadingIds.has(conn.id)
              return (
                <Card
                  key={conn.id}
                  style={{ width: 340 }}
                  title={
                    <Space>
                      <span style={{ fontWeight: 600 }}>{conn.name}</span>
                      <Tag color={meta.color}>
                        {meta.icon} {meta.label}
                      </Tag>
                      {!conn.enabled && <Tag color="default">已禁用</Tag>}
                    </Space>
                  }
                  actions={[
                    <Tooltip
                      title={conn.enabled ? '禁用连接' : '启用连接'}
                      key="toggle"
                    >
                      <Button
                        type="text"
                        icon={<PoweroffOutlined />}
                        loading={togglingIds.has(conn.id)}
                        onClick={() => handleToggleEnabled(conn)}
                      >
                        {conn.enabled ? '禁用' : '启用'}
                      </Button>
                    </Tooltip>,
                    <Popconfirm
                      key="delete"
                      title="确认删除该 MCP 连接？"
                      description="删除后将从所有已挂载智能体移除"
                      onConfirm={() => handleDelete(conn)}
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
                  {/* 传输详情：stdio 显命令预览，sse 显 URL */}
                  <div style={{ minHeight: 48 }}>
                    {conn.transport === 'sse' ? (
                      <>
                        <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>
                          端点 URL
                        </div>
                        <div
                          style={{
                            fontSize: 13,
                            fontFamily: 'monospace',
                            wordBreak: 'break-all',
                            color: conn.url ? '#333' : '#ccc',
                          }}
                        >
                          {conn.url || '（未配置 URL）'}
                        </div>
                      </>
                    ) : (
                      <>
                        <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>
                          启动命令
                        </div>
                        <div
                          style={{
                            fontSize: 13,
                            fontFamily: 'monospace',
                            wordBreak: 'break-all',
                            color: conn.command ? '#333' : '#ccc',
                          }}
                        >
                          {stdioCommandPreview(conn)}
                        </div>
                      </>
                    )}
                  </div>

                  {/* 环境变量（stdio，非空才显） */}
                  {conn.transport === 'stdio' && conn.env && Object.keys(conn.env).length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <span style={{ fontSize: 12, color: '#999', marginRight: 6 }}>环境变量:</span>
                      <Space wrap size={[4, 4]}>
                        {Object.entries(conn.env).map(([k, v]) => (
                          <Tag key={k} style={{ fontSize: 11, margin: 0 }}>
                            {k}={v}
                          </Tag>
                        ))}
                      </Space>
                    </div>
                  )}

                  {/* 工具列表预览（自省，展开 Collapse 懒拉取） */}
                  <Collapse
                    ghost
                    size="small"
                    style={{ marginTop: 8, marginLeft: -12, marginRight: -12 }}
                    onChange={(keys) => {
                      if (keys.includes('tools') && conn.enabled) {
                        fetchTools(conn)
                      }
                    }}
                    items={[
                      {
                        key: 'tools',
                        label: (
                          <Space size={4}>
                            <ToolOutlined />
                            <span>暴露工具</span>
                            {tools && (
                              <Tag style={{ margin: 0, fontSize: 11 }}>{tools.length}</Tag>
                            )}
                          </Space>
                        ),
                        children: (
                          <>
                            {!conn.enabled ? (
                              <span style={{ color: '#999', fontSize: 12 }}>
                                连接已禁用，无工具（启用后可预览）
                              </span>
                            ) : toolsLoading ? (
                              <span style={{ color: '#999', fontSize: 12 }}>加载中…</span>
                            ) : tools && tools.length > 0 ? (
                              <Space wrap size={[4, 4]}>
                                {tools.map((t, i) => (
                                  <Tag key={t.name ?? i} color="blue" style={{ fontSize: 11, margin: 0 }}>
                                    {t.name}
                                  </Tag>
                                ))}
                              </Space>
                            ) : (
                              <span style={{ color: '#999', fontSize: 12 }}>
                                {tools ? '无工具（连接未暴露工具或自省失败）' : '点击展开加载…'}
                              </span>
                            )}
                          </>
                        ),
                      },
                    ]}
                  />
                </Card>
              )
            })}
          </Space>
        )}
      </Spin>

      {/* ── MC-02: 添加连接 Modal ──
       * transport 用 Segmented 切换（stdio/sse 二选一），Form.useWatch 监听 transport
       * 控制字段显隐。stdio 必填 command（args/env 可选），sse 必填 url（headers 可选）。
       * env/headers 用 JSON 文本域录入（key-value 透传后端，后端用 dict 接收）。
       * Form.useForm + Modal 配合：footer 自定义（取消/确定按钮调 handleCreate）。 */}
      <Modal
        open={createOpen}
        title="添加 MCP 连接"
        onCancel={() => setCreateOpen(false)}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
        onOk={handleCreate}
        destroyOnClose
        width={560}
      >
        <Form
          form={form}
          layout="vertical"
          style={{ marginTop: 12 }}
        >
          <Form.Item
            name="name"
            label="连接名称"
            rules={[{ required: true, message: '请输入连接名称' }]}
          >
            <Input placeholder="如：文件系统 MCP" autoComplete="off" />
          </Form.Item>

          <Form.Item name="transport" label="传输方式" rules={[{ required: true }]}>
            <Segmented
              block
              options={[
                {
                  value: 'stdio',
                  label: (
                    <Space size={4}>
                      <CodeOutlined />
                      <span>stdio（本地命令）</span>
                    </Space>
                  ),
                },
                {
                  value: 'sse',
                  label: (
                    <Space size={4}>
                      <CloudOutlined />
                      <span>sse（远程端点）</span>
                    </Space>
                  ),
                },
              ]}
            />
          </Form.Item>

          {/* ── stdio 传输字段 ── */}
          {transport === 'stdio' && (
            <>
              <Form.Item
                name="command"
                label="启动命令"
                rules={[{ required: true, message: '请输入启动命令' }]}
                extra="实际 spawn 的可执行程序（如 npx / node / python3）"
              >
                <Input placeholder="npx" autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="args"
                label="参数"
                tooltip="每行一个参数，按顺序传给 command"
              >
                <Input.TextArea
                  rows={3}
                  placeholder={'-y\n@modelcontextprotocol/server-filesystem\n/tmp'}
                />
              </Form.Item>
              <Form.Item
                name="env"
                label="环境变量（JSON）"
                tooltip="传给子进程的环境变量，JSON 对象格式"
              >
                <Input.TextArea
                  rows={2}
                  placeholder='{"API_KEY": "sk-..."}'
                />
              </Form.Item>
            </>
          )}

          {/* ── sse 传输字段 ── */}
          {transport === 'sse' && (
            <>
              <Form.Item
                name="url"
                label="端点 URL"
                rules={[{ required: true, message: '请输入 SSE 端点 URL' }]}
                extra="远程 MCP server 的 SSE 端点地址"
              >
                <Input placeholder="http://127.0.0.1:8080/sse" autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="headers"
                label="请求头（JSON）"
                tooltip="连接 SSE 端点时携带的 HTTP 请求头"
              >
                <Input.TextArea
                  rows={2}
                  placeholder='{"Authorization": "Bearer ..."}'
                />
              </Form.Item>
            </>
          )}

          <Form.Item name="enabled" label="创建后启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
