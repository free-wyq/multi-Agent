import { useState } from 'react'
import { Layout as AntLayout, Segmented } from 'antd'
import {
  MessageOutlined,
  AppstoreOutlined,
  ShopOutlined,
} from '@ant-design/icons'

import Sidebar from './Sidebar'
import ChatView from './ChatView'
import SettingsModal, { type NavKey } from './SettingsModal'
import AgentPage from '../pages/AgentPage'
import SkillPage from '../pages/SkillPage'

const { Header, Sider, Content } = AntLayout

/** 品牌橙（与 Sidebar 一致）。 */
const BRAND = '#F26522'
/** 侧栏宽度（与 Sidebar 的 SIDEBAR_WIDTH 一致，Sider 容器匹配 Sidebar 设计宽）。 */
const SIDEBAR_WIDTH = 240

type View = 'chat' | 'agent' | 'skill'

/**
 * Layout — 应用根布局（顶部栏 + 左右两栏）。
 *
 * 2026-07-26：迁移到 AntD Layout 组件族（Layout/Header/Sider/Content），落实
 * [[use-open-source-not-handrolled]]（有开源就用开源不手搓）。原手写 flex div 全部
 * 换成 AntD 语义标签，结构语义对齐 VS Code/Linear 风格。
 *
 * 布局演进 2026-07-12：在原「Sidebar + ChatView」之上加全局顶部栏，承载品牌 +
 * 三视图切换（对话 / 智能体广场 / skill广场）。主内容区按 activeView 在
 * ChatView / AgentPage / SkillPage 间切换——后两者直接复用全屏路由页组件，它们自带
 * 数据拉取与 height:100%+overflowY:auto，无需额外适配。
 *
 * 用户入口（头像 → SettingsModal 默认「用户信息」项）原在顶栏右上角，2026-07-12 移至
 * 侧栏左下角——顶栏是品牌+视图切换的语义区，混入用户/登录入口语义杂；侧栏底部恰空，
 * 符合 VS Code/Cursor/Linear 等开发者工具「用户入口放左下角」的习惯。回调 openUserSettings
 * 下发给 Sidebar 渲染。
 *
 * 侧栏列表项点击（createNewConversation/selectGroup）时经 onNavigateChat 自动切回对话视图，
 * 保证「在广场页点侧栏某个会话/群组 → 立即进入对话」直觉化。
 *
 * 高度链（[[flex-height-chain-lock]]）：#root{height:100%}（App.css）→
 * 外层 AntLayout{height:100%, display:flex, flexDirection:column} →
 * Header{flexShrink:0, height:48} + 内层 AntLayout{flex:1, minHeight:0, display:flex, flexDirection:row} →
 * Sider{flexShrink:0, width:240} + Content{flex:1, minHeight:0, overflow:hidden}。
 *
 * 关键覆盖：
 *  - AntD Header 默认 lineHeight:64px 会撑高，必须覆盖为 lineHeight:'normal'；
 *    默认深色背景覆盖为 var(--surface-raised)。
 *  - AntD Sider 默认深色背景，用 theme="light" + 显式 background 覆盖；
 *    默认 trigger 折叠按钮用 trigger={null} 隐藏（不引入 collapsible 新交互）。
 *  - Content 显式 minHeight:0 + overflow:hidden，否则 ChatView 的 flex:1 会塌 0。
 */
export default function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false)
  /** 打开设置弹窗时默认聚焦的导航项：头像入口='user'，其余默认 'mcp'。 */
  const [settingsInitialKey, setSettingsInitialKey] = useState<NavKey>('user')
  const [view, setView] = useState<View>('chat')

  // 用户入口（现移至侧栏左下角）：打开弹窗并默认定位到「用户信息」。
  const openUserSettings = () => {
    setSettingsInitialKey('user')
    setSettingsOpen(true)
  }

  return (
    <AntLayout style={{ height: '100%', overflow: 'hidden' }}>
      {/* 顶部栏：品牌 + 视图切换。
          三段等分（左/右各 flex:1）让中间 Segmented 真正居中。用户入口已移至侧栏左下角，
          右段留白保持居中布局（不再放头像）。
          白底浮起 + 底部投影，与主区灰底拉开层次（见 App.css --shadow-topbar）。
          覆盖 AntD Header 默认 lineHeight:64px（会撑高 48px 顶栏）+ 默认深色背景。 */}
      <Header
        style={{
          height: 48,
          flexShrink: 0,
          padding: '0 16px',
          borderBottom: '1px solid var(--border-soft)',
          display: 'flex',
          alignItems: 'center',
          background: 'var(--surface-raised)',
          boxShadow: 'var(--shadow-topbar)',
          lineHeight: 'normal',
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
        {/* 右段留白（flex:1）仅用于平衡左段让 Segmented 居中——用户入口已移至侧栏左下角 */}
        <div style={{ flex: 1 }} />
      </Header>

      {/* 主区：侧栏 + 视图内容。
          内层 AntLayout（hasSider 自动 flexDirection:row）。
          主区底色 --surface-main（次冷灰），比顶栏/侧栏/卡片白底后退一层，
          让浮起面（侧栏、广场页卡片、聊天气泡）的边界清晰可辨。
          注意：本层必须 minHeight:0——ChatView 用 flex:1 撑高度，若退回 block，
          flex:1 失效 → 高度塌 0 → 对话框与消息滚动一并消失（曾踩坑）。 */}
      <AntLayout style={{ flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' }}>
        <Sider
          theme="light"
          width={SIDEBAR_WIDTH}
          trigger={null}
          style={{
            flexShrink: 0,
            background: 'var(--surface-raised)',
          }}
        >
          <Sidebar
            onNavigateChat={() => setView('chat')}
            onOpenUserSettings={openUserSettings}
          />
        </Sider>
        <Content
          style={{
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            display: 'flex',
            overflow: 'hidden',
            background: 'var(--surface-main)',
          }}
        >
          {view === 'chat' && <ChatView />}
          {view === 'agent' && <AgentPage />}
          {view === 'skill' && <SkillPage />}
        </Content>
      </AntLayout>

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialKey={settingsInitialKey}
      />
    </AntLayout>
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
