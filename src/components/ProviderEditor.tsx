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
 * SettingsModal 尚未接入（PE-07 改造）。
 */
import { useState, useEffect } from 'react'
import {
  Modal,
  Form,
  Input,
  Divider,
  Select,
  AutoComplete,
  InputNumber,
  Row,
  Col,
  Table,
  Button,
  Radio,
  Switch,
  message,
} from 'antd'
import type { TableProps } from 'antd'
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons'
import { providerApi } from '../services/api'
import type {
  LlmProvider,
  LlmModel,
  LlmProviderPayload,
  ProviderPreset,
} from '../services/api'

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
 * Provider 类型 AutoComplete 候选项（用户可手输不在列表内的值）。
 * 与旧 SettingsModal PROVIDER_OPTIONS 同源，结构对齐 AutoComplete options（{value}）。
 */
const PROVIDER_OPTIONS = [
  { value: 'openai' },
  { value: 'deepseek' },
  { value: 'anthropic' },
  { value: 'kimi' },
  { value: 'glm' },
  { value: 'qwen' },
  { value: 'ollama' },
]

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

/**
 * 新增模型空行默认值（PE-04「+ 添加模型」push 进 models）。
 * model_id/display_name 留空让用户填；能力开关默认开（多数现代模型都支持）；
 * is_default=false（单 default 不变量——handleAddModel 会处理「空目录时自动置首个为 default」）。
 */
