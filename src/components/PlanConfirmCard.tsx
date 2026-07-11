import { useState } from 'react'
import { Alert, Button, Card, Input, Modal, Select, Tag, message } from 'antd'
import { CheckOutlined, ThunderboltOutlined, EditOutlined } from '@ant-design/icons'
import { planApi, type PlanStep, type PlanModifyStep } from '../services/api'

interface PlanConfirmCardProps {
  groupId: string
  /** 来自 useBusEvent 的 coordinator_plan 事件（由父组件传入，避免重复订阅 WS）。 */
  plan: PlanStep[]
}

/** 计划步骤状态 → 徽标（与 LeaderPanel 对齐，保持视觉一致） */
function stepBadge(status: string): { color: string; label: string } {
  switch (status) {
    case 'completed': return { color: 'green', label: '已完成' }
    case 'dispatched': return { color: 'blue', label: '已派发' }
    case 'failed': return { color: 'red', label: '失败' }
    case 'pending':
    default: return { color: 'default', label: '待执行' }
  }
}

/** 修改弹窗中单步的可编辑值（深拷贝自 plan，编辑不影响父组件状态）。 */
interface EditStep {
  step: number
  agent_id: string
  agent_name: string
  instruction: string
  depends_on: number[]
}

/**
 * M12-PL02 计划确认卡片：展示协调者拆解的计划步骤 + 三动作按钮。
 *
 * - 确认继续：planApi.confirm —— 唤醒驻留计划按原样 fan-out（方案 B 引擎内存态等待）。
 * - 直接干：planApi.directRun —— 把 group config.auto_confirm 置 True（后续计划免确认）+ 恢复当前驻留计划。
 * - 修改：弹出编辑窗，按 step 号 patch 指令/依赖后 planApi.modify —— 后端复位被改步为 pending 并重广播 + 确认派发。
 *
 * 计划数据由父组件（GroupPage）通过 useBusEvent 订阅 coordinator_plan 事件后传入，
 * 卡片本身不重复订阅 WS，仅负责展示与触发动作；动作完成后 WS 会推送新的
 * coordinator_plan / task_dispatch 事件，父组件 plan 状态自动刷新，卡片随之重渲染。
 */
