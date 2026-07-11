import { Card, Descriptions, Tag } from 'antd'
import { CheckCircleFilled, ApiOutlined, GlobalOutlined, KeyOutlined } from '@ant-design/icons'
import type { LlmConfig } from '../services/api'

interface ModelCardProps {
  config: LlmConfig
  /** 是否为切换后的快照（true = 切换成功反馈；false = 仅查看）。
   *  仅影响标题文案与边框色，正文 Descriptions 一致。 */
  switched?: boolean
}

/**
 * SC-04 `/model` 结果卡片：把 LLM 配置快照渲染进聊天流。
 *
 * 后端 GET/PUT /api/config 返回脱敏配置（api_key 仅首尾 3 字符预览，真实密钥不离开进程）。
 * 本卡片用 antd Descriptions 紧凑展示关键字段——model（核心，热切换对象）、provider、base_url、
 * api_key（脱敏预览 + 已配置徽标）、temperature、max_tokens。
 *
 * 设计：
 *  - Descriptions size="small" column=1：聊天流内卡片宽度受限，单列纵向铺字段最清晰，不挤。
 *  - model 用 Tag color="blue" 高亮：它是 /model 命令的主体 + 唯一可变字段，视觉提权。
 *  - api_key 旁加「已配置/未配置」Tag：has_key=true 绿色 CheckCircleFilled 已配置，false 红色未配置
 *    ——密钥脱敏不可读，但配置状态必须显式可见（用户需知道密钥是否就绪才能判断 LLM 能否调通）。
 *  - switched=true 时标题加 ✅ + 紫色边框：与「仅查看」区分，给「切换成功」即时反馈。
 *  - provider/base_url/temperature/max_tokens 只读展示：后端仅 model 可热切换（set_config 只写
 *    LLM_MODEL），其余 env-driven，展示让用户知道当前 provider/端点（排查「为啥走 deepseek」类问题）。
 */
export default function ModelCard({ config, switched = false }: ModelCardProps) {
  return (
    <Card
      size="small"
      style={{
        marginBottom: 12,
        borderColor: switched ? '#b37feb' : '#d3adf7',
      }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {switched && <CheckCircleFilled style={{ color: '#52c41a' }} />}
          <Tag color="purple" style={{ margin: 0 }}>LLM 配置</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            {switched ? '已切换模型' : '当前配置'}
          </span>
        </span>
      }
    >
      <Descriptions size="small" column={1} labelStyle={{ width: 96, color: '#666' }}>
        <Descriptions.Item label="模型">
          <Tag color="blue">{config.model}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="Provider">
          <ApiOutlined style={{ marginRight: 6, color: '#1677ff' }} />
          {config.provider}
        </Descriptions.Item>
        <Descriptions.Item label="Base URL">
          <GlobalOutlined style={{ marginRight: 6, color: '#1677ff' }} />
          <span style={{ fontSize: 12, wordBreak: 'break-all' }}>{config.base_url}</span>
        </Descriptions.Item>
        <Descriptions.Item label="API Key">
          <KeyOutlined style={{ marginRight: 6, color: '#faad14' }} />
          {config.has_key ? (
            <>
              <code style={{ fontSize: 12 }}>{config.api_key}</code>
              <Tag color="green" style={{ marginLeft: 8 }}>已配置</Tag>
            </>
          ) : (
            <Tag color="red">未配置</Tag>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Temperature">{config.temperature}</Descriptions.Item>
        <Descriptions.Item label="Max Tokens">{config.max_tokens}</Descriptions.Item>
      </Descriptions>
    </Card>
  )
}
