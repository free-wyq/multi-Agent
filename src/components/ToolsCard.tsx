import { Card, Empty, Tag } from 'antd'
import { ToolOutlined, ApiOutlined } from '@ant-design/icons'
import type { SlashToolsResult, ToolPreviewItem } from '../services/api'

interface ToolsCardProps {
  result: SlashToolsResult
}

/**
 * SC-05 `/tools` 结果卡片：聚合展示「内置工具 + 各 mounted_mcp 工具」。
 *
 * 数据来自后端 BE-01 `POST /api/slash` command=tools（单一真源——内置工具定义在
 * engine.tools.tools_for_group，前端硬编码会漂移；MCP 工具需 async 自省，前端逐连接
 * GET /api/mcp/{id}/tools 是 N+1 且看不到合并集）。一次调用返回 internal + mcp 两段。
 *
 * 设计：
 *  - 两段分区：内置工具（ToolOutlined 蓝色图标）+ MCP 工具（ApiOutlined 紫色图标），
 *    视觉区分工具来源——内置是框架自带（read_file/write_file/edit_file…），MCP 是外部连接挂载。
 *  - 每个工具一行：`name`（code 等宽蓝底）+ description（灰字，后端已截断 200 字符）。
 *  - 空段用 antd Empty 紧凑占位（image={false} 无插画，仅文字「无」），比空 div 更明确。
 *  - 标题展示总数（internal + mcp = total），让用户一眼看到「这个 agent 能调多少工具」。
 *  - 后端 ok=false（MCP 加载失败）时顶部红色 Alert 展示 error，仍展示 internal 段（已加载的）。
 */
function ToolGroup({
  title,
  icon,
  color,
  tools,
}: {
  title: string
  icon: React.ReactNode
  color: string
  tools: ToolPreviewItem[]
}) {
  return (
    <div>
      <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ color }}>{icon}</span>
        <span style={{ fontWeight: 600, fontSize: 13 }}>{title}</span>
        <Tag style={{ margin: 0 }}>{tools.length}</Tag>
      </div>
      {tools.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="无"
          style={{ margin: '4px 0 8px 0' }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {tools.map((t) => (
            <div
              key={t.name}
              style={{
                padding: '4px 8px',
                background: '#fafafa',
                borderRadius: 4,
                border: '1px solid #f0f0f0',
              }}
            >
              <code
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: '#1677ff',
                  background: '#f0f5ff',
                  padding: '1px 6px',
                  borderRadius: 4,
                  marginRight: 8,
                }}
              >
                {t.name}
              </code>
              {t.description && (
                <span style={{ fontSize: 12, color: '#666' }}>{t.description}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function ToolsCard({ result }: ToolsCardProps) {
  const internal = result.tools?.internal ?? []
  const mcp = result.tools?.mcp ?? []

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#91d5ff' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ToolOutlined style={{ color: '#1677ff' }} />
          <Tag color="blue" style={{ margin: 0 }}>工具清单</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {result.total ?? internal.length + mcp.length} 个
            （内置 {internal.length} · MCP {mcp.length}）
          </span>
        </span>
      }
    >
      {result.ok === false && result.error && (
        <div
          style={{
            marginBottom: 8,
            padding: '6px 10px',
            background: '#fff2f0',
            border: '1px solid #ffccc7',
            borderRadius: 4,
            fontSize: 12,
            color: '#cf1322',
          }}
        >
          ⚠️ {result.error}
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <ToolGroup
          title="内置工具"
          icon={<ToolOutlined />}
          color="#1677ff"
          tools={internal}
        />
        <ToolGroup
          title="MCP 工具"
          icon={<ApiOutlined />}
          color="#722ed1"
          tools={mcp}
        />
      </div>
    </Card>
  )
}
