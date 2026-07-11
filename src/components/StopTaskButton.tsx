import { useState } from 'react'
import { Button, Popconfirm, Tooltip, message } from 'antd'
import { StopOutlined } from '@ant-design/icons'
import { taskApi } from '../services/api'

interface StopTaskButtonProps {
  /** 要停止的任务 id（tq_ 运行时 id，即 agent_status 的 current_task_id）。 */
  taskId: string
  /** 任务所属群组 id（后端按 group_id 缩小引擎扫描，tq_ 全局唯一故可省略）。 */
  groupId: string
  /** 可选：智能体名，用于 toast 文案与 tooltip。 */
  agentName?: string
  /** 按钮尺寸，默认 small（监控面板紧凑场景）。 */
  size?: 'small' | 'middle' | 'large'
  /** 停止请求成功后的回调（可选；通常无需手刷——引擎回 idle 会推 agent_status
   *  WS 事件，useBusEvent 自动更新状态，按钮随 status!=executing 消失）。 */
  onStopped?: () => void
}

/**
 * PL-11 停止任务按钮：调 POST /api/tasks/{id}/stop。
 *
 * 后端双保险——executing 的 cancel _worker_task（引擎回 idle），queued 的打 cancelled
 * 标记（_handle_task 跳过执行）；都不是则 200 no-op（可能已完成，非错误）。响应 message
 * 是后端给的中文可读文案，直接 toast。停止后引擎 _reset_idle 会推 agent_status(idle) WS
 * 事件，useBusEvent 自动更新 agentStatuses，本按钮随 status!=executing 自然消失，无需
 * 调用方手动刷新。
 *
 * Popconfirm 二次确认：停止是中断执行的中等风险操作，确认避免误点（铁律4 低风险直接执行、
 * 中风险确认——停止一个正在跑的 LLM 任务会浪费已消耗 token，属中风险，故加确认）。
 */
export default function StopTaskButton({
  taskId,
  groupId,
  agentName,
  size = 'small',
  onStopped,
}: StopTaskButtonProps) {
  const [loading, setLoading] = useState(false)

  const handleStop = async () => {
    setLoading(true)
    try {
      const resp = await taskApi.stop(taskId, groupId)
      message.success(resp.message || '已发送停止请求')
      onStopped?.()
    } catch (e) {
      message.error(`停止失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setLoading(false)
    }
  }

  const tooltip = agentName ? `停止 ${agentName} 当前任务` : '停止当前任务'

  return (
    <Popconfirm
      title="确认停止该任务？"
      description="将中断正在执行的 LLM 调用，已消耗的 token 不可恢复。"
      okText="停止"
      okButtonProps={{ danger: true, loading }}
      cancelText="取消"
      onConfirm={handleStop}
      disabled={loading}
    >
      <Tooltip title={tooltip}>
        <Button
          size={size}
          danger
          type="primary"
          icon={<StopOutlined />}
          loading={loading}
          disabled={loading}
        >
          停止
        </Button>
      </Tooltip>
    </Popconfirm>
  )
}
