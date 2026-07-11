import { useState } from 'react'
import {
  Layout as AntLayout,
  Menu,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  RobotOutlined,
  TeamOutlined,
  DashboardOutlined,
  MessageOutlined,
  AppstoreAddOutlined,
  ApiOutlined,
  ScheduleOutlined,
} from '@ant-design/icons'

import AgentPage from '../pages/AgentPage'
import GroupPage from '../pages/GroupPage'
import TaskPage from '../pages/TaskPage'
import MonitorPage from '../pages/MonitorPage'
import SkillPage from '../pages/SkillPage'
import McpPage from '../pages/McpPage'
import SchedulePage from '../pages/SchedulePage'

const { Sider, Content } = AntLayout

type PageKey =
  | 'agents'
  | 'groups'
  | 'tasks'
  | 'monitor'
  | 'skills'
  | 'mcp'
  | 'schedule'

const menuItems: MenuProps['items'] = [
  { key: 'agents', icon: <RobotOutlined />, label: '智能体' },
  { key: 'groups', icon: <TeamOutlined />, label: '群组' },
  { key: 'tasks', icon: <DashboardOutlined />, label: '任务' },
  { key: 'monitor', icon: <MessageOutlined />, label: '监控' },
  { key: 'skills', icon: <AppstoreAddOutlined />, label: '技能市场' },
  { key: 'mcp', icon: <ApiOutlined />, label: 'MCP 工具' },
  { key: 'schedule', icon: <ScheduleOutlined />, label: '定时任务' },
]

export default function Layout() {
  const [active, setActive] = useState<PageKey>('agents')

  const {
    token: { colorBgContainer, borderRadiusLG },
  } = theme.useToken()

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <Sider theme="light" breakpoint="lg" collapsedWidth="0" width={180}>
        <div
          style={{
            height: 64,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontWeight: 700,
            fontSize: 16,
            color: '#1677ff',
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          🤖 Multi-Agent
        </div>
        <Menu
          mode="inline"
          selectedKeys={[active]}
          items={menuItems}
          onClick={({ key }) => setActive(key as PageKey)}
        />
      </Sider>
      <AntLayout>
        <Content
          style={{
            margin: 16,
            padding: 24,
            background: colorBgContainer,
            borderRadius: borderRadiusLG,
            minHeight: 280,
          }}
        >
          {active === 'agents' && <AgentPage />}
          {active === 'groups' && <GroupPage />}
          {active === 'tasks' && <TaskPage />}
          {active === 'monitor' && <MonitorPage />}
          {active === 'skills' && <SkillPage />}
          {active === 'mcp' && <McpPage />}
          {active === 'schedule' && <SchedulePage />}
        </Content>
      </AntLayout>
    </AntLayout>
  )
}
