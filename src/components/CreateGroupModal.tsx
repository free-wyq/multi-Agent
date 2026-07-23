import { useState } from 'react'
import { Form, Input, Modal, Segmented, Select, message } from 'antd'
import { groupApi, type AgentDefinition } from '../services/api'

/**
 * 协作模式选项（Segmented 用）。
 *
 * - centralized（中心化，默认）：群主主导，supervisor 子图拆计划派工。
 * - decentralized（去中心化）：纯 swarm，群里无群主概念，裸消息群主当首发
 *   （对标 LangGraph create_swarm 的 default_active_agent），@群主合法 handoff。
 *
 * value 是后端 config.collaboration_mode 写入值；label 是 UI 显示文字。
 */
const MODE_OPTIONS = [
  { label: '中心化', value: 'centralized' },
  { label: '去中心化', value: 'decentralized' },
]

interface CreateGroupModalProps {
  /** Modal 开关（由 Sidebar 持有，新建群组按钮触发）。 */
  open: boolean
  /** 关闭回调（取消/创建后）。 */
  onClose: () => void
  /** 全部智能体（群主候选 + 成员候选）。由 Sidebar 透传（SelectionContext 集中持有）。 */
  agents: AgentDefinition[]
  /** 创建成功后通知父刷新（SelectionContext.refreshAll）。 */
  onCreated?: () => void
}

/**
 * CreateGroupModal — 新建群组 Modal（连带建，2026-07-23）。
 *
 * Sidebar「新建群组」按钮此前只 toast「待接入」，本次连带把建群 Modal 一起建。
 * 字段：群名 Input（可点「自动生成」回填）+ 群主 Select + 成员 Select mode="multiple"
 * + 协作模式 Segmented（默认 centralized）。提交调 groupApi.create({name, coordinator_id,
 * member_ids, config: {collaboration_mode}})。
 *
 * 协作模式 Segmented 是本次改造的核心字段——去中心化模式下 coordinator 纳入 members
 * 建 agent 节点（裸消息群主当首发），中心化排除（supervisor 子图）。切换由后端
 * recompile_group 触发图重编译（做法 A 图级二选一）。
 *
 * 不处理单聊（single_chat）——单聊走点选智能体的 find-or-create 单聊群路径，
 * 不经此 Modal。
 */
export default function CreateGroupModal({
  open,
  onClose,
  agents,
  onCreated,
}: CreateGroupModalProps) {
  const [form] = Form.useForm()
  const [creating, setCreating] = useState(false)

  const handleGenerateName = async () => {
    try {
      const values = await form.validateFields(['coordinator_id', 'member_ids'])
      const coordId = (values.coordinator_id as string) || undefined
      const memberIds = (values.member_ids as string[]) || []
      if (!coordId && memberIds.length === 0) {
        message.warning('请先选择群主或成员再生成名称')
        return
      }
      const result = await groupApi.generateNameDesc(coordId, memberIds)
      form.setFieldsValue({
        name: result.name,
        description: result.description,
      })
    } catch {
      /* 校验失败 Form 已标红 */
    }
  }

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      const collaborationMode = (values.collaboration_mode as string) || 'centralized'
      setCreating(true)
      await groupApi.create({
        name: values.name as string,
        coordinator_id: (values.coordinator_id as string) || undefined,
        description: (values.description as string) || undefined,
        member_ids: (values.member_ids as string[]) || [],
        config: { collaboration_mode: collaborationMode },
      })
      message.success('群组已创建')
      form.resetFields()
      onCreated?.()
      onClose()
    } catch {
      message.error('创建群组失败')
    } finally {
      setCreating(false)
    }
  }

  const handleCancel = () => {
    form.resetFields()
    onClose()
  }

  return (
    <Modal
      open={open}
      title="新建群组"
      onCancel={handleCancel}
      onOk={handleCreate}
      confirmLoading={creating}
      destroyOnClose
      okText="创建"
      width={520}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ collaboration_mode: 'centralized' }}
        style={{ marginTop: 12 }}
      >
        <Form.Item
          name="name"
          label="群组名称"
          rules={[{ required: true, message: '请输入群组名称' }]}
        >
          <Input
            placeholder="如：登录重构攻坚组"
            autoComplete="off"
            addonAfter={
              <a
                onClick={handleGenerateName}
                style={{ fontSize: 12, cursor: 'pointer' }}
              >
                自动生成
              </a>
            }
          />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea rows={2} placeholder="一句话描述群组目标" />
        </Form.Item>
        <Form.Item
          name="coordinator_id"
          label="群主"
          rules={[{ required: true, message: '请选择群主' }]}
          tooltip="群主在中心化模式下主导调度（supervisor 子图拆计划派工）；去中心化模式下当裸消息首发（swarm default_active_agent）。"
        >
          <Select
            placeholder="选择群主"
            options={agents.map((a) => ({ value: a.id, label: `${a.name} (${a.role})` }))}
            showSearch
            optionFilterProp="label"
          />
        </Form.Item>
        <Form.Item name="member_ids" label="成员">
          <Select
            mode="multiple"
            placeholder="选择成员（可多选）"
            options={agents.map((a) => ({ value: a.id, label: `${a.name} (${a.role})` }))}
            showSearch
            optionFilterProp="label"
          />
        </Form.Item>
        <Form.Item
          name="collaboration_mode"
          label="协作模式"
          tooltip="中心化：群主主导，supervisor 子图拆计划派工。去中心化：纯 swarm，裸消息群主当首发，@群主合法 handoff。切换后图重编译。"
        >
          <Segmented options={MODE_OPTIONS} />
        </Form.Item>
      </Form>
    </Modal>
  )
}
