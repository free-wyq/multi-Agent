import { useState } from 'react'
import { Button, Segmented, Tooltip } from 'antd'
import {
  MessageOutlined,
  AppstoreOutlined,
  ShopOutlined,
  SettingOutlined,
} from '@ant-design/icons'

import Sidebar from './Sidebar'
import ChatView from './ChatView'
import SettingsModal from './SettingsModal'
import AgentPage from '../pages/AgentPage'
import SkillPage from '../pages/SkillPage'

/** 品牌蓝（与 Sidebar 一致）。 */
const BRAND = '#0A5ACF'

type View = 'chat' | 'agent' | 'skill'

/**
 * Layout — 应用根布局（顶部栏 + 左右两栏）。
 *
 * 布局演进 2026-07-12：在原「Sidebar + ChatView」之上加全局顶部栏，承载品牌 +
 * 三视图切换（聊天 / 智能体广场 / skill广场）+ 设置入口。主内容区按 activeView 在
 * ChatView / AgentPage / SkillPage 间切换——后两者直接复用全屏路由页组件，它们自带
 * 数据拉取与 height:100%+overflowY:auto，无需额外适配。
 *
 * 侧栏列表项点击（selectAgent/selectGroup）时经 onNavigateChat 自动切回聊天视图，
 * 保证「在广场页点侧栏某个智能体 → 立即进入与它的单聊」直觉化。
 *
 * 高度链：#root{height:100%}（App.css）→ 本 flex column 容器 height:100% →
 * 顶栏 flexShrink:0(h48) + 下方 flex:1 minHeight:0 行（Sidebar + 主区）。顶栏不动
 * 现有侧栏/聊天的高度链，仅在外层多套一层 column。
 */
export default function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [view, setView] = useState<View>('chat')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* 顶部栏：品牌 + 视图切换 + 设置。
          三段等分（左/右各 flex:1）让中间 Segmented 真正居中，不随品牌/设置宽度偏移。 */}
      <div
        style={{
          height: 48,
          flexShrink: 0,
          padding: '0 16px',
          borderBottom: '1px solid #ececec',
          display: 'flex',
          alignItems: 'center',
          background: '#fff',
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
            { value: 'chat', label: <ViewLabel icon={<MessageOutlined />} text="聊天" /> },
            { value: 'agent', label: <ViewLabel icon={<AppstoreOutlined />} text="智能体广场" /> },
            { value: 'skill', label: <ViewLabel icon={<ShopOutlined />} text="skill广场" /> },
          ]}
        />
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          <Tooltip title="设置">
            <Button type="text" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)} />
          </Tooltip>
        </div>
      </div>

      {/* 主区：侧栏 + 视图内容 */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex', overflow: 'hidden' }}>
        <Sidebar onNavigateChat={() => setView('chat')} />
        {view === 'chat' && <ChatView />}
        {view === 'agent' && <AgentPage />}
        {view === 'skill' && <SkillPage />}
      </div>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
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
