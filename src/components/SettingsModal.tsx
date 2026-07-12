/**
 * SettingsModal：设置弹窗（左右布局，非顶部 Tabs）。
 *
 * 为什么不用 antd Tabs（顶部 tabBar）：
 *  - 产品要求左侧 180px 导航列 + 右侧 flex:1 内容区，antd Tabs 默认顶部 tabBar 与此布局冲突；
 *  - 自己画左侧可点击导航行 + 右侧按 activeKey 条件渲染更直白，密度与 GroupInfoDrawer/AgentDetailPanel 一致。
 *
 * 七个设置项：MCP / 技能 / 记忆 / 模型服务商 / 外部系统 / 即时消息 / 用户信息。
 *  - MCP、技能直接复用全屏路由页（McpPage/SkillPage），它们自带数据拉取与 height:100%+overflowY:auto
 *    根容器，放进右侧时外层已 overflowY auto，让其自然铺；
 *  - 记忆是占位（后端 /api/memory 端点待补）；
 *  - 模型服务商：多 provider 管理（providerApi CRUD + activate），新增/编辑委托 ProviderEditor 组件
 *    （多模型目录 + 连接级配置；PE-07 替换原内联单模型 Form）。本组件只管列表展示 + 开关启用 + 删除。
 *  - 外部系统：智能体数据导出（文件下载 / Webhook 推送 / 数据库同步），占位卡片待后端端点。
 *  - 即时消息：接收外部 IM（微信/钉钉/飞书）消息转发给智能体，占位卡片待接入。
 *  - 用户信息：游客态占位，待接登录。
 */
import { useEffect, useState } from 'react'
import { Modal, Button, Spin, Empty, Tag, message, Switch, Popconfirm, Avatar, Slider, Select, Space, Tooltip } from 'antd'
import {
  ApiOutlined,
  AppstoreOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  PlusOutlined,
  UserOutlined,
  ExportOutlined,
  MessageOutlined,
  SoundOutlined,
} from '@ant-design/icons'
import McpPage from '../pages/McpPage'
import SkillPage from '../pages/SkillPage'
import ProviderEditor from './ProviderEditor'
import { providerApi, type LlmProvider } from '../services/api'
import { useSettings } from '../contexts/SettingsContext'
import { useTts } from '../hooks/useTts'

interface SettingsModalProps {
  open: boolean
  onClose: () => void
  /** 打开时默认聚焦的导航项（顶部栏头像入口传 'user'，其余默认 'mcp'）。
   *  destroyOnClose 下每次重开都按此重置。 */
  initialKey?: NavKey
}

/** 左侧导航项 key：联合类型供 activeKey/initialKey 共用（导出供 Layout 引用）。 */
export type NavKey = 'mcp' | 'skills' | 'memory' | 'model' | 'user' | 'external' | 'im' | 'tts'

/** 左侧导航项定义：key 唯一标识，用于 activeKey 条件渲染右侧内容。 */
interface NavItem {
  key: NavKey
  label: string
  icon: React.ReactNode
}

const NAV_ITEMS: NavItem[] = [
  { key: 'mcp', label: 'MCP', icon: <ApiOutlined /> },
  { key: 'skills', label: '技能', icon: <AppstoreOutlined /> },
  { key: 'memory', label: '记忆', icon: <DatabaseOutlined /> },
  { key: 'model', label: '模型服务商', icon: <CloudServerOutlined /> },
  { key: 'tts', label: '语音朗读', icon: <SoundOutlined /> },
  { key: 'external', label: '外部系统', icon: <ExportOutlined /> },
  { key: 'im', label: '即时消息', icon: <MessageOutlined /> },
  { key: 'user', label: '用户信息', icon: <UserOutlined /> },
]

/** 品牌蓝：仅用于选中项左条 + 选中文字强调，主体保持浅灰白。 */
const BRAND_BLUE = '#0A5ACF'