export default function PlanConfirmCard({ groupId, plan }: PlanConfirmCardProps) {
  const [confirming, setConfirming] = useState(false)
  const [directing, setDirecting] = useState(false)
  const [modifyOpen, setModifyOpen] = useState(false)
  const [modifying, setModifying] = useState(false)
  const [editSteps, setEditSteps] = useState<EditStep[]>([])

  if (!plan || plan.length === 0) return null

  const pendingCount = plan.filter((s) => s.status === 'pending').length
  const hasPending = pendingCount > 0
  const busy = confirming || directing || modifying

  const handleConfirm = async () => {
    setConfirming(true)
    try {
      await planApi.confirm(groupId)
      message.success('已确认，计划开始派发')
    } catch (err) {
      message.error(`确认失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setConfirming(false)
    }
  }

  const handleDirectRun = async () => {
    setDirecting(true)
    try {
      const res = await planApi.directRun(groupId)
      message.success(
        res.resumed_resident_plan
          ? '已切换为直接干模式并恢复派发'
          : '已切换为直接干模式（后续计划自动派发）',
      )
    } catch (err) {
      message.error(`切换失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setDirecting(false)
    }
  }

  const openModify = () => {
    // 深拷贝当前 plan 作为编辑初值，编辑期间不污染父组件的 plan 状态
    setEditSteps(
      plan.map((s) => ({
        step: s.step,
        agent_id: s.agent_id,
        agent_name: s.agent_name,
        instruction: s.instruction,
        depends_on: [...(s.depends_on ?? [])],
      })),
    )
    setModifyOpen(true)
  }

  const handleModifySubmit = async () => {
    // 校验：指令不能为空
    const emptyIdx = editSteps.findIndex((s) => !s.instruction.trim())
    if (emptyIdx !== -1) {
      message.warning(`步骤 ${editSteps[emptyIdx].step} 的指令不能为空`)
      return
    }
    // 依赖校验：不能依赖自身 / 依赖的步骤必须存在
    const stepNums = new Set(editSteps.map((s) => s.step))
    for (const s of editSteps) {
      for (const dep of s.depends_on) {
        if (dep === s.step) {
          message.warning(`步骤 ${s.step} 不能依赖自身`)
          return
        }
        if (!stepNums.has(dep)) {
          message.warning(`步骤 ${s.step} 依赖了不存在的步骤 ${dep}`)
          return
        }
      }
    }

    setModifying(true)
    try {
      const steps: PlanModifyStep[] = editSteps.map((s) => ({
        step: s.step,
        agent_id: s.agent_id,
        agent_name: s.agent_name,
        instruction: s.instruction.trim(),
        depends_on: s.depends_on,
      }))
      await planApi.modify(groupId, steps)
      message.success('计划已修改并派发')
      setModifyOpen(false)
    } catch (err) {
      message.error(`修改失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setModifying(false)
    }
  }

  const patchEditStep = (idx: number, patch: Partial<EditStep>) => {
    setEditSteps((prev) => prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)))
  }

  return (
    <Card
      size="small"
      style={{ marginBottom: 12, borderColor: '#d3adf7' }}
      title={
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Tag color="purple" style={{ margin: 0 }}>协调者计划</Tag>
          <span style={{ fontSize: 13, color: '#666' }}>
            共 {plan.length} 步 · 待执行 {pendingCount}
          </span>
        </span>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="请确认是否按此计划执行"
        description="确认继续 → 按原计划派发；直接干 → 本群后续计划自动派发不再确认；修改 → 编辑步骤后派发。"
      />

      {/* 步骤列表 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
        {plan.map((step) => {
          const badge = stepBadge(step.status)
          return (
            <div
              key={`pc-step-${step.step}`}
              style={{
                padding: '8px 12px',
                background: step.status === 'pending' ? '#fff7e6' : '#fafafa',
                borderRadius: 4,
                border: '1px solid #f0f0f0',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ fontWeight: 600, color: '#722ed1' }}>步骤 {step.step}</span>
                <Tag color={badge.color}>{badge.label}</Tag>
                <span style={{ fontSize: 12, color: '#666' }}>
                  {step.agent_name || step.agent_id}
                </span>
              </div>
              <div style={{ fontSize: 13, color: '#333', whiteSpace: 'pre-wrap' }}>
                {step.instruction}
              </div>
              {step.depends_on && step.depends_on.length > 0 && (
                <div style={{ fontSize: 11, color: '#999', marginTop: 4 }}>
                  依赖: 步骤 {step.depends_on.join(', ')}
                </div>
              )}
              {step.result && (
                <div style={{ fontSize: 12, color: '#666', marginTop: 4, whiteSpace: 'pre-wrap' }}>
                  结果: {step.result}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* 操作按钮：确认/直接干需有待执行步骤；三者互斥（任一进行中禁用其余）。 */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <Button
          type="primary"
          icon={<CheckOutlined />}
          loading={confirming}
          disabled={!hasPending || (busy && !confirming)}
          onClick={handleConfirm}
        >
          确认继续
        </Button>
        <Button
          icon={<ThunderboltOutlined />}
          loading={directing}
          disabled={!hasPending || (busy && !directing)}
          onClick={handleDirectRun}
        >
          直接干
        </Button>
        <Button
          icon={<EditOutlined />}
          disabled={plan.length === 0 || (busy && !modifying)}
          onClick={openModify}
        >
          修改
        </Button>
      </div>

      {/* 修改弹窗 */}
      <Modal
        open={modifyOpen}
        title="修改执行计划"
        width={620}
        onCancel={() => setModifyOpen(false)}
        onOk={handleModifySubmit}
        confirmLoading={modifying}
        okText="保存并派发"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxHeight: '60vh', overflowY: 'auto', paddingRight: 4 }}>
          {editSteps.map((step, idx) => (
            <div
              key={`mod-${step.step}`}
              style={{
                padding: '12px',
                background: '#fafafa',
                borderRadius: 6,
                border: '1px solid #f0f0f0',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontWeight: 600, color: '#722ed1' }}>步骤 {step.step}</span>
                <Tag>{step.agent_name || step.agent_id}</Tag>
              </div>
              <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>指令</div>
                <Input.TextArea
                  rows={2}
                  value={step.instruction}
                  onChange={(e) => patchEditStep(idx, { instruction: e.target.value })}
                />
              </div>
              <div>
                <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>依赖步骤</div>
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder="无依赖（可并行）"
                  value={step.depends_on}
                  onChange={(vals: number[]) => patchEditStep(idx, { depends_on: vals })}
                  options={editSteps
                    .filter((s) => s.step !== step.step)
                    .map((s) => ({ value: s.step, label: `步骤 ${s.step}` }))}
                />
              </div>
            </div>
          ))}
        </div>
      </Modal>
    </Card>
  )
}
