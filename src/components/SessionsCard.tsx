import { Card, Empty, Tag, Tooltip } from 'antd'
import { HistoryOutlined } from '@ant-design/icons'
import type { Group, Message } from '../services/api'

interface SessionsCardProps {
  /** 全部群组（groupApi.list）——按 group 聚合会话的骨架；消息按 group_id 匹配到此处。 */
  groups: Group[]
  /** 全部群组最近消息（messageApi.listAll）——按 group_id 聚合统计每会话消息数 + 最后一条预览。 */
  messages: Message[]
  /** 当前聚焦群组 id（高亮当前会话），null = 未选群。 */
  currentGroupId: string | null
}

/** msg.type → 角色前缀（群聊流里区分谁说的；预览用）。 */
const SENDER_PREFIX: Record<string, string> = {
  user_input: '👤',
  agent_reply: '🤖',
  task_log: '🔧',
  task_dispatch: '📤',
  task_complete: '✅',
  task_failed: '❌',
}

/**
 * ISO 时间 → 相对时间文案（会话预览右侧展示，与 SessionList.formatRelativeTime 对齐）。空/无效返回空串。
 */
function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const diff = Date.now() - t
  if (diff < 0) return '刚刚'
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  const day = Math.floor(hr / 24)
  if (day < 7) return `${day} 天前`
  return new Date(iso).toLocaleDateString()
}

/**
 * SC-08 `/sessions` 结果卡片：按 group 聚合列历史会话。
 *
 * 数据来源：groupApi.list（全部群组，作为会话骨架）+ messageApi.listAll（全部群组最近消息，
 * 跨群聚合做消息数统计 + 最后消息预览）。每个 group = 一个会话，按最后活跃时间倒序
 * （有消息的取最后消息时间，无消息的取 group.updated_at）。
 *
 * 设计：
 *  - 每个会话一行：名称（当前会话蓝加粗高亮）+ 消息数 Tag + 最近活跃相对时间。
 *  - 第二行预览：最后一条消息内容（单行 ellipsis 截断），msg.type 角色前缀区分发言者。
 *  - 当前会话（currentGroupId）蓝边高亮，与 SessionList 视觉呼应。
 *  - 顶部摘要：总会话数 + 有消息会话数 + 总消息数。
 *  - 无会话/无消息友好占位（Empty simple）。
 *
 * 与 SessionList 区别：SessionList 是左侧栏常驻导航（切群用，时间用 group.updated_at 近似，
 * 不拉消息预览避免 N+1）；/sessions 卡片是命令触发的「历史会话概览」快照——补上消息数 +
 * 最后预览（messageApi.listAll 一次拉全跨群聚合，非 N+1）。
 */
export default function SessionsCard({ groups, messages, currentGroupId }: SessionsCardProps) {
  // 按 group_id 聚合消息：统计每会话消息数 + 最后一条消息
  // Path C: Message.group_id 改 optional（后端 emit 双 key，conversation_id 主 +
  // group_id 兼容），fallback 到 conversation_id（两者值相同）。
  const stats = new Map<string, { count: number; last: Message | null }>()
  for (const g of groups) stats.set(g.id, { count: 0, last: null })
  for (const m of messages) {
    const gid = m.group_id ?? m.conversation_id
    if (!gid) continue
    const s = stats.get(gid)
    if (!s) continue
    s.count += 1
    if (!s.last || new Date(m.created_at).getTime() > new Date(s.last.created_at).getTime()) {
      s.last = m
    }
  }

  // 会话排序：最后活跃时间倒序（有消息取 last.created_at，无消息取 group.updated_at）
  const sessions = [...groups].sort((a, b) => {
    const sa = stats.get(a.id)
    const sb = stats.get(b.id)
    const ta = sa?.last ? new Date(sa.last.created_at).getTime() : new Date(a.updated_at || 0).getTime()
    const tb = sb?.last ? new Date(sb.last.created_at).getTime() : new Date(b.updated_at || 0).getTime()
    return tb - ta
  })

  const totalMessages = messages.length
  const activeSessions = sessions.filter((g) => (stats.get(g.id)?.count ?? 0) > 0).length

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#69b1ff' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <HistoryOutlined style={{ color: '#1677ff' }} />
          <Tag color="blue" style={{ margin: 0 }}>历史会话</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {sessions.length} 个会话
            {sessions.length > 0 && (
              <>{`（活跃 ${activeSessions} · 消息 ${totalMessages}）`}</>
            )}
          </span>
        </span>
      }
    >
      {sessions.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无会话"
          style={{ margin: '8px 0' }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {sessions.map((g) => {
            const s = stats.get(g.id)!
            const isActive = currentGroupId === g.id
            const preview = s.last?.content?.trim() || ''
            const prefix = s.last ? (SENDER_PREFIX[s.last.type] ?? '💬') : ''
            return (
              <div
                key={g.id}
                style={{
                  padding: '8px 10px',
                  background: isActive ? '#e6f4ff' : '#fafafa',
                  borderRadius: 4,
                  border: `1px solid ${isActive ? '#91d5ff' : '#f0f0f0'}`,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 8,
                  }}
                >
                  <span
                    style={{
                      fontWeight: isActive ? 600 : 500,
                      fontSize: 13,
                      color: isActive ? '#1677ff' : '#333',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      flex: 1,
                      minWidth: 0,
                    }}
                  >
                    {g.name}
                  </span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                    {s.count > 0 && <Tag color="blue" style={{ margin: 0 }}>{s.count} 条</Tag>}
                    <span style={{ fontSize: 11, color: '#bbb' }}>
                      {formatRelativeTime(s.last?.created_at || g.updated_at)}
                    </span>
                  </div>
                </div>
                {preview && (
                  <Tooltip title={preview}>
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
                      {prefix} {preview}
                    </div>
                  </Tooltip>
                )}
                {!preview && g.description && (
                  <div
                    style={{
                      fontSize: 12,
                      color: '#bbb',
                      marginTop: 4,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {g.description}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
