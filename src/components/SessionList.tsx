import { Button, Empty, Spin, Tooltip } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import type { Group } from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'

/**
 * SH-01 会话列表（左侧栏）。
 *
 * 把「群组」重映射为「会话」呈现：每个群组 = 一个对话会话（当前 1:1，reset-session
 * 清空即「开新会话」），按 group 聚合成一行——头像 + 名称 + 描述/时间，最近活跃在上。
 *
 * - 会话列表（按 group 聚合）：groups 按 updated_at 倒序，一行一群组。
 * - 新建会话入口：顶部「新建会话」按钮 → onNewSession（父组件打开新建群组 Modal）。
 * - 高亮当前：活跃群组（BusEventContext.groupId）行蓝底加粗。
 *
 * 数据源：groups/loading/onNewSession 由父组件传入（父负责拉取）；活跃群组 id +
 * 切群 setter 从 BusEventContext 读（复用全局共享 WS 态，不自起 WS、不重复订阅）。
 * 不拉每群消息做预览——后端无 last-message 端点，N+1 拉取是性能风险且非本组件职责，
 * 时间戳用 group.updated_at（群组级最近变更）作会话活跃度近似，留后续 last-message
 * 端点就绪后再接预览。
 */
interface SessionListProps {
  groups: Group[]
  loading?: boolean
  /** 新建会话入口回调（父组件打开新建群组 Modal）。 */
  onNewSession?: () => void
}

/** ISO 时间 → 相对时间文案（会话列表右侧展示）。空/无效返回空串。 */
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

export default function SessionList({ groups, loading, onNewSession }: SessionListProps) {
  // 活跃群组 + 切群 setter 走全局共享态（WS 系列后全应用一条 WS）。
  const { groupId, setGroupId } = useBusEventContext()

  // 按 group 聚合：一行一群组，最近活跃（updated_at 倒序）在上。
  const sorted = [...groups].sort(
    (a, b) => new Date(b.updated_at || 0).getTime() - new Date(a.updated_at || 0).getTime(),
  )

  return (
    <div
      style={{
        width: 240,
        flexShrink: 0,
        borderRight: '1px solid #f0f0f0',
        display: 'flex',
        flexDirection: 'column',
        background: '#fff',
        height: '100%',
      }}
    >
      {/* 新建会话入口 */}
      <div
        style={{
          padding: '8px 12px',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          flexShrink: 0,
        }}
      >
        <Tooltip title={onNewSession ? '新建一个会话（群组）' : undefined}>
          <Button
            type="text"
            icon={<PlusOutlined />}
            size="small"
            disabled={!onNewSession}
            onClick={onNewSession}
          >
            新建会话
          </Button>
        </Tooltip>
      </div>

      {/* 会话列表 */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 8px' }}>
        {loading ? (
          <div style={{ textAlign: 'center', padding: 20 }}>
            <Spin size="small" />
          </div>
        ) : sorted.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无会话"
            style={{ margin: '12px 0' }}
          />
        ) : (
          sorted.map((g) => {
            const active = groupId === g.id
            return (
              <div
                key={g.id}
                onClick={() => setGroupId(g.id)}
                style={{
                  padding: '10px 12px',
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: active ? '#FFF3ED' : 'transparent',
                  transition: 'background 0.2s',
                  marginBottom: 2,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                }}
                onMouseEnter={(e) => {
                  if (!active) (e.currentTarget as HTMLDivElement).style.background = '#f5f5f5'
                }}
                onMouseLeave={(e) => {
                  if (!active) (e.currentTarget as HTMLDivElement).style.background = 'transparent'
                }}
              >
                <img
                  src="/group-avatar.png"
                  alt=""
                  style={{ width: 40, height: 40, borderRadius: 6, objectFit: 'cover', flexShrink: 0 }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      gap: 6,
                    }}
                  >
                    <span
                      style={{
                        fontWeight: active ? 600 : 400,
                        fontSize: 14,
                        color: active ? '#F26522' : '#333',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        flex: 1,
                        minWidth: 0,
                      }}
                    >
                      {g.name}
                    </span>
                    <span style={{ fontSize: 11, color: '#bbb', flexShrink: 0 }}>
                      {formatRelativeTime(g.updated_at)}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: 12,
                      color: '#999',
                      marginTop: 2,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {g.description || '暂无描述'}
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
