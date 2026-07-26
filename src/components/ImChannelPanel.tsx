import { useEffect, useMemo, useState } from 'react'
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
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  ExperimentOutlined,
  MessageOutlined,
} from '@ant-design/icons'
import {
  imChannelApi,
  conversationApi,
  groupApi,
  agentApi,
  type ImChannel,
  type ImChannelCreatePayload,
  type ImChannelTestResult,
  type Conversation,
  type Group,
  type AgentDefinition,
} from '../services/api'

/* 平台元信息：Tag 颜色 + 中文名 + 出站日志路径提示。
 * 三平台对齐后端 ADAPTERS 注册表（wechat/dingtalk/feishu，任务19b）。
 * mock 阶段 send_outbound 走 logger.info（无真实 HTTP），档四换成 httpx push 时改这里
 * 的「出站」提示文案即可，组件结构不动。 */
const PLATFORM_META: Record<
  string,
  { color: string; label: string; desc: string }
> = {
  wechat: {
    color: 'green',
    label: '企业微信',
    desc: '企业微信回调，接收好友/群消息转发给智能体，回复回推到会话',
  },
  dingtalk: {
    color: 'blue',
    label: '钉钉',
    desc: '钉钉机器人，接收群 @ 消息转发给智能体，回复推送到钉钉群',
  },
  feishu: {
    color: 'geekblue',
    label: '飞书',
    desc: '飞书机器人，接收飞书消息转发给智能体，回复推送到飞书会话',
  },
}

function platformMeta(platform: string) {
  return (
    PLATFORM_META[platform] ?? {
      color: 'default',
      label: platform,
      desc: '未知平台（后端未注册 adapter，入站会被 400 拒绝）',
    }
  )
}

/** 平台 config 表单字段约定（mock 阶段三平台统一）：
 *  - app_id / app_secret：应用凭证（app_secret 脱敏）
 *  - verify_token：回调校验 token（脱敏）
 *  - webhook_url：出站推送端点（mock 阶段不用，档四 httpx 推送目标）
 *  - default_session：出站默认平台会话（出站钩子 _resolve_target 用）
 * 档四各平台字段差异（钉钉 timestamp+sign / 飞书 encrypt_key）可在此按 platform 分流，
 * 但 MVP 三平台同构，统一表单即可。 */
type ConfigFormValues = {
  app_id?: string
  app_secret?: string
  verify_token?: string
  webhook_url?: string
  default_session?: string
}

/** 创建/编辑表单值。config 子字段以明文录入，提交时组装成 config dict。
 *  target_kind 用 Segmented（single/group），target_conversation_id 用 Select（联动
 *  已有会话/群组列表）。编辑态下 config 拉回的是脱敏副本（***），用户不改的密钥
 *  传 *** 回去，后端 _merge_masked_config 会 merge 回原值（与 mcpApi 同款）。 */
type ChannelFormValues = {
  name: string
  platform: string
  target_kind: 'single' | 'group'
  target_conversation_id: string
  target_agent_id?: string
  enabled: boolean
  outbound_log: boolean
  config: ConfigFormValues
}

/** 平台 Segmented 选项（图标 + 中文名）。 */
const PLATFORM_OPTIONS = Object.entries(PLATFORM_META).map(([value, meta]) => ({
  value,
  label: (
    <Space size={4}>
      <MessageOutlined />
      <span>{meta.label}</span>
    </Space>
  ),
}))

