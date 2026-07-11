import { Card, Empty, Tag } from 'antd'
import { ExperimentOutlined } from '@ant-design/icons'
import type { Skill } from '../services/api'

interface SkillsCardProps {
  skills: Skill[]
}

/** source → Tag 颜色（与 SkillPage SOURCE_COLOR 对齐，保持全应用一致）。 */
const SOURCE_COLOR: Record<string, string> = {
  builtin: 'green',
  custom: 'blue',
  market: 'orange',
}

/**
 * SC-06 `/skills` 结果卡片：浏览已安装技能列表。
 *
 * 数据来自 `GET /api/skills`（skillApi.list）——本地已落库的技能（builtin/custom/market
 * 三来源），区别于 SkillPage 的「市场浏览」（SkillMarketEntry，可发现待安装未落库）。
 * /skills 命令聚焦「已装的有什么」，故用 skillApi.list 而非市场 catalog。
 *
 * 设计：
 *  - 紧凑列表：每个技能一行——name（粗体）+ source Tag（颜色区分来源：builtin 绿/custom 蓝/
 *    market 橙，与 SkillPage 一致）+ tags（灰底小 Tag）+ mounted_to 计数徽标（已挂载到 N 个 agent）。
 *  - 空列表 antd Empty 占位（无技能时友好提示，非空 div）。
 *  - 标题展示总数 + 来源分布（builtin N · custom N · market N），一眼掌握技能库构成。
 *  - description 截断展示（单行 ellipsis，避免长描述撑高卡片）。
 *  - 不展示 content（技能正文可能很长，/skills 是浏览列表不是查看详情；详情走 SkillPage）。
 */
export default function SkillsCard({ skills }: SkillsCardProps) {
  const bySource = (src: string) => skills.filter((s) => s.source === src).length
  const builtinCount = bySource('builtin')
  const customCount = bySource('custom')
  const marketCount = bySource('market')

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#b7eb8f' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ExperimentOutlined style={{ color: '#52c41a' }} />
          <Tag color="green" style={{ margin: 0 }}>技能列表</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {skills.length} 个
            {skills.length > 0 && (
              <>
                （内置 {builtinCount} · 自定义 {customCount} · 市场 {marketCount}）
              </>
            )}
          </span>
        </span>
      }
    >
      {skills.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无已安装技能"
          style={{ margin: '8px 0' }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {skills.map((skill) => (
            <div
              key={skill.id}
              style={{
                padding: '8px 10px',
                background: '#fafafa',
                borderRadius: 4,
                border: '1px solid #f0f0f0',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  flexWrap: 'wrap',
                }}
              >
                <span style={{ fontWeight: 600, fontSize: 13, color: '#333' }}>
                  {skill.name}
                </span>
                <Tag color={SOURCE_COLOR[skill.source] ?? 'default'} style={{ margin: 0 }}>
                  {skill.source}
                </Tag>
                {skill.mounted_to.length > 0 && (
                  <Tag color="purple" style={{ margin: 0 }}>
                    已挂载 {skill.mounted_to.length}
                  </Tag>
                )}
                {skill.tags.slice(0, 3).map((tag) => (
                  <Tag key={tag} style={{ margin: 0, fontSize: 11 }}>
                    {tag}
                  </Tag>
                ))}
              </div>
              {skill.description && (
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
                  {skill.description}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
