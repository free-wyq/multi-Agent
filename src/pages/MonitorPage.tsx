import { useState } from 'react'
import { Card, Space, Tag } from 'antd'
import { RobotOutlined, ClockCircleOutlined } from '@ant-design/icons'
import LogPanel from '../components/LogPanel'

interface AgentLogEntry {
  agentId: string
  agentName: string
  status: 'idle' | 'running' | 'waiting'
  waitFor?: string
}

const MOCK_AGENTS: AgentLogEntry[] = [
  { agentId: 'agent-1', agentName: '前端开发', status: 'running' },
  { agentId: 'agent-2', agentName: '后端开发', status: 'running' },
  { agentId: 'agent-3', agentName: '测试工程师', status: 'waiting', waitFor: '前端开发和后端开发' },
  { agentId: 'agent-4', agentName: 'DevOps', status: 'idle' },
]

export default function MonitorPage() {
  const [agents] = useState<AgentLogEntry[]>(MOCK_AGENTS)

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>对话监控</h2>
      </div>

      <Space wrap align="start">
        {agents.map((agent) => (
          <Card
            key={agent.agentId}
            title={
              <Space>
                <RobotOutlined />
                {agent.agentName}
                <Tag
                  color={
                    agent.status === 'running'
                      ? 'blue'
                      : agent.status === 'waiting'
                      ? 'orange'
                      : 'default'
                  }
                >
                  {agent.status === 'running' && '🟢 执行中'}
                  {agent.status === 'waiting' && '⬜ 等待中'}
                  {agent.status === 'idle' && '⚪ 空闲'}
                </Tag>
              </Space>
            }
            style={{ width: 380 }}
            bodyStyle={{ padding: 0 }}
          >
            {agent.status === 'waiting' && agent.waitFor ? (
              <div
                style={{
                  padding: 16,
                  color: '#666',
                  fontSize: 13,
                  textAlign: 'center',
                }}
              >
                <ClockCircleOutlined /> 等待 {agent.waitFor} 完成...
              </div>
            ) : agent.status === 'idle' ? (
              <div
                style={{
                  padding: 16,
                  color: '#999',
                  fontSize: 13,
                  textAlign: 'center',
                }}
              >
                暂无执行中的任务
              </div>
            ) : (
              <LogPanel agentId={agent.agentId} />
            )}
          </Card>
        ))}
      </Space>
    </div>
  )
}