export default function ImChannelPanel() {
  const [channels, setChannels] = useState<ImChannel[]>([])
  const [loading, setLoading] = useState(false)
  /* 启停 / 测试 切换中 id（防重复点击，单卡独立 loading）。 */
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set())
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set())
  /* 每个渠道最近一次 test 结果（ok/error 内联展示，档四真实平台失败时一眼可见）。 */
  const [testResults, setTestResults] = useState<Record<string, ImChannelTestResult>>({})

  /* 创建/编辑 Modal。editing=null 走新增态，非 null 走编辑态（灌入脱敏 config）。 */
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<ImChannel | null>(null)
  const [submitLoading, setSubmitLoading] = useState(false)
  const [form] = Form.useForm<ChannelFormValues>()
  const targetKind = Form.useWatch('target_kind', form) ?? 'single'

  /* 目标会话/群组候选 + agent 候选（target_conversation_id / target_agent_id Select 用）。
   * target_kind=single → 列单聊 Conversation；target_kind=group → 列群组 Group。
   * 编辑态灌值时若目标不在候选里（已被删的会话），Select 仍能显示原 id（value 透传）。 */
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])

  const fetchAll = async () => {
    setLoading(true)
    try {
      const [channelList, convList, groupList, agentList] = await Promise.all([
        imChannelApi.list(),
        conversationApi.list(),
        groupApi.list(),
        agentApi.list(),
      ])
      setChannels(channelList)
      setConversations(convList)
      setGroups(groupList)
      setAgents(agentList)
    } catch (e) {
      message.error(e instanceof Error ? e.message : '获取 IM 渠道列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
  }, [])

  /* target_conversation_id Select 候选项（按 target_kind 分流）。 */
  const targetOptions = useMemo(() => {
    if (targetKind === 'group') {
      return groups.map((g) => ({ value: g.id, label: `${g.name}（${g.id}）` }))
    }
    return conversations.map((c) => ({ value: c.id, label: `${c.name}（${c.id}）` }))
  }, [targetKind, groups, conversations])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({
      platform: 'wechat',
      target_kind: 'single',
      enabled: false,
      outbound_log: true,
      config: {},
    })
    setModalOpen(true)
  }

  const openEdit = (ch: ImChannel) => {
    setEditing(ch)
    form.resetFields()
    /* 编辑态：config 是脱敏副本（***）；用户不改的密钥字段保持 *** 回传，后端 merge。
     * target_kind / target_conversation_id / 开关原样灌入。 */
    const cfg = (ch.config ?? {}) as ConfigFormValues
    form.setFieldsValue({
      name: ch.name,
      platform: ch.platform,
      target_kind: (ch.target_kind === 'group' ? 'group' : 'single'),
      target_conversation_id: ch.target_conversation_id,
      target_agent_id: ch.target_agent_id || undefined,
      enabled: ch.enabled,
      outbound_log: ch.outbound_log,
      config: {
        app_id: (cfg.app_id as string) || '',
        app_secret: (cfg.app_secret as string) || '',
        verify_token: (cfg.verify_token as string) || '',
        webhook_url: (cfg.webhook_url as string) || '',
        default_session: (cfg.default_session as string) || '',
      },
    })
    setModalOpen(true)
  }

  /* 提交创建/编辑。config 组装：过滤空串字段（避免落空值覆盖），密钥字段 *** 透传
   * （编辑态未改 → 后端 merge 回原值；新增态 *** 无意义但后端会落 ***，故新增态
   * 空串字段直接 omit）。 */
  const handleSubmit = async () => {
    let values: ChannelFormValues
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSubmitLoading(true)
    try {
      const cfg: Record<string, string> = {}
      const cfgIn = values.config ?? {}
      ;(['app_id', 'app_secret', 'verify_token', 'webhook_url', 'default_session'] as const).forEach(
        (k) => {
          const v = (cfgIn as Record<string, unknown>)[k]
          if (typeof v === 'string' && v !== '') cfg[k] = v
        },
      )
      const payload: ImChannelCreatePayload = {
        name: values.name,
        platform: values.platform,
        target_conversation_id: values.target_conversation_id,
        target_kind: values.target_kind,
        target_agent_id: values.target_agent_id ?? '',
        enabled: values.enabled,
        outbound_log: values.outbound_log,
        config: cfg,
      }
      if (editing) {
        const updated = await imChannelApi.update(editing.id, payload)
        if (updated) {
          message.success(`已更新「${updated.name}」`)
        } else {
          message.error('渠道不存在或已被删除')
        }
      } else {
        const created = await imChannelApi.create(payload)
        message.success(`已创建「${created.name}」`)
      }
      setModalOpen(false)
      await fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSubmitLoading(false)
    }
  }

  const handleToggleEnabled = async (ch: ImChannel) => {
    setTogglingIds((prev) => new Set(prev).add(ch.id))
    try {
      const next = ch.enabled
        ? await imChannelApi.disable(ch.id)
        : await imChannelApi.enable(ch.id)
      if (next) {
        setChannels((prev) => prev.map((c) => (c.id === ch.id ? next : c)))
        message.success(`已${next.enabled ? '启用' : '禁用'}「${next.name}」`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '操作失败')
    } finally {
      setTogglingIds((prev) => {
        const n = new Set(prev)
        n.delete(ch.id)
        return n
      })
    }
  }

  const handleDelete = async (ch: ImChannel) => {
    try {
      await imChannelApi.delete(ch.id)
      message.success(`已删除「${ch.name}」`)
      setTestResults((prev) => {
        const cp = { ...prev }
        delete cp[ch.id]
        return cp
      })
      fetchAll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : '删除失败')
    }
  }

  /* mock 出站探针：触发后端 adapter.send_outbound（logger.info），返回 ok/error。
   * ok=True 表示 adapter 加载 + 出站日志行触发（e2e caplog 断言该行）；档四真实
   * 平台 ok=True 表示 HTTP 推送成功。失败内联展示 error（永不 500）。 */
  const handleTest = async (ch: ImChannel) => {
    setTestingIds((prev) => new Set(prev).add(ch.id))
    try {
      const result = await imChannelApi.test(ch.id)
      setTestResults((prev) => ({ ...prev, [ch.id]: result }))
      if (result.ok) {
        message.success(`「${ch.name}」出站探针成功（mock 日志已记录）`)
      } else {
        message.warning(`「${ch.name}」探针失败：${result.error ?? '未知原因'}`)
      }
    } catch (e) {
      message.error(e instanceof Error ? e.message : '测试失败')
    } finally {
      setTestingIds((prev) => {
        const n = new Set(prev)
        n.delete(ch.id)
        return n
      })
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
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        <span style={{ color: '#999', fontSize: 13 }}>
          已配置 {channels.length} 个 IM 渠道，入站消息转发给智能体，回复回推到平台
        </span>
        <Space>
          <Tooltip title="重新拉取渠道列表">
            <Button icon={<ReloadOutlined />} onClick={fetchAll} disabled={loading} />
          </Tooltip>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            添加渠道
          </Button>
        </Space>
      </div>

      <Spin spinning={loading}>
        {channels.length === 0 && !loading ? (
          <Empty description="暂无 IM 渠道，点击「添加渠道」接入企业微信 / 钉钉 / 飞书" />
        ) : (
          <Space wrap align="start">
            {channels.map((ch) => {
              const meta = platformMeta(ch.platform)
              const result = testResults[ch.id]
              return (
                <Card
                  key={ch.id}
                  style={{ width: 360 }}
                  title={
                    <Space>
                      <span style={{ fontWeight: 600 }}>{ch.name}</span>
                      <Tag color={meta.color}>{meta.label}</Tag>
                      {!ch.enabled && <Tag color="default">已禁用</Tag>}
                      {ch.enabled && <Tag color="success">已启用</Tag>}
                    </Space>
                  }
                  actions={[
                    <Tooltip
                      title={ch.enabled ? '禁用渠道（入站 410 + 出站静默）' : '启用渠道'}
                      key="toggle"
                    >
                      <Button
                        type="text"
                        icon={<PoweroffOutlined />}
                        loading={togglingIds.has(ch.id)}
                        onClick={() => handleToggleEnabled(ch)}
                      >
                        {ch.enabled ? '禁用' : '启用'}
                      </Button>
                    </Tooltip>,
                    <Tooltip title="发送 mock 出站探针（验证 adapter 加载 + 出站日志）" key="test">
                      <Button
                        type="text"
                        icon={<ExperimentOutlined />}
                        loading={testingIds.has(ch.id)}
                        onClick={() => handleTest(ch)}
                      >
                        测试
                      </Button>
                    </Tooltip>,
                    <Popconfirm
                      key="delete"
                      title="确认删除该 IM 渠道？"
                      description="删除后入站回调立即失效，已绑定会话不再收到平台消息"
                      onConfirm={() => handleDelete(ch)}
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
                  {/* 投递目标：单聊 conversation / 群聊 group */}
                  <div style={{ minHeight: 48 }}>
                    <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>
                      投递目标（{ch.target_kind === 'group' ? '群聊' : '单聊'}）
                    </div>
                    <div
                      style={{
                        fontSize: 13,
                        fontFamily: 'monospace',
                        wordBreak: 'break-all',
                        color: ch.target_conversation_id ? '#333' : '#ccc',
                      }}
                    >
                      {ch.target_conversation_id || '（未配置目标）'}
                    </div>
                    {ch.target_agent_id && (
                      <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
                        绑定智能体：{ch.target_agent_id}
                      </div>
                    )}
                  </div>

                  {/* 平台描述 */}
                  <div style={{ fontSize: 12, color: '#999', marginTop: 8, lineHeight: 1.5 }}>
                    {meta.desc}
                  </div>

                  {/* config 摘要（脱敏后的非空字段，密钥显 ***） */}
                  {ch.config && Object.keys(ch.config).length > 0 && (
                    <div style={{ marginTop: 8 }}>
                      <span style={{ fontSize: 12, color: '#999', marginRight: 6 }}>凭证:</span>
                      <Space wrap size={[4, 4]}>
                        {Object.entries(ch.config).map(([k, v]) => (
                          <Tag key={k} style={{ fontSize: 11, margin: 0 }}>
                            {k}={String(v)}
                          </Tag>
                        ))}
                      </Space>
                    </div>
                  )}

                  {/* mock 出站日志开关（outbound_log=False 时出站钩子跳过该 channel） */}
                  <div style={{ marginTop: 8, fontSize: 12, color: '#999' }}>
                    出站日志：{ch.outbound_log ? '已记录（mock logger.info）' : '已关闭（出站跳过）'}
                  </div>

                  {/* 最近一次 test 结果（内联展示，失败时一眼可见） */}
                  {result && (
                    <div
                      style={{
                        marginTop: 8,
                        fontSize: 12,
                        color: result.ok ? '#52c41a' : '#ff4d4f',
                      }}
                    >
                      最近测试：{result.ok ? '✓ 探针成功' : `✗ ${result.error ?? '失败'}`}
                    </div>
                  )}

                  <Button
                    type="link"
                    size="small"
                    style={{ paddingLeft: 0, marginTop: 8 }}
                    onClick={() => openEdit(ch)}
                  >
                    编辑配置
                  </Button>
                </Card>
              )
            })}
          </Space>
        )}
      </Spin>

      {/* ── 创建/编辑 Modal ──
       * platform Segmented 三选一（wechat/dingtalk/feishu）；target_kind Segmented
       * single/group 驱动 target_conversation_id 候选分流；config 子表单录入平台凭证
       * （app_id/app_secret/verify_token/webhook_url/default_session）。编辑态 config
       * 拉回脱敏副本（***），用户不改的密钥字段回传 ***，后端 merge 保留原值。 */}
      <Modal
        open={modalOpen}
        title={editing ? '编辑 IM 渠道' : '添加 IM 渠道'}
        onCancel={() => setModalOpen(false)}
        confirmLoading={submitLoading}
        okText="保存"
        cancelText="取消"
        onOk={handleSubmit}
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="name"
            label="渠道名称"
            rules={[{ required: true, message: '请输入渠道名称' }]}
          >
            <Input placeholder="如：客服微信群" autoComplete="off" />
          </Form.Item>

          <Form.Item name="platform" label="平台" rules={[{ required: true }]}>
            <Segmented block options={PLATFORM_OPTIONS} />
          </Form.Item>

          <Form.Item name="target_kind" label="投递目标类型" rules={[{ required: true }]}>
            <Segmented
              block
              options={[
                { value: 'single', label: '单聊（route_direct_message）' },
                { value: 'group', label: '群聊（route_user_message）' },
              ]}
            />
          </Form.Item>

          <Form.Item
            name="target_conversation_id"
            label="投递目标"
            rules={[{ required: true, message: '请选择投递目标' }]}
            tooltip="入站消息投递到的内部会话/群组；单聊选 Conversation，群聊选 Group"
          >
            <Select
              placeholder={targetKind === 'group' ? '选择群组' : '选择单聊会话'}
              showSearch
              optionFilterProp="label"
              options={targetOptions}
            />
          </Form.Item>

          <Form.Item
            name="target_agent_id"
            label="绑定智能体（可选）"
            tooltip="单聊场景即 conversation.agent_id；群聊可空，走 @mention 路由"
          >
            <Select
              placeholder="选择智能体（群聊可空）"
              allowClear
              showSearch
              optionFilterProp="label"
              options={agents.map((a) => ({
                value: a.id,
                label: `${a.name}（${a.role}）`,
              }))}
            />
          </Form.Item>

          {/* ── 平台凭证 config 子表单 ──
           * 三平台 MVP 统一字段；app_secret/verify_token 脱敏回显（***）。
           * 档四各平台特异字段可按 platform 分流显隐，此处统一保持简单。 */}
          <div style={{ fontSize: 12, color: '#999', marginBottom: 8 }}>
            平台凭证（app_secret / verify_token 脱敏回显，未改字段回传 *** 保留原值）
          </div>
          <Form.Item name={['config', 'app_id']} label="App ID">
            <Input placeholder="应用 ID" autoComplete="off" />
          </Form.Item>
          <Form.Item name={['config', 'app_secret']} label="App Secret">
            <Input.Password
              placeholder="应用密钥（编辑态 *** 表示未改）"
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item name={['config', 'verify_token']} label="Verify Token">
            <Input.Password
              placeholder="回调校验 token（编辑态 *** 表示未改）"
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item name={['config', 'webhook_url']} label="Webhook URL">
            <Input placeholder="出站推送端点（mock 阶段不用）" autoComplete="off" />
          </Form.Item>
          <Form.Item
            name={['config', 'default_session']}
            label="默认平台会话"
            tooltip="出站钩子解析的目标平台会话（config.default_session，缺省为 'default'）"
          >
            <Input placeholder="如：external_session_id" autoComplete="off" />
          </Form.Item>

          <Space size="large">
            <Form.Item name="enabled" label="启用渠道" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="outbound_log" label="记录出站日志" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  )
}