function emptyModel(): LlmModel {
  return {
    model_id: '',
    display_name: '',
    context_window: 0,
    supports_function_calling: true,
    supports_vision: false,
    supports_streaming: true,
    is_default: false,
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
  // ProviderEditor 常驻挂载（SettingsModal 始终渲染本组件，open 只控 Modal 显隐，不卸载组件），
  // 故 useState 初始化器只在首次挂载跑一次（那时 provider 恒 undefined → EMPTY_FORM）。
  // 不加此 effect，编辑态 provider prop 变化不会重灌 formState → 表单全空白（信息不反显）。
  // 打开时按 provider 重置：编辑态灌入、新增态清空；关闭态不重置（保留输入供改后重试仅对当前
  // 仍打开有效，关闭后 formOpen=false，下次打开按新 provider 重置——取消即丢弃未保存编辑）。
  const [formState, setFormState] = useState<ProviderFormState>(() =>
    provider ? providerToFormState(provider) : EMPTY_FORM,
  )
  useEffect(() => {
    if (!open) return
    setFormState(provider ? providerToFormState(provider) : EMPTY_FORM)
  }, [open, provider])

  // ── PE-03 extra_headers JSON 文本镜像 ──
  // formState.extra_headers 是结构化 Record|null（保存时用），但表单里是单行文本框
  // （用户手输 JSON）。extraHeadersText 是它的字符串镜像：加载时 stringify，输入时
  // 仅更新文本（容错：非法 JSON 不抛错、不回灌 formState，解析成功才同步）。
  // 这样用户可自由编辑半成品 JSON（如缺右括号中途态），不会因每次按键 parse 失败而弹错。
  const [extraHeadersText, setExtraHeadersText] = useState<string>(() =>
    formState.extra_headers ? JSON.stringify(formState.extra_headers) : '',
  )
  /** extra_headers 解析态：''=空(未输入) / 'ok'=合法JSON已同步 / 'error'=非法JSON。 */
  const [extraHeadersError, setExtraHeadersError] = useState<
    '' | 'ok' | 'error'
  >('')
  // 预设/编辑态灌入 extra_headers 后同步文本镜像（applyPreset 用 setFormState 改 extra_headers，
  // 文本镜像需跟上——监听 formState.extra_headers 引用变化时 stringify）。
  useEffect(() => {
    setExtraHeadersText(
      formState.extra_headers ? JSON.stringify(formState.extra_headers) : '',
    )
    setExtraHeadersError('')
  }, [formState.extra_headers])

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

  /**
   * PE-03 extra_headers 文本变更：仅更新文本镜像 + 实时尝试 parse。
   *
   * 容错策略——空串 = null（清除自定义头）；合法 JSON = 解析为 Record 写回 formState；
   * 非法 JSON = 标记 error 但保留文本（不回灌、不抛错），用户可继续编辑到合法为止。
   * 解析结果非对象（如 JSON.parse('"str"') 得 string、'123' 得 number）也算 error
   * ——extra_headers 必须是 string→string 的对象（Record<string,string>）。
   */
  const handleExtraHeadersChange = (text: string) => {
    setExtraHeadersText(text)
    const trimmed = text.trim()
    if (trimmed === '') {
      setFormState((prev) => ({ ...prev, extra_headers: null }))
      setExtraHeadersError('')
      return
    }
    try {
      const parsed: unknown = JSON.parse(trimmed)
      // 必须是 plain object（Record<string,string>）；数组/null/原始值均不接受。
      if (
        typeof parsed !== 'object' ||
        parsed === null ||
        Array.isArray(parsed)
      ) {
        setExtraHeadersError('error')
        return
      }
      setFormState((prev) => ({
        ...prev,
        extra_headers: parsed as Record<string, string>,
      }))
      setExtraHeadersError('ok')
    } catch {
      setExtraHeadersError('error')
    }
  }

  // ── PE-06 保存 ──
  const [saving, setSaving] = useState(false)

  /**
   * 保存：name 必填 trim → 构造 LlmProviderPayload → create/update → onSaved + 关闭。
   *
   * payload 字段取舍：
   *  - name：必填 trim（空串直接 warning 拦截，不进网络层）；
   *  - api_key：仅 formState.api_key 非空时传（编辑态留空 = 不修改，与 placeholder
   *    「留空则不修改」一语义；新增态空串本就无密钥可传）；
   *  - models：整体传（即使空数组也传——显式 [] = 清空目录，undefined = 不动目录，
   *    此处用户在编辑器内主动管理目录，传 [] 才能落库「删光」语义）。
   *    单 default 不变量：保存前规整——若已有 default 则仅保留首个（防多选），
   *    若无 default 且目录非空则把首个置 true（防「无 default」态）。
   *  - 其余连接级字段全量传（Form 项即真源，与旧 SettingsModal 全量提交一语义）。
   *
   * extra_headers 处于 error 态时拦截保存（非法 JSON 不应落库）。
   * 失败 message.error（不关闭 Modal，保留用户输入便于改后重试）。
   */
  const handleSave = async () => {
    const name = formState.name.trim()
    if (!name) {
      message.warning('服务商名称不能为空')
      return
    }
    if (extraHeadersError === 'error') {
      message.warning('Extra Headers JSON 格式错误，请先修正')
      return
    }
    // 单 default 规整：保留首个 default（防多选），无 default 且非空则置首个。
    const defaultIdx = formState.models.findIndex((m) => m.is_default)
    const models: LlmModel[] = formState.models.map((m, i) => ({
      ...m,
      is_default:
        defaultIdx >= 0 ? i === defaultIdx : i === 0,
    }))

    const payload: LlmProviderPayload = {
      name,
      provider: formState.provider,
      base_url: formState.base_url,
      temperature: formState.temperature,
      max_tokens: formState.max_tokens,
      models,
      api_version: formState.api_version,
      organization: formState.organization,
      extra_headers: formState.extra_headers,
      request_timeout: formState.request_timeout,
      max_retries: formState.max_retries,
      proxy: formState.proxy,
    }
    if (formState.api_key) {
      payload.api_key = formState.api_key
    }

    setSaving(true)
    try {
      if (isEdit && provider) {
        await providerApi.update(provider.id, payload)
        message.success('服务商已更新')
      } else {
        await providerApi.create(payload)
        message.success('服务商已新增')
      }
      onSaved?.()
      onClose?.()
    } catch (e) {
      message.error(`保存服务商失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }

  // ── PE-04 模型目录 Table handlers ──
  /**
   * 改某行某字段（immutable 更新：map 出新数组，命中 index 替换该行）。
   * 用 index 作为 row key 而非 model_id（model_id 可空/重复，见下方 columns 说明）。
   */
  const updateModel = (index: number, patch: Partial<LlmModel>) => {
    setFormState((prev) => ({
      ...prev,
      models: prev.models.map((m, i) => (i === index ? { ...m, ...patch } : m)),
    }))
  }

  /** 「+ 添加模型」：push 空行。若当前无任何 default，自动把新行置为 default（单 default 不变量）。 */
  const handleAddModel = () => {
    setFormState((prev) => {
      const noDefault = !prev.models.some((m) => m.is_default)
      return {
        ...prev,
        models: [...prev.models, { ...emptyModel(), is_default: noDefault }],
      }
    })
  }

  /** 删除某行。若删的是 default 且还有剩余行，把首个置为新 default（保单 default 不变量）。 */
  const handleDeleteModel = (index: number) => {
    setFormState((prev) => {
      const target = prev.models[index]
      const next = prev.models.filter((_, i) => i !== index)
      // 删的是 default 且仍有剩余 → 把首个补为 default，避免「无 default」态。
      if (target?.is_default && next.length > 0) {
        next[0] = { ...next[0], is_default: true }
      }
      return { ...prev, models: next }
    })
  }

  /** 设某行为 default（单选语义：先全部置 false，再把目标置 true）。 */
  const handleSetDefault = (index: number) => {
    setFormState((prev) => ({
      ...prev,
      models: prev.models.map((m, i) => ({ ...m, is_default: i === index })),
    }))
  }

  // ── PE-05 测试连通 / 拉取模型 ──
  // 仅编辑态可用（新增态 provider 未落库无 id，后端 test/fetchModels 路由是 /providers/{id}/...）。
  // 两个按钮共享「探测类」语义：失败是正常结果（不 throw），返回 {ok, ...} 结构化结果。
  const [testing, setTesting] = useState(false)
  /** 测试连通结果：null=未测 / {ok,latency_ms,error} 最近一次结果。 */
  const [testResult, setTestResult] = useState<{
    ok: boolean
    latency_ms: number
    error: string
  } | null>(null)

  const [fetchingModels, setFetchingModels] = useState(false)
  /** 拉取模型结果：null=未拉 / {ok,error}（models 成功时直接覆盖 formState，不单独存）。 */
  const [fetchModelsResult, setFetchModelsResult] = useState<{
    ok: boolean
    error: string
  } | null>(null)

  /** 测试连通：调 providerApi.test(id)，显示 ok/latency_ms/error。仅编辑态。 */
  const handleTestConnection = async () => {
    if (!provider?.id || testing) return
    setTesting(true)
    setTestResult(null)
    try {
      const r = await providerApi.test(provider.id)
      setTestResult({ ok: r.ok, latency_ms: r.latency_ms, error: r.error })
    } catch (e) {
      // 网络层失败（后端不可达）——http() 已抛 Error，这里兜底成结构化结果。
      const msg = e instanceof Error ? e.message : String(e)
      setTestResult({ ok: false, latency_ms: 0, error: msg })
    } finally {
      setTesting(false)
    }
  }

  /**
   * 拉取模型：调 providerApi.fetchModels(id)，成功后用返回的 models 覆盖 formState.models。
   *
   * 覆盖语义对齐任务约定「拉取成功后用返回的 models 覆盖」——用户当前编辑的目录被丢弃，
   * 故拉取前不二次确认（按钮文案 + 结果展示已暗示覆盖；若需保护可在 PE-08 验证时加 Popconfirm，
   * 当前保持简单）。fetchModels 返回的 models 每条浅拷贝切断与响应对象的引用。
   * 后端 fetchModels 已保证首个 is_default=true（见 api.ts JSDoc），覆盖后单 default 不变量天然成立。
   */
  const handleFetchModels = async () => {
    if (!provider?.id || fetchingModels) return
    setFetchingModels(true)
    setFetchModelsResult(null)
    try {
      const r = await providerApi.fetchModels(provider.id)
      if (r.ok && r.models.length > 0) {
        // 覆盖 formState.models（浅拷贝每条）。保单 default：后端返回首个已 is_default；
        // 防御性兜底——若后端全 false 则置首个为 default。
        const models = r.models.map((m) => ({ ...m }))
        if (!models.some((m) => m.is_default)) {
          models[0] = { ...models[0], is_default: true }
        }
        setFormState((prev) => ({ ...prev, models }))
        setFetchModelsResult({ ok: true, error: '' })
        message.success(`已拉取 ${models.length} 个模型`)
      } else if (r.ok && r.models.length === 0) {
        // ok=true 但空目录：上游 /models 返空，提示但不清空当前目录（避免误删用户已填）。
        setFetchModelsResult({ ok: false, error: '上游返回空模型目录' })
        message.warning('上游返回空模型目录，未覆盖')
      } else {
        setFetchModelsResult({ ok: false, error: r.error || '拉取失败' })
        message.error(`拉取模型失败：${r.error || '未知错误'}`)
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setFetchModelsResult({ ok: false, error: msg })
      message.error(`拉取模型失败：${msg}`)
    } finally {
      setFetchingModels(false)
    }
  }


  // PE-04 模型目录 Table 列定义。rowKey 用 index（见下方 Table props 说明）。
  const modelColumns: TableProps<LlmModel>['columns'] = [
    {
      title: 'Model ID',
      dataIndex: 'model_id',
      width: 140,
      render: (_v, _r, index) => (
        <Input
          value={_r.model_id}
          onChange={(e) => updateModel(index, { model_id: e.target.value })}
          placeholder="如 deepseek-chat"
          size="small"
        />
      ),
    },
    {
      title: '显示名',
      dataIndex: 'display_name',
      width: 120,
      render: (_v, _r, index) => (
        <Input
          value={_r.display_name}
          onChange={(e) =>
            updateModel(index, { display_name: e.target.value })
          }
          placeholder="留空则用 Model ID"
          size="small"
        />
      ),
    },
    {
      title: '上下文窗口',
      dataIndex: 'context_window',
      width: 110,
      render: (_v, _r, index) => (
        <InputNumber
          value={_r.context_window}
          onChange={(n) => updateModel(index, { context_window: n ?? 0 })}
          min={0}
          step={1024}
          size="small"
          style={{ width: '100%' }}
          placeholder="0"
        />
      ),
    },
    {
      title: 'FC',
      dataIndex: 'supports_function_calling',
      width: 50,
      align: 'center' as const,
      render: (_v, _r, index) => (
        <Switch
          size="small"
          checked={_r.supports_function_calling}
          onChange={(on) =>
            updateModel(index, { supports_function_calling: on })
          }
        />
      ),
    },
    {
      title: '视觉',
      dataIndex: 'supports_vision',
      width: 50,
      align: 'center' as const,
      render: (_v, _r, index) => (
        <Switch
          size="small"
          checked={_r.supports_vision}
          onChange={(on) => updateModel(index, { supports_vision: on })}
        />
      ),
    },
    {
      title: '流式',
      dataIndex: 'supports_streaming',
      width: 50,
      align: 'center' as const,
      render: (_v, _r, index) => (
        <Switch
          size="small"
          checked={_r.supports_streaming}
          onChange={(on) => updateModel(index, { supports_streaming: on })}
        />
      ),
    },
    {
      title: '默认',
      dataIndex: 'is_default',
      width: 56,
      align: 'center' as const,
      render: (_v, _r, index) => (
        <Radio
          checked={_r.is_default}
          onChange={() => handleSetDefault(index)}
        />
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 64,
      render: (_v, _r, index) => (
        <Button
          type="link"
          danger
          size="small"
          icon={<DeleteOutlined />}
          onClick={() => handleDeleteModel(index)}
        />
      ),
    },
  ]

  return (
    <Modal
      open={open}
      title={isEdit ? '编辑服务商' : '新增服务商'}
      width={640}
      destroyOnClose
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving}
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
        {/* ── PE-03 连接配置 ── */}
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item label="Provider 类型">
              <AutoComplete
                value={formState.provider}
                onChange={(v) =>
                  setFormState({ ...formState, provider: v })
                }
                options={PROVIDER_OPTIONS}
                placeholder="如 openai / deepseek"
                filterOption={(input, option) =>
                  (option?.value ?? '')
                    .toLowerCase()
                    .includes(input.toLowerCase())
                }
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label="Base URL">
              <Input
                value={formState.base_url}
                onChange={(e) =>
                  setFormState({ ...formState, base_url: e.target.value })
                }
                placeholder="https://api.openai.com/v1"
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item label="API Key">
          <Input.Password
            value={formState.api_key}
            onChange={(e) =>
              setFormState({ ...formState, api_key: e.target.value })
            }
            // 编辑态：后端返回的是脱敏 mask，留空 = 不修改；新增态：正常占位。
            placeholder={isEdit ? '留空则不修改' : 'sk-...'}
          />
        </Form.Item>
        <Divider plain style={{ fontSize: 12, color: '#999' }}>
          采样参数
        </Divider>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item label="Temperature">
              <InputNumber
                value={formState.temperature}
                onChange={(v) =>
                  setFormState({
                    ...formState,
                    temperature: v ?? 0.0,
                  })
                }
                step={0.1}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label="Max Tokens">
              <InputNumber
                value={formState.max_tokens}
                onChange={(v) =>
                  setFormState({
                    ...formState,
                    max_tokens: v ?? 4096,
                  })
                }
                step={256}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
        </Row>
        <Divider plain style={{ fontSize: 12, color: '#999' }}>
          高级连接配置
        </Divider>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item label="API Version">
              <Input
                value={formState.api_version}
                onChange={(e) =>
                  setFormState({
                    ...formState,
                    api_version: e.target.value,
                  })
                }
                placeholder="如 2024-02-15（Anthropic 留空）"
              />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item label="Organization">
              <Input
                value={formState.organization}
                onChange={(e) =>
                  setFormState({
                    ...formState,
                    organization: e.target.value,
                  })
                }
                placeholder="OpenAI 组织 id（可留空）"
              />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item
          label="Extra Headers"
          validateStatus={
            extraHeadersError === 'error' ? 'error' : undefined
          }
          help={
            extraHeadersError === 'error'
              ? 'JSON 格式错误，请检查（如 {"X-Custom":"value"}）'
              : 'JSON 格式自定义请求头，空则不附加'
          }
        >
          <Input.TextArea
            value={extraHeadersText}
            onChange={(e) => handleExtraHeadersChange(e.target.value)}
            placeholder='{"X-Custom-Header": "value"}'
            autoSize={{ minRows: 2, maxRows: 4 }}
          />
        </Form.Item>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item label="Request Timeout (s)">
              <InputNumber
                value={formState.request_timeout}
                onChange={(v) =>
                  setFormState({
                    ...formState,
                    request_timeout: v ?? 120,
                  })
                }
                min={0}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item label="Max Retries">
              <InputNumber
                value={formState.max_retries}
                onChange={(v) =>
                  setFormState({
                    ...formState,
                    max_retries: v ?? 2,
                  })
                }
                min={0}
                style={{ width: '100%' }}
              />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item label="Proxy">
              <Input
                value={formState.proxy}
                onChange={(e) =>
                  setFormState({
                    ...formState,
                    proxy: e.target.value,
                  })
                }
                placeholder="http://127.0.0.1:7890"
              />
            </Form.Item>
          </Col>
        </Row>
        {/* PE-04 模型目录 Table · PE-05 测试连通+拉取模型 */}
        <Divider plain style={{ fontSize: 12, color: '#999' }}>
          模型目录
        </Divider>
        {/* PE-05 测试连通 + 拉取模型：仅编辑态（新增态无 id）。两按钮共享结果展示区。 */}
        {isEdit && (
          <div
            style={{
              marginBottom: 12,
              display: 'flex',
              gap: 8,
              flexWrap: 'wrap',
              alignItems: 'center',
            }}
          >
            <Button
              size="small"
              loading={testing}
              onClick={handleTestConnection}
            >
              测试连通
            </Button>
            <Button
              size="small"
              loading={fetchingModels}
              onClick={handleFetchModels}
            >
              拉取模型
            </Button>
            {/* 测试连通结果：ok=绿 latency_ms / fail=红 error */}
            {testResult && (
              <span
                style={{
                  fontSize: 12,
                  color: testResult.ok ? '#52c41a' : '#ff4d4f',
                }}
              >
                {testResult.ok
                  ? `连通成功 · ${testResult.latency_ms}ms`
                  : `连通失败：${testResult.error || '未知错误'}`}
              </span>
            )}
            {/* 拉取模型结果：仅失败时展示（成功已 message.success + 覆盖目录） */}
            {fetchModelsResult && !fetchModelsResult.ok && (
              <span style={{ fontSize: 12, color: '#ff4d4f' }}>
                拉取失败：{fetchModelsResult.error}
              </span>
            )}
          </div>
        )}
        {/* PE-04 模型目录 Table：每行一个 LlmModel，可增删 + 能力开关 + 单 default Radio。
         *  rowKey 用 index（非 model_id——model_id 可空/重复，作 key 会导致 React
         *  diff 错乱；index 稳定标识行位置，删除时 filter 重建数组 index 自然重排）。 */}
        <div style={{ marginBottom: 8, display: 'flex', gap: 8 }}>
          <Button
            size="small"
            icon={<PlusOutlined />}
            onClick={handleAddModel}
          >
            添加模型
          </Button>
          <span style={{ fontSize: 12, color: '#999', alignSelf: 'center' }}>
            单选「默认」列指定生效模型（全 provider 仅一个）
          </span>
        </div>
        <Table<LlmModel>
          size="small"
          rowKey={(_r, index) => String(index)}
          columns={modelColumns}
          dataSource={formState.models}
          pagination={false}
          scroll={{ x: 640 }}
          locale={{ emptyText: '暂无模型，点击「添加模型」' }}
        />
      </Form>
    </Modal>
  )
}
