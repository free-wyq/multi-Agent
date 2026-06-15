import { useEffect, useState } from 'react'
import {
  Card,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Tag,
  Space,
  message,
  Empty,
  Popconfirm,
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined } from '@ant-design/icons'
import { agentApi, type AgentDefinition } from '../services/api'

const ROLES = [
  '后端开发工程师',
  '前端开发工程师',
  '测试工程师',
  'DevOps 工程师',
  '产品经理',
  '自定义',
]

export default function AgentPage() {
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<AgentDefinition | null>(null)
  const [form] = Form.useForm()

  const fetchAgents = async () => {
    setLoading(true)
    try {
      const data = await agentApi.list()
      setAgents(data)
    } catch {
      message.error('获取智能体列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAgents()
  }, [])

  const handleCreateOrUpdate = async (values: Record<string, unknown>) => {
    try {
      if (editing) {
        const payload = values as Parameters<typeof agentApi.update>[1]
        await agentApi.update(editing.id, payload)
        message.success('更新成功')
      } else {
        const payload = values as unknown as Parameters<typeof agentApi.create>[0]
        await agentApi.create(payload)
        message.success('创建成功')
      }
      setModalOpen(false)
      setEditing(null)
      form.resetFields()
      fetchAgents()
    } catch {
      message.error('操作失败')
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await agentApi.delete(id)
      message.success('删除成功')
      fetchAgents()
    } catch {
      message.error('删除失败')
    }
  }

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  const openEdit = (agent: AgentDefinition) => {
    setEditing(agent)
    form.setFieldsValue({
      name: agent.name,
      role: agent.role,
      extra_skills: agent.extra_skills ?? [],
      system_prompt: agent.system_prompt,
    })
    setModalOpen(true)
  }

  const roleValue = Form.useWatch('role', form)

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>智能体管理</h2>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建智能体
        </Button>
      </div>

      {agents.length === 0 && !loading ? (
        <Empty description="暂无智能体" />
      ) : (
        <Space wrap>
          {agents.map((agent) => (
            <Card
              key={agent.id}
              title={agent.name}
              style={{ width: 320 }}
              loading={loading}
              actions={[
                <Button
                  key="edit"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={() => openEdit(agent)}
                >
                  编辑
                </Button>,
                <Popconfirm
                  key="del"
                  title="确认删除？"
                  onConfirm={() => handleDelete(agent.id)}
                >
                  <Button type="text" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>,
              ]}
            >
              <p>
                <strong>角色：</strong>
                {agent.role}
              </p>
              <p>
                <strong>技能：</strong>
                {agent.extra_skills && agent.extra_skills.length > 0 ? (
                  agent.extra_skills.map((s) => <Tag key={s}>{s}</Tag>)
                ) : (
                  <span style={{ color: '#999' }}>无</span>
                )}
              </p>
            </Card>
          ))}
        </Space>
      )}

      <Modal
        open={modalOpen}
        title={editing ? '编辑智能体' : '新建智能体'}
        onCancel={() => {
          setModalOpen(false)
          setEditing(null)
          form.resetFields()
        }}
        onOk={() => form.submit()}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={handleCreateOrUpdate}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
          >
            <Input placeholder="如：前端开发小新" />
          </Form.Item>
          <Form.Item
            name="role"
            label="角色"
            rules={[{ required: true, message: '请选择角色' }]}
          >
            <Select placeholder="选择角色" options={ROLES.map((r) => ({ value: r, label: r }))} />
          </Form.Item>
          {roleValue === '自定义' && (
            <Form.Item name="system_prompt" label="角色描述">
              <Input.TextArea rows={3} placeholder="输入自定义角色描述（将作为 system prompt）" />
            </Form.Item>
          )}
          <Form.Item name="extra_skills" label="额外技能">
            <Select
              mode="tags"
              placeholder="输入技能名称后回车"
              tokenSeparators={[',']}
              allowClear
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
