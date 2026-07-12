/**
 * ProviderEditor：LLM 服务商新增/编辑弹窗（多模型目录升级 · PE-01~06）。
 *
 * 与旧 SettingsModal 内联的单模型表单不同——本组件承载完整多模型目录：
 *  - 预设选择器（PE-02）：选预设灌入连接配置 + 预置 models，选「自定义」从零开始；
 *  - 连接配置 Form（PE-03）：name/provider/base_url/api_key + 8 连接级字段；
 *  - 模型目录 Table（PE-04）：每行一个 LlmModel，可增删 + 能力开关 + 单 default；
 *  - 测试连通/拉取模型（PE-05）：仅编辑态（新增态无 id）；
 *  - 保存（PE-06）：构造 payload（恰好一个 is_default）→ create/update → onSaved。
 *
 * PE-01 当前为骨架：props + formState（models + 8 连接级字段）+ Modal 容器，
 * 组件可独立编译；SettingsModal 尚未接入（PE-07 改造）。
 */
import { useState, useEffect } from 'react'
import { Modal, Form, Input, Divider, Select, message } from 'antd'
import { providerApi } from '../services/api'
import type { LlmProvider, LlmModel, ProviderPreset } from '../services/api'

/** 服务商表单内部 state（PE-01：含 models 目录 + 8 连接级字段）。 */
interface ProviderFormState {
  name: string
  provider: string
  base_url: string
  /** 编辑态留空 = 不修改（与旧表单一语义；PE-03 placeholder 提示「留空则不修改」）。 */
  api_key: string
  /** 多模型目录（provider 拥有 N 个模型，恰好 1 个 is_default）。 */
  models: LlmModel[]
  // ── 8 连接级字段（作用于端点，所有模型共享）──
  /** API 版本（Anthropic 等需 x-api-version 的端点用）；空串 = 未配置。 */
  api_version: string
  /** OpenAI 组织 id（部分端点用 org 头路由计费）；空串 = 未配置。 */
  organization: string
  /** 自定义请求头（合并到 Authorization 之外）；null = 不附加。 */
  extra_headers: Record<string, string> | null
  /** 单请求超时秒数（默认 120）。 */
  request_timeout: number
  /** 失败重试次数（默认 2）。 */
  max_retries: number
  /** HTTP 代理地址；空串 = 直连。 */
  proxy: string
  /** 采样温度（默认 0.0）。 */
  temperature: number
  /** 单次响应最大 token（默认 4096）。 */
  max_tokens: number
}

/** 新增态默认 formState（自定义 provider 从零开始）。 */
const EMPTY_FORM: ProviderFormState = {
  name: '',
  provider: 'openai',
  base_url: '',
  api_key: '',
  models: [],
  api_version: '',
  organization: '',
  extra_headers: null,
  request_timeout: 120,
  max_retries: 2,
  proxy: '',
  temperature: 0.0,
  max_tokens: 4096,
}

/**
 * 编辑态：把 LlmProvider 灌入 formState。
 *
 * api_key 留空（后端返回的 api_key 是脱敏 mask，不可回填；PE-03 用 placeholder
 * 提示「留空则不修改」）。models 浅拷贝每个条目，避免前端编辑直接改后端返回对象。
 */
function providerToFormState(p: LlmProvider): ProviderFormState {
  return {
    name: p.name,
    provider: p.provider,
    base_url: p.base_url,
    api_key: '',
    models: p.models ? p.models.map((m) => ({ ...m })) : [],
    api_version: p.api_version,
    organization: p.organization,
    extra_headers: p.extra_headers,
    request_timeout: p.request_timeout,
    max_retries: p.max_retries,
    proxy: p.proxy,
    temperature: p.temperature,
    max_tokens: p.max_tokens,
  }
}

interface ProviderEditorProps {
  open: boolean
  /** 编辑态传入；新增态 undefined（title 据此切换「新增/编辑服务商」）。 */
  provider?: LlmProvider
  /** 保存成功后回调（父组件刷新列表）。 */
  onSaved?: () => void
  /** 关闭弹窗回调。 */
  onClose?: () => void
}

