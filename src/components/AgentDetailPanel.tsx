import { Card, Empty, Tag, Tooltip, Typography } from 'antd'
import { RobotOutlined } from '@ant-design/icons'
import type { AgentDefinition } from '../services/api'

const { Paragraph } = Typography

interface AgentDetailPanelProps {
  /**
   * 待展示的智能体列表（handler 已解析）。
   * - 长度 === 1 → 完整聚合详情卡片（system_prompt 摘要 + mounted_skills/mcp + allowed/denied
   *   tools + model + max_turns），即 SC-09 「打开 AgentDetailPanel 聚合卡片」的主路径。
   * - 长度 > 1 → 紧凑名册列表（name/role + 挂载计数），提示用 `/agent <名称>` 查看详情。
   * - 长度 === 0 → Empty 占位（无匹配智能体）。
   */
  agents: AgentDefinition[]
}

/**
 * 截断长文本为概要（system_prompt 可能很长，详情卡只展示前 N 字 + Tooltip 全文）。
 * 保留换行：先去首尾空白，超长截断加省略号；空内容返回空串（调用方据此判空不渲染）。
 */
function summarize(text: string | null | undefined, max = 200): string {
  if (!text) return ''
  const t = text.trim()
  if (t.length <= max) return t
  return t.slice(0, max) + '…'
}

/**
 * SC-09 `/agent` 结果卡片（含 AD-01 只读聚合详情）。
 *
 * 数据来自 `agentApi.list()`（前端按 name/id 解析到目标 agent）——一个智能体的全部配置聚合
 * 展示：身份（name/role/description）+ system_prompt 摘要 + 核心技能 skills/extra_skills +
 * 已挂载技能 mounted_skills + 已挂载 MCP mounted_mcp + 工具权限 allowed/denied_tools +
 * 运行参数 model/max_turns。让用户在聊天里 /agent 快速回看某智能体的完整配置画像，
 * 不必跳 AgentPage 翻卡片。
 *
 * 设计：
 *  - 两种模式：单 agent → 完整聚合详情；多 agent → 紧凑名册列表（提示按名查详情）。
 *  - 详情按「身份 → 角色描述 → 核心技能 → 已挂载技能 → 已挂载 MCP → 工具权限 → 运行参数」
 *    分段，每段标题灰底小标签，值用 Tag 行或文本，空段不渲染（避免空标签行噪音）。
 *  - system_prompt 只展示前 200 字摘要 + Tooltip 全文（system_prompt 可能数百字，全展开撑爆卡片）。
 *  - 紫边卡片 #d3adf7——智能体是「系统配置」类（与 ModelCard 紫边呼应），区别于查看类蓝边。
 *
 * 与 AgentPage 区别：AgentPage 是管理页（CRUD + 编辑 Modal + 编辑入口 AD-02）；/agent 卡片是
 * 聊天流内只读聚合详情快照（AD-01 范围：只展示不编辑）。编辑入口由后续 AD-02 任务接入。
 */
