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
import { useState } from 'react'
import { Modal, Form, Input, Divider } from 'antd'
import type { LlmProvider, LlmModel } from '../services/api'

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
        <Form.Item label="名称" required>
          <Input
            value={formState.name}
            onChange={(e) =>
              setFormState({ ...formState, name: e.target.value })
            }
            placeholder="如 OpenAI 官方、DeepSeek"
          />
        </Form.Item>
        {/* PE-02 预设选择器 · PE-03 连接配置 · PE-04 模型目录 Table · PE-05 测试连通+拉取模型 */}
        <Divider plain style={{ fontSize: 12, color: '#999' }}>
          预设选择器 · 连接配置 · 模型目录 · 测试连通（PE-02~05 待实现）
        </Divider>
      </Form>
    </Modal>
  )
}
