import { useState } from 'react'
import { ConfigProvider } from 'antd'
import { BusEventProvider } from './contexts/BusEventContext'
import { SelectionProvider } from './contexts/SelectionContext'
import Layout from './components/Layout'

/**
 * App — 顶层：ConfigProvider（主题）→ BusEventProvider（全局唯一群组 WS）→
 * SelectionProvider（左栏选择模型）→ Layout（左右两栏）。
 *
 * 布局重构 2026-07-11：去 react-router。单聊/群聊都收敛到「一个 groupId + ChatPanel」，
 * 左栏 Sidebar 触发选择（selectAgent find-or-create single_chat 群 / selectGroup 直接切群），
 * 两者最终都调 BusEventContext.setGroupId 切换 WS 订阅。SelectionProvider 在
 * BusEventProvider 内（它消费 groupId/setGroupId），Layout 在 SelectionProvider 内
 * （Sidebar/ChatView 消费选择态）。
 *
 * activeGroupId 起始 null（未选群不订阅 WS，避免冷启动对空群组建连）；切换群组时旧 WS 在
 * useBusEvent effect cleanup 中 unlisten，零泄漏。
 *
 * 品牌蓝统一为 #0A5ACF（极简开发者工具风格）。
 */
function App() {
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null)

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: '#0A5ACF',
          borderRadius: 6,
        },
      }}
    >
      <BusEventProvider groupId={activeGroupId} setGroupId={setActiveGroupId}>
        <SelectionProvider>
          <Layout />
        </SelectionProvider>
      </BusEventProvider>
    </ConfigProvider>
  )
}

export default App