export default function SettingsModal({ open, onClose, initialKey = 'mcp' }: SettingsModalProps) {
  // 选中项：默认 MCP（头像入口可传 'user'）。destroyOnClose 卸载后再次打开按 initialKey 重置。
  const [activeKey, setActiveKey] = useState<NavKey>(initialKey)

  // initialKey 变化时同步（同一弹窗实例复用时，从头像进入 vs 从设置进入的默认页不同）。
  useEffect(() => {
    if (open) setActiveKey(initialKey)
  }, [open, initialKey])

  // ── 多服务商管理 state ──
  // 用户反馈：服务商是「一个列表 + 开关样式可选择启用谁」。每行一个 Switch = 启用/停用该服务商；
  // 任意时刻至多一个启用，开启一个会自动关闭其余（后端 single-active 不变式保证）。点击「编辑」
  // 仍可改 model/key 等字段；不再保留只读的 /api/config 快照表单（它与服务商列表信息重复）。
  const [providers, setProviders] = useState<LlmProvider[]>([])
  const [providersLoading, setProvidersLoading] = useState(false)
  const [providersLoaded, setProvidersLoaded] = useState(false)
  // 编辑/新增 Modal：editingProvider=null 走新增态，非 null 走编辑态（把整个 provider 对象传给
  // ProviderEditor，由其 providerToFormState 灌入；destroyOnClose 下每次重开重新初始化，无需同步）。
  const [formOpen, setFormOpen] = useState(false)
  const [editingProvider, setEditingProvider] = useState<LlmProvider | null>(null)
  // 开关切换中态（按 provider id 防抖，避免连点）
  const [togglingId, setTogglingId] = useState<string | null>(null)

  // 拉取服务商列表（切到 model tab 且未加载过时）
  useEffect(() => {
    if (!open || activeKey !== 'model' || providersLoaded) return
    refreshProviders()
  }, [open, activeKey, providersLoaded])

  const refreshProviders = () => {
    setProvidersLoading(true)
    providerApi
      .list()
      .then((list) => {
        setProviders(list)
        setProvidersLoaded(true)
      })
      .catch(() => {
        message.error('获取服务商列表失败')
      })
      .finally(() => setProvidersLoading(false))
  }

  // ── 服务商列表 handlers（CRUD 委托 ProviderEditor，本组件只管列表 + 开关 + 删除）──

  /** 打开新增表单：清空 editingProvider（新增态）后开 Modal。 */
  const handleAddProvider = () => {
    setEditingProvider(null)
    setFormOpen(true)
  }

  /** 打开编辑表单：记下待编辑 provider 对象后开 Modal。 */
  const handleEditProvider = (p: LlmProvider) => {
    setEditingProvider(p)
    setFormOpen(true)
  }

  /** ProviderEditor 保存成功后回调：刷新列表 + 关闭 Modal。 */
  const handleEditorSaved = () => {
    refreshProviders()
    setFormOpen(false)
    setEditingProvider(null)
  }

  /** ProviderEditor 关闭回调（取消/点 X）：仅关 Modal，不刷新（无变更）。 */
  const handleEditorClose = () => {
    setFormOpen(false)
    setEditingProvider(null)
  }

  /** 删除服务商。 */
  const handleDeleteProvider = async (p: LlmProvider) => {
    try {
      await providerApi.remove(p.id)
      message.success(`已删除「${p.name}」`)
      refreshProviders()
    } catch (e) {
      message.error(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  /** 开关切换：开启 → activate 该服务商（后端会自动关闭其余）；关闭 → 若当前是 active 则不处理
   *  （single-active 下至少需保留一个生效，关掉唯一生效项无意义），否则无操作。
   *  即「开关样式选择启用谁」：开 = 启用这个，关 = 不启用这个（但后端保证至少一个生效）。 */
  const handleToggleActive = async (p: LlmProvider, nextOn: boolean) => {
    if (nextOn) {
      // 开启 → 设为当前生效（后端 single-active 自动停用其余）
      setTogglingId(p.id)
      try {
        await providerApi.activate(p.id)
        refreshProviders()
      } catch (e) {
        message.error(`启用失败：${e instanceof Error ? e.message : String(e)}`)
      } finally {
        setTogglingId(null)
      }
    } else if (p.is_active) {
      // 关闭当前生效项：single-active 下不允许「一个都不生效」，提示并保持开启。
      message.warning('至少需保留一个生效的服务商')
    }
    // 关闭非生效项：本来就关，无操作
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title="设置"
      width={900}
      footer={null}
      destroyOnClose
      // body 给 70vh 让右侧内容可滚；左右布局靠内部 flex 实现（padding 0 让左右贴边）。
      styles={{ body: { padding: 0, height: '70vh', display: 'flex', overflow: 'hidden' } }}
    >
      <div style={{ display: 'flex', height: '100%', width: '100%' }}>
        {/* 左侧导航列：180px 固定宽，浅灰背景，右边框分隔。
         * 不用 antd Menu 是为了完全自控选中态样式（左 3px 品牌蓝条 + 文字加粗），
         * 与 GroupInfoDrawer 自画列表行密度一致。 */}
        <div
          style={{
            width: 180,
            flexShrink: 0,
            background: '#f7f8fa',
            borderRight: '1px solid #f0f0f0',
            overflowY: 'auto',
          }}
        >
          {NAV_ITEMS.map((item) => {
            const active = item.key === activeKey
            return (
              <div
                key={item.key}
                onClick={() => setActiveKey(item.key)}
                style={{
                  padding: '10px 16px',
                  display: 'flex',
                  gap: 8,
                  alignItems: 'center',
                  cursor: 'pointer',
                  fontSize: 14,
                  position: 'relative',
                  // 选中态：白底 + 品牌蓝文字加粗；未选中 hover 走浅灰（CSS in style 无法写 hover，
                  // 故用 onMouseEnter/Leave 切背景；此处用恒定态 + 选中优先，hover 交给浏览器默认）
                  background: active ? '#fff' : undefined,
                  color: active ? BRAND_BLUE : undefined,
                  fontWeight: active ? 600 : 400,
                }}
                // 选中项左侧 3px 品牌蓝实心条：用伪 borderLeft 实现最简（无需 absolute 定位）
                data-active={active}
              >
                {active && (
                  <span
                    style={{
                      position: 'absolute',
                      left: 0,
                      top: 0,
                      bottom: 0,
                      width: 3,
                      background: BRAND_BLUE,
                    }}
                  />
                )}
                {item.icon}
                <span>{item.label}</span>
              </div>
            )
          })}
        </div>

        {/* 右侧内容区：flex:1 白底，overflowY auto 让长内容（MCP 列表/技能市场）可滚。
         *  - McpPage/SkillPage 根容器自带 height:100%+overflowY:auto，外层再 overflow 不会双重滚动
         *   （里层先吃满高度滚动，外层无溢出）；保留外层 overflow auto 是为 memory/model 短内容兜底。 */}
        <div
          style={{
            flex: 1,
            background: '#fff',
            padding: 16,
            overflowY: 'auto',
            minHeight: 0,
          }}
        >
          {activeKey === 'mcp' && <McpPage />}
          {activeKey === 'skills' && <SkillPage />}
          {activeKey === 'memory' && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100%',
              }}
            >
              <Empty description="记忆管理开发中（后端 /api/memory 端点待补）" />
            </div>
          )}
          {activeKey === 'tts' && <TtsSettingsPanel />}
          {activeKey === 'external' && (
            <div style={{ maxWidth: 560 }}>
              <div style={{ marginBottom: 4, fontSize: 16, fontWeight: 600 }}>
                外部系统
              </div>
              <div style={{ fontSize: 13, color: '#999', marginBottom: 20 }}>
                将智能体产生的数据导出到外部系统（文件下载 / Webhook 推送 / 数据库同步等）
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {/* 导出卡片 1：文件下载 */}
                <ExternalExportCard
                  title="导出为文件"
                  desc="将对话历史、任务记录、智能体配置导出为 JSON / CSV 文件，下载到本机"
                  badge="文件下载"
                  onAction={() => message.info('导出文件功能开发中（后端 /api/export 端点待补）')}
                />
                {/* 导出卡片 2：Webhook 推送 */}
                <ExternalExportCard
                  title="Webhook 推送"
                  desc="智能体产出的消息 / 产物实时推送到指定 Webhook 地址，供第三方系统消费"
                  badge="实时推送"
                  onAction={() => message.info('Webhook 推送配置开发中')}
                />
                {/* 导出卡片 3：数据库同步 */}
                <ExternalExportCard
                  title="数据库同步"
                  desc="将智能体数据定期同步到外部数据库（PostgreSQL / MySQL 等），供 BI 与报表系统使用"
                  badge="定时同步"
                  onAction={() => message.info('数据库同步配置开发中')}
                />
              </div>
            </div>
          )}
          {activeKey === 'im' && (
            <div style={{ maxWidth: 560 }}>
              <div style={{ marginBottom: 4, fontSize: 16, fontWeight: 600 }}>
                即时消息
              </div>
              <div style={{ fontSize: 13, color: '#999', marginBottom: 20 }}>
                接收外部即时通讯平台消息，转发给智能体处理；智能体回复可回推到对应平台
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <ImChannelCard
                  name="微信"
                  desc="接入企业微信 / 微信，接收好友或群消息转发给智能体，回复回推到会话"
                  status="未接入"
                  onAction={() => message.info('微信接入配置开发中')}
                />
                <ImChannelCard
                  name="钉钉"
                  desc="接入钉钉机器人，接收钉钉群 @ 消息转发给智能体，回复推送到钉钉群"
                  status="未接入"
                  onAction={() => message.info('钉钉接入配置开发中')}
                />
                <ImChannelCard
                  name="飞书"
                  desc="接入飞书机器人，接收飞书消息转发给智能体，回复推送到飞书会话"
                  status="未接入"
                  onAction={() => message.info('飞书接入配置开发中')}
                />
              </div>
            </div>
          )}
          {activeKey === 'user' && (
            <div style={{ maxWidth: 480 }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 16,
                  padding: '20px 0',
                  borderBottom: '1px solid #f0f0f0',
                }}
              >
                <Avatar size={64} icon={<UserOutlined />} />
                <div>
                  <div style={{ fontSize: 18, fontWeight: 600 }}>游客</div>
                  <div style={{ fontSize: 13, color: '#999', marginTop: 4 }}>
                    未登录 · 登录功能开发中
                  </div>
                </div>
              </div>
              <p style={{ fontSize: 13, color: '#666', marginTop: 20, lineHeight: 1.8 }}>
                后续将支持账号登录，登录后可同步智能体配置、对话历史与技能到云端。
                当前为本地单机模式，所有数据保存在本机。
              </p>
            </div>
          )}
          {activeKey === 'model' && (
            <>
              {/* 顶部：标题 + 新增按钮 */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  marginBottom: 12,
                }}
              >
                <span style={{ fontSize: 12, color: '#999' }}>
                  模型服务商（开关选择启用谁，同时仅一个生效）
                </span>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  size="small"
                  onClick={handleAddProvider}
                >
                  新增服务商
                </Button>
              </div>

              {/* 服务商列表（开关样式：每行一个 Switch 控制是否启用） */}
              {providersLoading ? (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    padding: 40,
                  }}
                >
                  <Spin />
                </div>
              ) : providers.length === 0 ? (
                <Empty description="尚未配置服务商，点击右上角新增" />
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {providers.map((p) => (
                    <div
                      key={p.id}
                      style={{
                        border: `1px solid ${p.is_active ? '#d6e4ff' : '#f0f0f0'}`,
                        borderRadius: 6,
                        padding: '10px 12px',
                        background: p.is_active ? '#f0f7ff' : '#fafafa',
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          flexWrap: 'wrap',
                          gap: 8,
                        }}
                      >
                        {/* 左：开关 + 名称 + 标签（开关即「启用谁」入口） */}
                        <div
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            flexWrap: 'wrap',
                          }}
                        >
                          <Switch
                            loading={togglingId === p.id}
                            checked={p.is_active}
                            onChange={(on) => handleToggleActive(p, on)}
                          />
                          <span style={{ fontWeight: 600, fontSize: 14 }}>{p.name}</span>
                          <Tag>{p.provider}</Tag>
                          <Tag color="blue">{p.model || '—'}</Tag>
                          {p.is_active && <Tag color="green">生效中</Tag>}
                          {p.has_key ? (
                            <Tag color="green">已配置</Tag>
                          ) : (
                            <Tag color="red">未配置</Tag>
                          )}
                        </div>
                        {/* 右：编辑/删除（不再单独「设为当前」按钮——开关即此功能） */}
                        <div style={{ display: 'flex', gap: 4 }}>
                          <Button
                            size="small"
                            onClick={() => handleEditProvider(p)}
                          >
                            编辑
                          </Button>
                          <Popconfirm
                            title="确认删除该服务商？"
                            onConfirm={() => handleDeleteProvider(p)}
                            okText="删除"
                            cancelText="取消"
                          >
                            <Button size="small" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        </div>
                      </div>
                      {/* 第二行：base_url（截断显示） */}
                      <div
                        style={{
                          fontSize: 12,
                          color: '#999',
                          marginTop: 4,
                          wordBreak: 'break-all',
                        }}
                      >
                        {p.base_url || '—'}
                        {p.api_key ? ` · key: ${p.api_key}` : ' · 未配置 key'}
                        {' · temp '}
                        {p.temperature}
                        {' · max '}
                        {p.max_tokens}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* 新增/编辑服务商：委托 ProviderEditor（多模型目录 + 连接级配置）。
        *  editingProvider=null 走新增态，传 undefined；非 null 走编辑态，传 provider 对象。
        *  onSaved=刷新列表+关 Modal；onClose=仅关 Modal（取消）。 */}
      <ProviderEditor
        open={formOpen}
        provider={editingProvider ?? undefined}
        onSaved={handleEditorSaved}
        onClose={handleEditorClose}
      />
    </Modal>
  )
}

