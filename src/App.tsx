import { useState } from 'react'
import { ConfigProvider } from 'antd'
import Layout from './components/Layout'
import { BusEventProvider } from './contexts/BusEventContext'

/**
 * App — 顶层：ConfigProvider（主题）→ BusEventProvider（全局唯一群组 WS）→ Layout。
 *
 * WS-03：在 provider 层持有 `activeGroupId` 并包裹 `BusEventProvider`，全应用共享一条
 * 群组 WS 连接。`activeGroupId` 经 provider 的 `setGroupId` 下发到 context——任何子组件
 * （WS-04 GroupPage / WS-05 MonitorPage 等迁移后）选中群组时调
 * `useBusEventContext().setGroupId(id)` 即可切换全局聚焦群组，provider 重新订阅对应
 * 事件流，全应用跟随。
 *
 * `activeGroupId` 起始为 null（未选群不订阅 WS，避免冷启动对空群组建连）；切换群组时旧
 * WS 在 `useBusEvent` effect cleanup 中 unlisten，零泄漏。
 *
 * 当前 Layout 仍是签名零参的旧组件（WS-04/WS-05 才迁移子页面到 context），暂不读
 * context——activeGroupId 此刻仅作为 provider 的 groupId 输入，使全局 WS 通道就位。
 * 子页面迁移后通过 `useBusEventContext()` 直接消费共享状态 + setGroupId 切群。
 */
function App() {
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null)

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: '#1677ff',
          borderRadius: 6,
        },
      }}
    >
      <BusEventProvider groupId={activeGroupId} setGroupId={setActiveGroupId}>
        <Layout />
      </BusEventProvider>
    </ConfigProvider>
  )
}

export default App


