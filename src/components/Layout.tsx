import { Layout as AntLayout, Dropdown, Button, theme } from 'antd'
import {
  RobotOutlined,
  AppstoreOutlined,
  ApiOutlined,
  ScheduleOutlined,
  DashboardOutlined,
  MessageOutlined,
  MenuOutlined,
} from '@ant-design/icons'
import type { MenuProps } from 'antd'
import { Outlet, useNavigate } from 'react-router-dom'

import Statusbar from './Statusbar'

const { Header, Content } = AntLayout

/**
 * Layout — 应用根 layout route（L1-02 路由化 + L1-03 高度链 + L4-05 Header 导航下拉）。
 *
 * 作为 router.tsx 的根 layout route element，常驻不卸载；Content 渲染 <Outlet/> 承载
 * 7 个子路由（/ 聊天主页 + /agents /skills /mcp /schedule /tasks /monitor）。
 * 路由切换只换 Outlet 内容，Layout + Header + Statusbar 不重挂——BusEventProvider
 * 在 App 层（RouterProvider 外），WS 连接跨路由不中断。
 *
 * L4-05：Header 右侧「☰资源」Dropdown 导航下拉，点击跳各全屏路由（agents/skills/mcp/
 * schedule/tasks/monitor）。取代原「资源管理」按钮 + SettingsDrawer 抽屉——配置页 L4-01~
 * 04 全迁路由后，抽屉（7 Tab）已无存在意义，整体退役。slash 命令 /agent /skills /mcp
 * /schedule 仍走聊天流内联速览卡片（只读，不动），与路由页（全功能管理）互补。
 *
 * 高度链（L1-03 定稿，修 TaskPage/聊天滚动随宽度变化崩的根因）：
 *   #root{height:100%}（App.css）→ AntLayout(height:100%, flex column)
 *     → Header(48, flexShrink:0) + Content(flex:1, minHeight:0) + Statusbar(26, flexShrink:0)
 *   关键：AntLayout 从 minHeight:100vh 改 height:100%——minHeight:100vh 会让 Layout 至少占
 *   一屏但允许超出（内容多时撑破、Body 出现滚动条），height:100% 严格锁在父（#root）高度，
 *   配合 Content 的 minHeight:0（flex 子项可缩，不强制内容高度），滚动局限在 Content 内，
 *   Header/Statusbar 恒定高度不被顶出视口。Header 从 56→48 收紧。
 */

/** 资源导航菜单项：跳各全屏路由。 */
const NAV_ITEMS: MenuProps['items'] = [
  { key: '/agents', label: '智能体', icon: <RobotOutlined /> },
  { key: '/skills', label: '技能市场', icon: <AppstoreOutlined /> },
  { key: '/mcp', label: 'MCP 工具', icon: <ApiOutlined /> },
  { key: '/schedule', label: '定时任务', icon: <ScheduleOutlined /> },
  { type: 'divider' },
  { key: '/tasks', label: '任务看板', icon: <DashboardOutlined /> },
  { key: '/monitor', label: '执行监控', icon: <MessageOutlined /> },
]

export default function Layout() {
  const navigate = useNavigate()

  const {
    token: { colorBgContainer },
  } = theme.useToken()

  /** 导航菜单点击 → 跳对应路由（hash 路由，navigate 内部转 #/path）。 */
  const handleNav: MenuProps['onClick'] = ({ key }) => {
    navigate(key)
  }

  return (
    <AntLayout style={{ height: '100%' }}>
      <Header
        style={{
          height: 48,
          lineHeight: '48px',
          padding: '0 20px',
          background: colorBgContainer,
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
        }}
      >
        <div
          style={{
            fontWeight: 700,
            fontSize: 16,
            color: '#1677ff',
            cursor: 'pointer',
          }}
          onClick={() => navigate('/')}
        >
          🤖 Multi-Agent
        </div>
        <Dropdown menu={{ items: NAV_ITEMS, onClick: handleNav }} placement="bottomRight">
          <Button icon={<MenuOutlined />}>资源</Button>
        </Dropdown>
      </Header>
      <Content
        style={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <Outlet />
      </Content>
      <Statusbar />
    </AntLayout>
  )
}
