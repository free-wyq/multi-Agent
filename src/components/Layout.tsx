import { useState } from 'react'

import Sidebar from './Sidebar'
import ChatView from './ChatView'
import SettingsModal from './SettingsModal'

/**
 * Layout — 应用根布局（左右两栏，布局重构 2026-07-11）。
 *
 * 取代原三栏 + 顶部 Header + 底部 Statusbar + 7 路由页的架构。新结构极简：
 * 左 240 Sidebar（品牌 + 智能体/多智能体折叠分组 + ⚙设置）+ 右 ChatView（统一标题区 +
 * ChatPanel）。无顶栏、无底栏——状态信息后续按需塞入右侧标题区。
 *
 * 设置弹窗由本组件持有 open 态，Sidebar 通过 onOpenSettings 触发。SelectionContext +
 * BusEventProvider 在 App 层包裹本组件（见 App.tsx），故 Sidebar/ChatView 内部可直接消费
 * 选择态与 WS 数据流。
 *
 * 高度链：#root{height:100%}（App.css）→ 本 flex 容器 height:100% → Sidebar 固定宽 +
 * ChatView flex:1 minWidth:0。
 */
export default function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false)

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>
      <Sidebar onOpenSettings={() => setSettingsOpen(true)} />
      <ChatView />
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}