export default function ProviderEditor({
  open,
  provider,
  onSaved,
  onClose,
}: ProviderEditorProps) {
  const isEdit = !!provider
  // destroyOnClose 下 Modal 关闭即卸载，再次打开重新跑 useState 初始化器——
  // 故 provider 变化（新增↔编辑切换）会重新灌入 formState，无需 useEffect 同步。
  const [formState, setFormState] = useState<ProviderFormState>(() =>
    provider ? providerToFormState(provider) : EMPTY_FORM,
  )

  // ── PE-02 预设选择器 ──
  // 预设是「编辑器加载的模板」（base_url + 连接配置 + 预置 models），用户选预设后一键灌入
  // formState，省去手填。仅新增态展示（编辑态已有配置，套预设会覆盖用户既有定制，有风险）。
  const [presets, setPresets] = useState<ProviderPreset[]>([])
  const [presetsLoading, setPresetsLoading] = useState(false)
  // 当前选中的预设 slug；'__custom__' = 自定义（不灌入）；undefined = 未选（初始态）。
  const [selectedPreset, setSelectedPreset] = useState<string | undefined>(
    undefined,
  )

  // 新增态打开时拉一次 catalog（后端静态目录恒可用，无网络/DB 依赖）。
  // 依赖 [open, isEdit]：仅新增态且打开时拉；编辑态/关闭时不拉。
  useEffect(() => {
    if (!open || isEdit) return
    if (presets.length > 0) return // 已拉过不重复
    setPresetsLoading(true)
    providerApi
      .catalog()
      .then((list) => setPresets(list))
      .catch(() => message.error('获取预设目录失败'))
      .finally(() => setPresetsLoading(false))
  }, [open, isEdit, presets.length])

  /**
   * 应用预设：把 ProviderPreset 的连接配置 + 预置 models 灌入 formState。
   *
   * 不灌入 name（用户自填，预设只承载连接/模型模板）与 api_key（预设无密钥，用户填）。
   * models 每条浅拷贝，避免与 presets 缓存中的对象共享引用（后续 PE-04 Table 编辑会 mutate）。
   */
  const applyPreset = (preset: ProviderPreset) => {
    setFormState((prev) => ({
      ...prev,
      provider: preset.provider,
      base_url: preset.base_url,
      api_version: preset.api_version,
      organization: preset.organization,
      extra_headers: preset.extra_headers,
      request_timeout: preset.request_timeout,
      max_retries: preset.max_retries,
      proxy: preset.proxy,
      temperature: preset.temperature,
      max_tokens: preset.max_tokens,
      models: preset.models.map((m) => ({ ...m })),
    }))
  }

  /** 预设 Select onChange：'__custom__' 或 undefined = 自定义（不灌入，保留当前 formState）。 */
  const handlePresetChange = (value: string) => {
    setSelectedPreset(value)
    if (value === '__custom__') return // 自定义：不灌入
    const preset = presets.find((p) => p.slug === value)
    if (preset) applyPreset(preset)
  }

  // 当前选中预设的 note（显示在选择器下方）；自定义/未选时无 note。
  const activePreset = presets.find((p) => p.slug === selectedPreset)
  const presetNote = activePreset?.note ?? ''

  // PE-06 将实现完整保存：name 必填校验 → 构造 LlmProviderPayload（api_key 仅非空传；
  //  models 整体传并保证恰好一个 is_default，无 default 时把首个置 true）→
  //  编辑态 update / 新增态 create → message.success + onSaved + 关闭。
  // 骨架阶段不落库，仅占位触发回调关闭（SettingsModal 未接入，不会被触发）。
  const handleSave = async () => {
    onSaved?.()
    onClose?.()
  }

  return (
    <Modal
      open={open}
      title={isEdit ? '编辑服务商' : '新增服务商'}
      width={640}
      destroyOnClose
      onCancel={onClose}
      onOk={handleSave}
      okText="保存"
      cancelText="取消"
    >
      <Form layout="vertical" style={{ marginTop: 8 }}>
        {/* PE-02 预设选择器：仅新增态。选预设一键灌入连接配置 + 预置 models 目录；
         *  选「自定义」从零开始不灌入。编辑态不展示（套预设会覆盖用户既有定制）。 */}
        {!isEdit && (
          <Form.Item label="预设">
            <Select
              value={selectedPreset}
              onChange={handlePresetChange}
              loading={presetsLoading}
              placeholder="选择预设快速填充，或选「自定义」从零开始"
              allowClear
              options={[
                // 「自定义」置顶：显式表达「不套预设」语义（与 allowClear 清除等价但更直白）。
                { label: '自定义（手动填写）', value: '__custom__' },
                ...presets.map((p) => ({ label: p.name, value: p.slug })),
              ]}
            />
            {presetNote && (
              <div style={{ fontSize: 12, color: '#999', marginTop: 6 }}>
                {presetNote}
              </div>
            )}
          </Form.Item>
        )}
        <Form.Item label="名称" required>
          <Input
            value={formState.name}
            onChange={(e) =>
              setFormState({ ...formState, name: e.target.value })
            }
            placeholder="如 OpenAI 官方、DeepSeek"
          />
        </Form.Item>
        {/* PE-03 连接配置 · PE-04 模型目录 Table · PE-05 测试连通+拉取模型 */}
        <Divider plain style={{ fontSize: 12, color: '#999' }}>
          连接配置 · 模型目录 · 测试连通（PE-03~05 待实现）
        </Divider>
      </Form>
    </Modal>
  )
}
