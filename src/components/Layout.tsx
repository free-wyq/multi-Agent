import { useState } from 'react'
import { Avatar, Segmented, Tooltip } from 'antd'
import {
  MessageOutlined,
  AppstoreOutlined,
  ShopOutlined,
  UserOutlined,
} from '@ant-design/icons'

import Sidebar from './Sidebar'
import ChatView from './ChatView'
import SettingsModal, { type NavKey } from './SettingsModal'
import AgentPage from '../pages/AgentPage'
import SkillPage from '../pages/SkillPage'

/** 品牌蓝（与 Sidebar 一致）。 */
const BRAND = '#0A5ACF'

type View = 'chat' | 'agent' | 'skill'

/**
 * Layout — 应用根布局（顶部栏 + 左右两栏）。
 *
 * 布局演进 2026-07-12：在原「Sidebar + ChatView」之上加全局顶部栏，承载品牌 +
 * 三视图切换（对话 / 智能体广场 / skill广场）+ 用户头像入口。主内容区按 activeView 在
 * ChatView / AgentPage / SkillPage 间切换——后两者直接复用全屏路由页组件，它们自带
 * 数据拉取与 height:100%+overflowY:auto，无需额外适配。
 *
 * 右上角原「⚙ 设置」改为用户头像（后续接登录）。点击头像打开 SettingsModal 并默认定位到
 * 「用户信息」项（initialKey='user'）；SettingsModal 左侧导航仍含 MCP/技能/记忆/模型服务商/
 * 用户信息 五项，故头像入口 ≈ 进入设置的快捷路径 + 默认落在用户信息页。
 *
 * 侧栏列表项点击（selectAgent/selectGroup）时经 onNavigateChat 自动切回对话视图，
 * 保证「在广场页点侧栏某个智能体 → 立即进入与它的单聊」直觉化。
 *
 * 高度链：#root{height:100%}（App.css）→ 本 flex column 容器 height:100% →
 * 顶栏 flexShrink:0(h48) + 下方 flex:1 minHeight:0 行（Sidebar + 主区）。顶栏不动
 * 现有侧栏/聊天的高度链，仅在外层多套一层 column。
 */
export default function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false)
  /** 打开设置弹窗时默认聚焦的导航项：头像入口='user'，其余默认 'mcp'。 */
  const [settingsInitialKey, setSettingsInitialKey] = useState<NavKey>('user')
  const [view, setView] = useState<View>('chat')

  // 头像入口：打开弹窗并默认定位到「用户信息」。
  const openUserSettings = () => {
    setSettingsInitialKey('user')
    setSettingsOpen(true)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* 顶部栏：品牌 + 视图切换 + 用户头像。
          三段等分（左/右各 flex:1）让中间 Segmented 真正居中，不随品牌/头像宽度偏移。
          白底浮起 + 底部投影，与主区灰底拉开层次（见 App.css --shadow-topbar）。 */}
      <div
        style={{
          height: 48,
          flexShrink: 0,
          padding: '0 16px',
          borderBottom: `1px solid var(--border-soft)`,
          display: 'flex',
          alignItems: 'center',
          background: 'var(--surface-raised)',
          boxShadow: 'var(--shadow-topbar)',
          position: 'relative',
          zIndex: 2,
        }}
      >
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 18 }}>🤖</span>
          <span style={{ fontWeight: 700, fontSize: 15, color: BRAND }}>MA</span>
        </div>
        <Segmented
          value={view}
          onChange={(val) => setView(val as View)}
          options={[
            { value: 'chat', label: <ViewLabel icon={<MessageOutlined />} text="对话" /> },
            { value: 'agent', label: <ViewLabel icon={<AppstoreOutlined />} text="智能体广场" /> },
            { value: 'skill', label: <ViewLabel icon={<ShopOutlined />} text="skill广场" /> },
          ]}
        />
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          <Tooltip title="用户信息">
            <Avatar
              size={30}
              icon={<UserOutlined />}
              style={{ cursor: 'pointer', background: BRAND }}
              onClick={openUserSettings}
            />
          </Tooltip>
        </div>
      </div>

      {/* 主区：侧栏 + 视图内容。
          主区底色 --surface-main（次冷灰），比顶栏/侧栏/卡片白底后退一层，
          让浮起面（侧栏、广场页卡片、聊天气泡）的边界清晰可辨。
          注意：本层必须 display:flex——ChatView 用 flex:1 撑高度，若退回 block，
          flex:1 失效 → 高度塌 0 → 对话框与消息滚动一并消失（曾踩坑）。 */}
      <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', overflow: 'hidden', background: 'var(--surface-main)' }}>
        <Sidebar onNavigateChat={() => setView('chat')} />
        <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', overflow: 'hidden' }}>
          {view === 'chat' && <ChatView />}
          {view === 'agent' && <AgentPage />}
          {view === 'skill' && <SkillPage />}
        </div>
      </div>

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialKey={settingsInitialKey}
      />
    </div>
  )
}

/** Segmented 选项标签：图标 + 文案。 */
function ViewLabel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      {icon}
      {text}
    </span>
  )
}
