import { useEffect, useState } from 'react'
import { Button, Form, Input, Modal, Select, message } from 'antd'
import { BulbOutlined } from '@ant-design/icons'
import {
  agentApi,
  groupApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
} from '../services/api'
import { useBusEventContext } from '../contexts/BusEventContext'
import ChatShell from '../components/ChatShell'
import ChatPanel from '../components/ChatPanel'

/**
 * SH-03 ChatPage：聊天主页（SH-07 后为默认主页）。
 *
 * 组合 SH 系列组件：
 *  - `<ChatShell groups loading onNewSession>`：会话壳布局（SessionList + 主区）。
 *  - `<ChatPanel group agents members>`：作为 ChatShell 主区 children，消息流+输入框+计划卡+停止按钮。
 *  - 新建群组 Modal：onNewSession 入口接线（SessionList 顶部「新建会话」→ 打开 Modal）。
 *
 * 数据加载：groupApi.list + agentApi.list + 当前群成员 groupApi.listMembers。
 * 切群走 BusEventContext.setGroupId（全局聚焦，WS 共享）——SessionList/ChatPanel 共用同一 groupId。
 *
 * 群信息抽屉、群设置 Modal、添加成员 Modal 等管理类 UI 由本页持有（与 GroupPage 并行存在，
 * SH-05 群组页降级后可统一收敛）。本任务范围仅「组合」——接通 ChatShell+ChatPanel+数据流，
 * 复用 GroupPage 验证过的 handleCreate 新建群组逻辑，最小可运行。
 *
 * 当前 Layout 仍用 GroupPage 作「群组」入口（SH-07 才切默认主页到 ChatPage），ChatPage 暂未被
 * 挂载——但独立编译通过 + 数据流自洽，SH-07 接入 Layout 后即可生效。
 */
export default function ChatPage() {
  const { groupId: chatGroupId, setGroupId: setChatGroupId } = useBusEventContext()

  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [members, setMembers] = useState<GroupMember[]>([])
  const [loading, setLoading] = useState(false)
  const [membersLoading, setMembersLoading] = useState(false)

  // 新建群组 Modal
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm] = Form.useForm()
  const [genNameLoading, setGenNameLoading] = useState(false)

  // MT-04: 自动生成团队名称/描述的 loading 态（LLM 调用耗时数秒，禁用按钮防重复点击）
  // 已在原 GroupPage 验证，ChatPage 复用同一交互。

  const chatGroup = groups.find((g) => g.id === chatGroupId) ?? null

  // 拉群组 + 智能体列表
  const fetchData = async () => {
    setLoading(true)
    try {
      const [gData, aData] = await Promise.all([groupApi.list(), agentApi.list()])
      setGroups(gData)
      setAgents(aData)
      // 首次加载未选群时默认选第一个，驱动 ChatPanel 加载消息。
      if (!chatGroupId && gData.length > 0) {
        setChatGroupId(gData[0].id)
      }
    } catch {
      message.error('获取数据失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 切群时加载成员（消息由 ChatPanel 自管）
  useEffect(() => {
    if (!chatGroupId) {
      setMembers([])
      return
    }
    setMembersLoading(true)
    groupApi
      .listMembers(chatGroupId)
      .then(setMembers)
      .catch(() => setMembers([]))
      .finally(() => setMembersLoading(false))
  }, [chatGroupId])

  // 新建群组（复用 GroupPage 验证过的 handleCreate）
  const handleCreate = async (values: Record<string, unknown>) => {
    try {
      const group = await groupApi.create({
        name: values.name as string,
        coordinator_id: values.coordinator_id as string | undefined,
        description: values.description as string | undefined,
      })
      const selected: string[] = (values.members as string[]) ?? []
      await Promise.all(
        selected.map((agentId) => groupApi.addMember(group.id, agentId)),
      )
      message.success('创建成功')
      setCreateOpen(false)
      createForm.resetFields()
      // 重新拉群组列表并切到新群
      const gData = await groupApi.list()
      setGroups(gData)
      setChatGroupId(group.id)
    } catch {
      message.error('创建失败')
    }
  }

  // MT-04: 自动生成团队名称和描述
  const handleGenerateNameDesc = async () => {
    const values = createForm.getFieldsValue(true)
    const coordinatorId = values.coordinator_id as string | undefined
    const memberIds = (values.members as string[]) ?? []
    if (!coordinatorId && memberIds.length === 0) {
      message.warning('请先选择群主或成员，再自动生成')
      return
    }
    setGenNameLoading(true)
    try {
      const result = await groupApi.generateNameDesc(coordinatorId, memberIds)
      createForm.setFieldsValue({
        name: result.name,
        description: result.description,
      })
      message.success('已生成团队名称和描述，可按需修改')
    } catch (e) {
      message.error(e instanceof Error ? e.message : '生成失败')
    } finally {
      setGenNameLoading(false)
    }
  }

  return (
    <>
      <ChatShell
        groups={groups}
        loading={loading}
        onNewSession={() => setCreateOpen(true)}
      >
        <ChatPanel
          group={chatGroup}
          agents={agents}
          members={members}
          loading={membersLoading}
        />
      </ChatShell>

      {/* 新建群组 Modal（onNewSession 入口） */}
      <Modal
        open={createOpen}
        title="新建会话"
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={() => createForm.submit()}
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate}>
          {/* MT-04: 自动生成团队名称和描述 */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
            <Button
              type="link"
              icon={<BulbOutlined />}
              loading={genNameLoading}
              onClick={handleGenerateNameDesc}
              style={{ padding: '0 0' }}
            >
              自动生成名称和描述
            </Button>
          </div>
          <Form.Item
            name="name"
            label="会话名称"
            rules={[{ required: true, message: '请输入会话名称' }]}
          >
            <Input placeholder="如：商城订单项目" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} placeholder="会话的用途描述（可选）" />
          </Form.Item>
          <Form.Item
            name="coordinator_id"
            label="群主"
            rules={[{ required: true, message: '请选择群主' }]}
          >
            <Select
              placeholder="选择群主智能体（必选）"
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item name="members" label="成员">
            <Select
              mode="multiple"
              placeholder="选择群组成员"
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
