import { useState } from 'react'
import { Layout as AntLayout, Button, theme } from 'antd'
import { AppstoreOutlined } from '@ant-design/icons'

import ChatPage from '../pages/ChatPage'
import SettingsDrawer from './SettingsDrawer'

const { Header, Content } = AntLayout

/**
 * SH-07 Layout 改造：聊天为默认主页，资源管理降为右上角次级入口。
 *
 * 旧 Layout：Sider 纵向菜单（智能体/群组/任务/监控/技能/MCP/定时 7 项）+ Content 按菜单
 * active 切换渲染对应页——7 个资源页平级并列，聊天藏在「群组」页里。
 *
 * 新 Layout（钉钉/企微风格——对话即工作台）：
 *  - 顶部 Header：左侧品牌标识，右侧「资源管理」按钮 → 拉开 SettingsDrawer（SH-06，7 页 Tab 承载）。
 *  - Content：ChatPage 全屏占满（ChatShell 自带 SessionList 左栏 + ChatPanel 主区，自带新建会话 Modal）。
 *  - 去掉 Sider 纵向菜单与 7 页条件渲染分支——资源页不再平级常驻，按需从抽屉访问。
 *
 * 高度链：`#root{height:100%}`（App.css）→ AntLayout(minHeight:100vh, flex column) → Header(56)
 *   + Content(flex:1, minHeight:0) → ChatShell(height:100%) 填满。Content 加 overflow:hidden
 *   让聊天滚动局限在 Content 内，不撑破外层。
 *
 * 资源入口选「资源管理」按钮 + AppstoreOutlined（资源网格意象），区别于各 Tab 内的图标
 * （Robot/Team/...）与 ChatPanel 头部 SettingOutlined（群信息），避免语义撞车。
 *
 * SettingsDrawer 用非受控模式（open/onClose/defaultActiveKey）——SH-06 设计已支持，SC-09
 * slash 命令受控定位到指定 tab 时再走 activeKey/onActiveKeyChange 受控用法，互不冲突。
 *
 * 注：SH-05（各资源页降级适配，如去掉页内自带 h2 避免与 Tab 标题重复）留该任务处理；
 *      本轮仅做路由接线——7 页直接承载进抽屉 Tab，Tab 标题 + 页内 h2 短期共存可接受。
 */
export default function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false)

  const {
    token: { colorBgContainer },
  } = theme.useToken()

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          height: 56,
          lineHeight: '56px',
          padding: '0 20px',
          background: colorBgContainer,
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexShrink: 0,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 16, color: '#1677ff' }}>
          🤖 Multi-Agent
        </div>
        <Button
          icon={<AppstoreOutlined />}
          onClick={() => setSettingsOpen(true)}
        >
          资源管理
        </Button>
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
        <ChatPage />
      </Content>
      <SettingsDrawer
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        defaultActiveKey="agents"
      />
    </AntLayout>
  )
}
