/**
 * SettingsModal：设置弹窗（左右布局，非顶部 Tabs）。
 *
 * 为什么不用 antd Tabs（顶部 tabBar）：
 *  - 产品要求左侧 180px 导航列 + 右侧 flex:1 内容区，antd Tabs 默认顶部 tabBar 与此布局冲突；
 *  - 自己画左侧可点击导航行 + 右侧按 activeKey 条件渲染更直白，密度与 GroupInfoDrawer/AgentDetailPanel 一致。
 *
 * 四个设置项：MCP / 技能 / 记忆 / 模型服务商。
 *  - MCP、技能直接复用全屏路由页（McpPage/SkillPage），它们自带数据拉取与 height:100%+overflowY:auto
 *    根容器，放进右侧时外层已 overflowY auto，让其自然铺；
 *  - 记忆是占位（后端 /api/memory 端点待补）；
 *  - 模型服务商：多 provider 管理（providerApi CRUD + activate），新增/编辑委托 ProviderEditor 组件
 *    （多模型目录 + 连接级配置；PE-07 替换原内联单模型 Form）。本组件只管列表展示 + 开关启用 + 删除。
 */
import { useEffect, useState } from 'react'
import { Modal, Button, Spin, Empty, Tag, message, Switch, Popconfirm } from 'antd'
import {
  ApiOutlined,
  AppstoreOutlined,
  DatabaseOutlined,
  CloudServerOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import McpPage from '../pages/McpPage'
import SkillPage from '../pages/SkillPage'
import ProviderEditor from './ProviderEditor'
import { providerApi, type LlmProvider } from '../services/api'

interface SettingsModalProps {
  open: boolean
  onClose: () => void
}

/** 左侧导航项定义：key 唯一标识，用于 activeKey 条件渲染右侧内容。 */
interface NavItem {
  key: 'mcp' | 'skills' | 'memory' | 'model'
  label: string
  icon: React.ReactNode
}

const NAV_ITEMS: NavItem[] = [
  { key: 'mcp', label: 'MCP', icon: <ApiOutlined /> },
  { key: 'skills', label: '技能', icon: <AppstoreOutlined /> },
  { key: 'memory', label: '记忆', icon: <DatabaseOutlined /> },
  { key: 'model', label: '模型服务商', icon: <CloudServerOutlined /> },
]

/** 品牌蓝：仅用于选中项左条 + 选中文字强调，主体保持浅灰白。 */
const BRAND_BLUE = '#0A5ACF'

export default function SettingsModal({ open, onClose }: SettingsModalProps) {
  // 选中项：默认 MCP。destroyOnClose 卸载后再次打开会重置为 'mcp'（符合「每次打开都从首项开始」预期）。
  const [activeKey, setActiveKey] = useState<NavItem['key']>('mcp')

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
