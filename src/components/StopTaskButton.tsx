import { useState } from 'react'
import { Button, Popconfirm, Tooltip, message } from 'antd'
import { StopOutlined } from '@ant-design/icons'
import { groupApi } from '../services/api'

interface StopTaskButtonProps {
  /** 回合所属群组 id——task-26 起停止走群图整回合硬停 groupApi.stopTurn(groupId)，
   *  不再依赖具体 task_id（去中心化回合 worker/协调者发言本就无 task_id，旧 taskApi.stop
   *  打不中）。所有调用点已改为只传 groupId。 */
  groupId: string
  /** 可选：智能体名，用于 toast 文案与 tooltip。 */
  agentName?: string
  /** 按钮尺寸，默认 small（监控面板紧凑场景）。 */
  size?: 'small' | 'middle' | 'large'
  /** 停止请求成功后的回调（可选；通常无需手刷——cancel_turn 后 stop-turn 端点
   *  会 emit agent_status(idle)，useBusEvent 自动更新状态，按钮随 status!=executing 消失）。 */
  onStopped?: () => void
}

/**
 * PL-11 / task-26 停止按钮：调 POST /api/groups/{id}/stop-turn。
 *
 * task-26 起从 per-task `taskApi.stop(taskId, groupId)` 改为群图整回合硬停
 * `groupApi.stopTurn(groupId)`——去中心化 swarm 回合（闲聊/@人/成语接龙）发言
 * 人本就无 task_id（不经驻留引擎 executing 状态机），旧 taskApi.stop 打不中这类
 * 回合，停止按钮形同虚设。stopTurn 走 GroupRuntime.cancel_turn 双层停（协作式
 * _stop_event.set + 硬切 _current_task.cancel），回合并发态都停得下来。返回 message
 * 是后端给的中文可读文案，直接 toast；cancelled 区分真停了 vs 无活跃回合 no-op。
 *
 * 停止后 stop-turn 端点 emit agent_status(idle)（cancel 分支自身不 emit），useBusEvent
 * 自动更新 agentStatuses，本按钮随 status!=executing 自然消失，无需调用方手刷。
 *
 * Popconfirm 二次确认：停止是中断执行的中等风险操作，确认避免误点（铁律4 低风险直接执行、
 * 中风险确认——停止一个正在跑的 LLM 任务会浪费已消耗 token，属中风险，故加确认）。
 */
export default function StopTaskButton({
  groupId,
  agentName,
  size = 'small',
  onStopped,
}: StopTaskButtonProps) {
  const [loading, setLoading] = useState(false)

  const handleStop = async () => {
    setLoading(true)
    try {
      const resp = await groupApi.stopTurn(groupId)
      // 保留 toast 的 message 字段契约（task-26 要求）：后端给什么文案就 toast 什么，
      // cancelled 区分真停/无活跃 no-op 仅记日志，文案本身已足够人读（「已停止…」/「无活跃回合」）。
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
