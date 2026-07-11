import { useState } from 'react'
import { ConfigProvider } from 'antd'
import { RouterProvider } from 'react-router-dom'
import { router } from './router'
import { BusEventProvider } from './contexts/BusEventContext'

/**
 * App — 顶层：ConfigProvider（主题）→ BusEventProvider（全局唯一群组 WS）→ RouterProvider。
 *
 * WS-03：在 provider 层持有 `activeGroupId` 并包裹 `BusEventProvider`，全应用共享一条
 * 群组 WS 连接。`activeGroupId` 经 provider 的 `setGroupId` 下发到 context——任何子组件
 * 选中群组时调 `useBusEventContext().setGroupId(id)` 即可切换全局聚焦群组，provider 重新
 * 订阅对应事件流，全应用跟随。
 *
 * L1-02：接入 react-router-dom v7（createHashRouter）。RouterProvider 替换原直接渲染的
 * <Layout/>——Layout 作根 layout route（router.tsx 中 element: <Layout/>），其内部 Content
 * 渲染 <Outlet/> 承载 7 个子路由。关键顺序：BusEventProvider 在 RouterProvider 外层，故
 * 跨路由切换（/agents ↔ / ↔ /tasks）时 provider 不卸载，WS 连接 + activeGroupId 不中断——
 * 路由切换只换 Outlet 内容，全局 WS 态在 Layout + 各页间共享，零重连。
 *
 * `activeGroupId` 起始为 null（未选群不订阅 WS，避免冷启动对空群组建连）；切换群组时旧
 * WS 在 `useBusEvent` effect cleanup 中 unlisten，零泄漏。子组件经 `useBusEventContext()`
 * 消费共享状态 + setGroupId 切群。
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
        <RouterProvider router={router} />
      </BusEventProvider>
    </ConfigProvider>
  )
}

export default App


