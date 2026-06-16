import { useState } from 'react'
import { Card, Button, Modal, Select, message, Space, Tag } from 'antd'
import { PlusOutlined, LinkOutlined } from '@ant-design/icons'

interface Skill {
  id: string
  name: string
  description: string
  mountedTo: string[]
}

const MOCK_SKILLS: Skill[] = [
  {
    id: 'skill-1',
    name: 'Python 开发',
    description: 'Python 项目脚手架、类型注解、单元测试自动生成',
    mountedTo: ['agent-1'],
  },
  {
    id: 'skill-2',
    name: 'React 组件开发',
    description: 'Ant Design + React 组件编写、Storybook 文档生成',
    mountedTo: [],
  },
  {
    id: 'skill-3',
    name: 'API 测试',
    description: '基于 pytest + requests 的接口自动化测试',
    mountedTo: ['agent-2'],
  },
  {
    id: 'skill-4',
    name: 'Docker 部署',
    description: 'Dockerfile 编写、docker-compose 编排',
    mountedTo: [],
  },
  {
    id: 'skill-5',
    name: 'SQL 查询优化',
    description: '慢查询分析、索引建议、SQL 重写',
    mountedTo: ['agent-1', 'agent-2'],
  },
]

const MOCK_AGENTS = [
  { id: 'agent-1', name: '后端开发小新' },
  { id: 'agent-2', name: '前端开发小美' },
  { id: 'agent-3', name: '测试工程师老王' },
]

export default function SkillPage() {
  const [skills, setSkills] = useState<Skill[]>(MOCK_SKILLS)
  const [mountOpen, setMountOpen] = useState(false)
  const [activeSkill, setActiveSkill] = useState<Skill | null>(null)
  const [agentIds, setAgentIds] = useState<string[]>([])

  const openMount = (skill: Skill) => {
    setActiveSkill(skill)
    setAgentIds(skill.mountedTo)
    setMountOpen(true)
  }

  const handleMount = () => {
    if (!activeSkill) return
    setSkills((prev) =>
      prev.map((s) =>
        s.id === activeSkill.id ? { ...s, mountedTo: agentIds } : s,
      ),
    )
    message.success(`已更新「${activeSkill.name}」的挂载关系`)
    setMountOpen(false)
    setActiveSkill(null)
    setAgentIds([])
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>技能市场</h2>
        <Button type="primary" icon={<PlusOutlined />}>
          提交技能
        </Button>
      </div>

      <Space wrap>
        {skills.map((skill) => (
          <Card
            key={skill.id}
            title={skill.name}
            style={{ width: 300 }}
            actions={[
              <Button
                key="mount"
                type="text"
                icon={<LinkOutlined />}
                onClick={() => openMount(skill)}
              >
                挂载
              </Button>,
            ]}
          >
            <p style={{ minHeight: 40 }}>{skill.description}</p>
            <div>
              {skill.mountedTo.length === 0 ? (
                <span style={{ color: '#999', fontSize: 12 }}>未挂载到任何智能体</span>
              ) : (
                <>
                  <div style={{ fontSize: 12, color: '#666', marginBottom: 4 }}>
                    已挂载 ({skill.mountedTo.length}):
                  </div>
                  <Space wrap>
                    {skill.mountedTo.map((id) => (
                      <Tag key={id}>
                        {MOCK_AGENTS.find((a) => a.id === id)?.name ?? id}
                      </Tag>
                    ))}
                  </Space>
                </>
              )}
            </div>
          </Card>
        ))}
      </Space>

      <Modal
        open={mountOpen}
        title={`挂载技能 —— ${activeSkill?.name}`}
        onCancel={() => setMountOpen(false)}
        onOk={handleMount}
      >
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="选择要挂载的智能体"
          value={agentIds}
          onChange={setAgentIds}
          options={MOCK_AGENTS.map((a) => ({ value: a.id, label: a.name }))}
        />
      </Modal>
    </div>
  )
}
