import { useEffect, useState, type ReactNode } from 'react'
import { Tag } from 'antd'
import {
  BulbOutlined,
  ToolOutlined,
  ApiOutlined,
} from '@ant-design/icons'
import {
  skillApi,
  mcpApi,
  type AgentDefinition,
  type GroupMember,
  type Skill,
} from '../services/api'

/** Drawer 内成员条目（含 isCoordinator 标记，群主也入列展示能力）。 */
interface DrawerMemberItem extends GroupMember {
  isCoordinator?: boolean
}

/**
 * MT-05 成员能力概况（技能/工具）聚合展示组件。
 *
 * 把群主 + 成员各自的能力栈归类汇总成几行（图标 + 标题 + Tag 列表），让用户在
 * 群信息抽屉一眼看到团队整体能力盘，而不必逐个点开 AgentPage 查：
 *  - 角色技能：agent.skills + agent.extra_skills（去重，角色身份自带的能力）
 *  - 已挂载技能：agent.mounted_skills（技能 id，经 skillNameMap 解析为可读技能名）
 *  - 可用工具：agent.allowed_tools（工具白名单，AG-05）
 *  - 禁用工具：agent.denied_tools（工具黑名单，前缀「禁:」与可用区分）
 *  - MCP 工具源：agent.mounted_mcp（MCP 连接 id，经 mcpNameMap 解析为可读连接名）
 *
 * 聚合规则：跨成员去重（同一技能多人挂载只显示一次，反映「团队级」能力盘）。
 * 空能力的类别不渲染该行；全部为空时显示占位「暂无能力配置」。
 *
 * L2-02 从 GroupPage / GroupInfoDrawer 提取为独立组件。
 *
 * skillNameMap / mcpNameMap 加载逻辑内聚在本组件：挂载时并行拉 skillApi.list +
 * mcpApi.list 建 id→name 映射，用于解析 mounted_skills / mounted_mcp 为可读名。
 * 失败静默（后端未起时降级显示原始 id，不阻断渲染）。映射只拉一次（全局技能/MCP
 * 名册不随群组变化），故空依赖数组——组件多次挂载/卸载重拉也无妨（轻量 GET）。
 */
interface MemberCapabilityOverviewProps {
  /** 成员列表（含群主，群主 isCoordinator=true）。 */
  members: DrawerMemberItem[]
  /** 全部智能体（按成员 agent_id 匹配取能力栈）。 */
  agents: AgentDefinition[]
}

export default function MemberCapabilityOverview({
  members,
  agents,
}: MemberCapabilityOverviewProps) {
  // skill id → name / mcp id → name 映射（内聚加载，解析 mounted_skills / mounted_mcp）
  const [skillNameMap, setSkillNameMap] = useState<Record<string, string>>({})
  const [mcpNameMap, setMcpNameMap] = useState<Record<string, string>>({})

  useEffect(() => {
    Promise.all([skillApi.list(), mcpApi.list()])
      .then(([skillList, mcpList]) => {
        const sm: Record<string, string> = {}
        skillList.forEach((s: Skill) => {
          sm[s.id] = s.name
        })
        setSkillNameMap(sm)
        const mm: Record<string, string> = {}
        mcpList.forEach((c) => {
          mm[c.id] = c.name
        })
        setMcpNameMap(mm)
      })
      .catch(() => {
        /* 静默：后端未起时降级显示原始 id，不阻断能力盘渲染 */
      })
  }, [])

  const memberAgentIds = new Set(members.map((m) => m.agent_id))
  const rosterAgents = agents.filter((a) => memberAgentIds.has(a.id))

  const roleSkills = Array.from(
    new Set(rosterAgents.flatMap((a) => [...(a.skills ?? []), ...(a.extra_skills ?? [])])),
  )
  const mountedSkillNames = Array.from(
    new Set(rosterAgents.flatMap((a) => a.mounted_skills ?? [])),
  ).map((id) => skillNameMap[id] ?? id)
  const allowedTools = Array.from(
    new Set(rosterAgents.flatMap((a) => a.allowed_tools ?? [])),
  )
  const deniedTools = Array.from(
    new Set(rosterAgents.flatMap((a) => a.denied_tools ?? [])),
  )
  const mountedMcpNames = Array.from(
    new Set(rosterAgents.flatMap((a) => a.mounted_mcp ?? [])),
  ).map((id) => mcpNameMap[id] ?? id)

  const sections: Array<{
    key: string
    icon: ReactNode
    title: string
    items: string[]
    color: string
    tagColor: 'purple' | 'geekblue' | 'green' | 'red' | 'orange'
    prefix?: string
  }> = [
    { key: 'role', icon: <BulbOutlined />, title: '角色技能', items: roleSkills, color: '#722ed1', tagColor: 'purple' as const },
    { key: 'mounted', icon: <ToolOutlined />, title: '已挂载技能', items: mountedSkillNames, color: '#F26522', tagColor: 'geekblue' as const },
    { key: 'allowed', icon: <ApiOutlined />, title: '可用工具', items: allowedTools, color: '#52c41a', tagColor: 'green' as const },
    { key: 'denied', icon: <ToolOutlined />, title: '禁用工具', items: deniedTools, color: '#ff4d4f', tagColor: 'red' as const, prefix: '禁:' },
    { key: 'mcp', icon: <ApiOutlined />, title: 'MCP 工具源', items: mountedMcpNames, color: '#fa8c16', tagColor: 'orange' as const },
  ].filter((s) => s.items.length > 0)

  return (
    <div style={{ padding: '12px 0' }}>
      <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
        <ApiOutlined style={{ color: '#F26522' }} />
        成员能力概况
      </div>
      {sections.length === 0 ? (
        <div style={{ fontSize: 12, color: '#b0b0b0', background: '#f5f5f5', padding: '8px 12px', borderRadius: 4 }}>
          暂无能力配置
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {sections.map((sec) => (
            <div key={sec.key}>
              <div style={{ fontSize: 12, color: '#666', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ color: sec.color }}>{sec.icon}</span>
                {sec.title}
                <span style={{ color: '#bbb' }}>({sec.items.length})</span>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, paddingLeft: 18 }}>
                {sec.items.map((s, i) => (
                  <Tag key={`${sec.key}-${s}-${i}`} color={sec.tagColor} style={{ margin: 0, fontSize: 11, lineHeight: '18px', padding: '0 6px' }}>
                    {sec.prefix ? `${sec.prefix}${s}` : s}
                  </Tag>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export type { DrawerMemberItem }
