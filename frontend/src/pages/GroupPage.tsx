import { useEffect, useState } from 'react'
import {
  Card,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Checkbox,
  Space,
  message,
  Empty,
  Popconfirm,
  Divider,
  Tag,
} from 'antd'
import { PlusOutlined, TeamOutlined, SendOutlined } from '@ant-design/icons'
import {
  agentApi,
  groupApi,
  type AgentDefinition,
  type Group,
  type GroupMember,
} from '../services/api'

export default function GroupPage() {
  const [groups, setGroups] = useState<Group[]>([])
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [membersMap, setMembersMap] = useState<Record<string, GroupMember[]>>({})
  const [loading, setLoading] = useState(false)

  const [createOpen, setCreateOpen] = useState(false)
  const [demandOpen, setDemandOpen] = useState(false)
  const [activeGroup, setActiveGroup] = useState<Group | null>(null)
  const [createForm] = Form.useForm()
  const [demandForm] = Form.useForm()

  const fetchData = async () => {
    setLoading(true)
    try {
      const [gData, aData] = await Promise.all([groupApi.list(), agentApi.list()])
      setGroups(gData)
      setAgents(aData)
      const mems: Record<string, GroupMember[]> = {}
      await Promise.all(
        gData.map(async (g) => {
          try {
            const m = await groupApi.listMembers(g.id)
            mems[g.id] = m
          } catch {
            mems[g.id] = []
          }
        }),
      )
      setMembersMap(mems)
    } catch {
      message.error('获取数据失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [])

  const handleCreate = async (values: Record<string, unknown>) => {
    try {
      const group = await groupApi.create({
        name: values.name as string,
        coordinator_agent_id: values.coordinator_agent_id as string | undefined,
      })
      const selected: string[] = (values.members as string[]) ?? []
      await Promise.all(
        selected.map((agentId) => groupApi.addMember(group.id, agentId)),
      )
      message.success('创建成功')
      setCreateOpen(false)
      createForm.resetFields()
      fetchData()
    } catch {
      message.error('创建失败')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await groupApi.delete(id)
      message.success('删除成功')
      fetchData()
    } catch {
      message.error('删除失败')
    }
  }

  const openDemand = (group: Group) => {
    setActiveGroup(group)
    setDemandOpen(true)
    demandForm.resetFields()
  }

  const handleDemandSubmit = async (_values: { demand: string }) => {
    if (!activeGroup) return
    try {
      await groupApi.update(activeGroup.id, { name: activeGroup.name })
      message.success('需求已提交（实际应调用需求/任务创建接口）')
      setDemandOpen(false)
    } catch {
      message.error('提交失败')
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>群组管理</h2>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          新建群组
        </Button>
      </div>

      {groups.length === 0 && !loading ? (
        <Empty description="暂无群组" />
      ) : (
        <Space wrap>
          {groups.map((group) => {
            const members = membersMap[group.id] ?? []
            return (
              <Card
                key={group.id}
                title={group.name}
                style={{ width: 340 }}
                loading={loading}
              >
                <p>
                  <strong>群主：</strong>
                  {agents.find((a) => a.id === group.coordinator_agent_id)?.name ?? '未设置'}
                </p>
                <Divider style={{ margin: '8px 0' }} />
                <Space wrap>
                  {members.length === 0 ? (
                    <span style={{ color: '#999' }}>暂无成员</span>
                  ) : (
                    members.map((m) => {
                      const agent = agents.find((a) => a.id === m.agent_id)
                      return (
                        <Tag key={m.id} icon={<TeamOutlined />}>
                          {agent?.name ?? m.agent_id}
                          {m.alias ? ` (${m.alias})` : ''}
                        </Tag>
                      )
                    })
                  )}
                </Space>
                <div style={{ marginTop: 12, textAlign: 'right' }}>
                  <Space>
                    <Button
                      size="small"
                      icon={<SendOutlined />}
                      onClick={() => openDemand(group)}
                    >
                      提交需求
                    </Button>
                    <Popconfirm
                      title="确认删除群组？"
                      onConfirm={() => handleDelete(group.id)}
                    >
                      <Button size="small" danger>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
              </Card>
            )
          })}
        </Space>
      )}

      {/* 新建群组 */}
      <Modal
        open={createOpen}
        title="新建群组"
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={() => createForm.submit()}
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="name"
            label="群组名称"
            rules={[{ required: true, message: '请输入群组名称' }]}
          >
            <Input placeholder="如：商城订单项目" />
          </Form.Item>
          <Form.Item name="coordinator_agent_id" label="群主">
            <Select
              placeholder="选择群主智能体（可选）"
              allowClear
              options={agents.map((a) => ({ value: a.id, label: a.name }))}
            />
          </Form.Item>
          <Form.Item name="members" label="成员">
            <Checkbox.Group options={agents.map((a) => ({ label: a.name, value: a.id }))} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 提交需求 */}
      <Modal
        open={demandOpen}
        title={`提交需求 —— ${activeGroup?.name}`}
        onCancel={() => {
          setDemandOpen(false)
          demandForm.resetFields()
        }}
        onOk={() => demandForm.submit()}
        destroyOnClose
      >
        <Form form={demandForm} layout="vertical" onFinish={handleDemandSubmit}>
          <Form.Item
            name="demand"
            label="需求描述"
            rules={[{ required: true, message: '请输入需求' }]}
          >
            <Input.TextArea
              rows={6}
              placeholder="描述你希望这个群组完成的工作..."
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
