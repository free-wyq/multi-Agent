import { Card, Empty, Tag } from 'antd'
import { ApiOutlined, CloudOutlined, CodeOutlined } from '@ant-design/icons'
import type { McpConnection } from '../services/api'

interface McpListCardProps {
  /** 全部 MCP 连接（mcpApi.list）——按 created_at 排序，前端展示。 */
  connections: McpConnection[]
}

/** transport → Tag 颜色 + 图标 + 标签（与 McpPage TRANSPORT_META 对齐，全应用一致）。 */
const TRANSPORT_META: Record<
  string,
  { color: string; icon: React.ReactNode; label: string }
> = {
  stdio: { color: 'geekblue', icon: <CodeOutlined />, label: 'stdio' },
  sse: { color: 'purple', icon: <CloudOutlined />, label: 'sse' },
}

/** 拼接 stdio 启动命令预览（command + args.join），一眼看出会 spawn 什么。与 McpPage 同算法。 */
function stdioCommandPreview(conn: McpConnection): string {
  const parts = [conn.command, ...(conn.args ?? [])].filter(Boolean)
  return parts.length ? parts.join(' ') : '（未配置命令）'
}

/**
 * SC-10 `/mcp` 结果卡片：内联展示 MCP 连接列表。
 *
 * 数据来自 `GET /api/mcp`（mcpApi.list）——全部 MCP 连接（stdio/sse 两种传输），区别于
 * McpPage 的管理页（CRUD + 工具自省 Collapse + enable/disable 切换）。/mcp 是「一眼看装了哪些
 * MCP 连接、各自什么传输、启没启用」的列表概览，不带编辑操作（编辑走 McpPage）。
 *
 * 设计：
 *  - 每连接一行：name（粗）+ transport Tag（stdio geekblue/sse purple，颜色同 McpPage）+
 *    enabled 状态 Tag（启用 success 绿 / 禁用 default 灰）。
 *  - 第二行预览：stdio→命令行预览（command + args），sse→url（单行 ellipsis 截断）。
 *  - 顶部摘要：总数 + 启用/禁用计数。
 *  - 空列表 Empty simple 占位。
 *
 * 与 McpPage 区别：McpPage 是管理页（创建 Modal + 工具自省 + enable/disable 切换 + 删除）；
 * /mcp 卡片是聊天流内只读列表快照（连接名/传输/状态/命令或 URL 预览），轻量浏览不编辑。
 */
export default function McpListCard({ connections }: McpListCardProps) {
  const enabledCount = connections.filter((c) => c.enabled).length
  const disabledCount = connections.length - enabledCount

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#ffadd2' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ApiOutlined style={{ color: '#eb2f96' }} />
          <Tag color="magenta" style={{ margin: 0 }}>MCP 连接</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {connections.length} 个
            {connections.length > 0 && (
              <>{`（启用 ${enabledCount} · 禁用 ${disabledCount}）`}</>
            )}
          </span>
        </span>
      }
    >
      {connections.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无 MCP 连接"
          style={{ margin: '8px 0' }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {connections.map((conn) => {
            const meta = TRANSPORT_META[conn.transport] ?? {
              color: 'default',
              icon: <ApiOutlined />,
              label: conn.transport,
            }
            const preview =
              conn.transport === 'sse' ? conn.url || '（未配置 URL）' : stdioCommandPreview(conn)
            return (
              <div
                key={conn.id}
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
                    {conn.name}
                  </span>
                  <Tag color={meta.color} style={{ margin: 0 }}>
                    {meta.icon} {meta.label}
                  </Tag>
                  <Tag color={conn.enabled ? 'success' : 'default'} style={{ margin: 0 }}>
                    {conn.enabled ? '启用' : '禁用'}
                  </Tag>
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: '#999',
                    marginTop: 4,
                    fontFamily: 'monospace',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {preview}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