/** 外部系统 — 导出方式卡片：标题 + 描述 + 角标 + 配置按钮。 */
function ExternalExportCard({
  title,
  desc,
  badge,
  onAction,
}: {
  title: string
  desc: string
  badge: string
  onAction: () => void
}) {
  return (
    <div
      style={{
        border: '1px solid var(--border-card)',
        borderRadius: 8,
        padding: '14px 16px',
        background: 'var(--surface-raised)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
          <span style={{ fontSize: 14, fontWeight: 600 }}>{title}</span>
          <Tag color="blue" style={{ margin: 0 }}>{badge}</Tag>
        </div>
        <div style={{ fontSize: 12, color: '#999', lineHeight: 1.5 }}>{desc}</div>
      </div>
      <Button size="small" onClick={onAction}>配置</Button>
    </div>
  )
}

/** 即时消息 — 接入渠道卡片：平台名 + 描述 + 接入状态 + 接入按钮。 */
function ImChannelCard({
  name,
  desc,
  status,
  onAction,
}: {
  name: string
  desc: string
  status: string
  onAction: () => void
}) {
  return (
    <div
      style={{
        border: '1px solid var(--border-card)',
        borderRadius: 8,
        padding: '14px 16px',
        background: 'var(--surface-raised)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
          <span style={{ fontSize: 14, fontWeight: 600 }}>{name}</span>
          <Tag style={{ margin: 0 }}>{status}</Tag>
        </div>
        <div style={{ fontSize: 12, color: '#999', lineHeight: 1.5 }}>{desc}</div>
      </div>
      <Button size="small" onClick={onAction}>接入</Button>
    </div>
  )
}

/** 语音朗读设置面板：纯前端偏好（绑 useSettings + useTts）。
 *  Web Speech API 不依赖后端，音色列表来自浏览器/Electron 语音引擎（中文优先）。
 *  不支持时整块灰禁 + 提示，不报错。 */
function TtsSettingsPanel() {
  const { tts, updateTts } = useSettings()
  const { supported, voices, speak, stop } = useTts()

  if (!supported) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100%',
        }}
      >
        <Empty description="当前环境不支持语音朗读（无语音引擎）" />
      </div>
    )
  }

  return (
    <div style={{ maxWidth: 560 }}>
      <div style={{ marginBottom: 4, fontSize: 16, fontWeight: 600 }}>语音朗读</div>
      <div style={{ fontSize: 13, color: '#999', marginBottom: 20 }}>
        智能体回复可朗读为语音。开启「自动朗读」后，每条新回复定稿即读；也可在对话气泡上 hover 点喇叭按需朗读。
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        {/* 总开关 */}
        <SettingRow
          title="启用语音朗读"
          desc="总开关。关闭后所有语音入口（自动朗读、气泡喇叭）一并禁用"
        >
          <Switch checked={tts.enabled} onChange={(v) => updateTts({ enabled: v })} />
        </SettingRow>

        {/* 自动朗读 */}
        <SettingRow
          title="自动朗读新回复"
          desc="智能体回复定稿后自动朗读（用户上滑读历史时不打断；切群/重连不误读历史）"
        >
          <Switch
            checked={tts.autoPlay}
            disabled={!tts.enabled}
            onChange={(v) => updateTts({ autoPlay: v })}
          />
        </SettingRow>

        {/* 音色 */}
        <SettingRow title="音色" desc="系统语音引擎提供的音色（中文优先排列）">
          <Select
            style={{ width: 280 }}
            value={tts.voiceURI ?? undefined}
            disabled={!tts.enabled}
            placeholder="系统默认"
            allowClear
            onChange={(v) => updateTts({ voiceURI: v ?? null })}
            options={voices.map((v) => ({
              value: v.voiceURI,
              label: `${v.name}（${v.lang}）`,
            }))}
            notFoundContent={voices.length === 0 ? <Spin size="small" /> : '无可用音色'}
          />
        </SettingRow>

        {/* 语速 */}
        <SettingRow title="语速" desc={`0.5 ~ 2 倍，当前 ${tts.rate.toFixed(1)} 倍`}>
          <Slider
            style={{ width: 280 }}
            min={0.5}
            max={2}
            step={0.1}
            value={tts.rate}
            disabled={!tts.enabled}
            onChange={(v) => updateTts({ rate: v })}
          />
        </SettingRow>

        {/* 音量 */}
        <SettingRow title="音量" desc={`0 ~ 1，当前 ${(tts.volume * 100).toFixed(0)}%`}>
          <Slider
            style={{ width: 280 }}
            min={0}
            max={1}
            step={0.05}
            value={tts.volume}
            disabled={!tts.enabled}
            onChange={(v) => updateTts({ volume: v })}
          />
        </SettingRow>

        {/* 音调 */}
        <SettingRow title="音调" desc={`0 ~ 2，当前 ${tts.pitch.toFixed(1)}`}>
          <Slider
            style={{ width: 280 }}
            min={0}
            max={2}
            step={0.1}
            value={tts.pitch}
            disabled={!tts.enabled}
            onChange={(v) => updateTts({ pitch: v })}
          />
        </SettingRow>

        {/* 试听 */}
        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Tooltip title={tts.enabled ? undefined : '请先开启总开关'}>
            <Space>
              <Button onClick={() => stop()} disabled={!tts.enabled}>停止</Button>
              <Button
                type="primary"
                disabled={!tts.enabled}
                onClick={() => speak('这是一条语音朗读测试，可据此调整音色、语速、音量与音调。')}
              >
                试听
              </Button>
            </Space>
          </Tooltip>
        </div>
      </div>
    </div>
  )
}

/** 设置行：左侧标题 + 描述，右侧控件。与现有设置面板密度一致。 */
function SettingRow({
  title,
  desc,
  children,
}: {
  title: string
  desc: string
  children: React.ReactNode
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 16,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500 }}>{title}</div>
        <div style={{ fontSize: 12, color: '#999', marginTop: 2, lineHeight: 1.5 }}>{desc}</div>
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  )
}