export default function AgentDetailPanel({ agents }: AgentDetailPanelProps) {
  // 多 agent：紧凑名册列表
  if (agents.length > 1) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12, borderColor: '#d3adf7' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#722ed1' }} />
            <Tag color="purple" style={{ margin: 0 }}>智能体名册</Tag>
            <span style={{ fontSize: 13, color: '#666' }}>共 {agents.length} 个 · 用 /agent 名称 查看详情</span>
          </span>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {agents.map((a) => {
            const skillCount = (a.mounted_skills?.length ?? 0) + (a.mounted_mcp?.length ?? 0)
            return (
              <div
                key={a.id}
                style={{
                  padding: '8px 10px',
                  background: '#fafafa',
                  borderRadius: 4,
                  border: '1px solid #f0f0f0',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, fontSize: 13, color: '#333' }}>{a.name}</span>
                  <Tag color="purple" style={{ margin: 0 }}>{a.role}</Tag>
                  {skillCount > 0 && (
                    <Tag style={{ margin: 0 }}>挂载 {skillCount}</Tag>
                  )}
                </div>
                {a.description && (
                  <div
                    style={{
                      fontSize: 12,
                      color: '#999',
                      marginTop: 4,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {a.description}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </Card>
    )
  }

  // 无 agent：占位
  const agent = agents[0]
  if (!agent) {
    return (
      <Card
        size="small"
        style={{ marginBottom: 12, borderColor: '#d3adf7' }}
        title={
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <RobotOutlined style={{ color: '#722ed1' }} />
            <Tag color="purple" style={{ margin: 0 }}>智能体详情</Tag>
          </span>
        }
      >
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="未找到匹配的智能体"
          style={{ margin: '8px 0' }}
        />
      </Card>
    )
  }

  // 单 agent：完整聚合详情
  const sysSummary = summarize(agent.system_prompt, 200)
  const hasSys = sysSummary.length > 0
  const allSkills = [...(agent.skills ?? []), ...(agent.extra_skills ?? [])]
  const mountedSkills = agent.mounted_skills ?? []
  const mountedMcp = agent.mounted_mcp ?? []
  const allowed = agent.allowed_tools ?? []
  const denied = agent.denied_tools ?? []
  const hasTools = allowed.length > 0 || denied.length > 0
  const hasRuntime = agent.model != null || agent.max_turns != null

  /** 分段标题：灰底小标签，保持视觉一致。 */
  const Section = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 11, color: '#999', marginBottom: 4 }}>{title}</div>
      {children}
    </div>
  )

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#d3adf7' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <RobotOutlined style={{ color: '#722ed1' }} />
          <Tag color="purple" style={{ margin: 0 }}>{agent.role}</Tag>
          <span style={{ fontSize: 13, color: '#333', fontWeight: 600 }}>{agent.name}</span>
        </span>
      }
    >
      {/* 身份 */}
      {agent.description && (
        <Section title="定位">
          <span style={{ fontSize: 12, color: '#666' }}>{agent.description}</span>
        </Section>
      )}

      {/* 角色描述（system_prompt 摘要） */}
      {hasSys && (
        <Section title="角色描述（system_prompt 摘要）">
          <Tooltip title={agent.system_prompt}>
            <Paragraph
              style={{ margin: 0, fontSize: 12, color: '#555', whiteSpace: 'pre-wrap' }}
            >
              {sysSummary}
            </Paragraph>
          </Tooltip>
        </Section>
      )}

      {/* 核心技能 */}
      {allSkills.length > 0 && (
        <Section title="核心技能">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {allSkills.map((s) => (
              <Tag key={s} color="purple" style={{ margin: 0 }}>{s}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 已挂载技能 */}
      {mountedSkills.length > 0 && (
        <Section title="已挂载技能">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {mountedSkills.map((sid) => (
              <Tag key={sid} color="geekblue" style={{ margin: 0 }}>{sid}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 已挂载 MCP */}
      {mountedMcp.length > 0 && (
        <Section title="已挂载 MCP">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {mountedMcp.map((mid) => (
              <Tag key={mid} color="magenta" style={{ margin: 0 }}>{mid}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 工具权限 */}
      {hasTools && (
        <Section title="工具权限">
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {allowed.map((t) => (
              <Tag key={t} color="green" style={{ margin: 0 }}>{t}</Tag>
            ))}
            {denied.map((t) => (
              <Tag key={t} color="red" style={{ margin: 0 }}>禁:{t}</Tag>
            ))}
          </div>
        </Section>
      )}

      {/* 运行参数 */}
      {hasRuntime && (
        <Section title="运行参数">
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
            {agent.model && (
              <span style={{ color: '#666' }}>
                模型: <Tag color="blue" style={{ margin: 0 }}>{agent.model}</Tag>
              </span>
            )}
            {agent.max_turns != null && (
              <span style={{ color: '#666' }}>
                最大轮次: <Tag style={{ margin: 0 }}>{agent.max_turns}</Tag>
              </span>
            )}
          </div>
        </Section>
      )}

      {/* 空配置兜底：啥都没有时给个提示，避免卡片只剩标题 */}
      {!agent.description && !hasSys && allSkills.length === 0 &&
        mountedSkills.length === 0 && mountedMcp.length === 0 && !hasTools && !hasRuntime && (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="该智能体无额外配置"
          style={{ margin: '8px 0' }}
        />
      )}
    </Card>
  )
}
