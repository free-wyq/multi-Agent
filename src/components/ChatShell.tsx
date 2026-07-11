import type { ReactNode } from 'react'
import type { Group } from '../services/api'
import SessionList from './SessionList'

/**
 * SH-02 ChatShell 会话壳布局：左侧 SessionList + 右侧主聊天区。
 *
 * 纯布局外壳——自身不拉数据、不订阅 WS：
 *  - 左 240px：`SessionList`（会话列表 + 新建入口 + 高亮当前），groups/loading/onNewSession
 *    由父组件（ChatPage）注入并透传给 SessionList。
 *  - 右 flex:1：主聊天区容器，渲染 `children`（SH-03 ChatPage 把 ChatPanel 作为 children
 *    塞入）。主区背景 #fafafa 与原 GroupPage 中间对话区一致，视觉零跳变。
 *
 * 高度策略：`height: 100%` 填满父容器——父（ChatPage/Content）负责给定可用高度
 * （SH-07 路由改造后聊天为默认主页，Content 会给到 `calc(100vh - ...)` 全高）。
 * 不在此处写死 `calc(100vh - 112px)`——那是 GroupPage 在 Layout Content(margin16+padding24)
 * 内的特化高度，ChatShell 作为通用外壳应填满父容器，由父决定具体高度，更可复用。
 *
 * 设计依据：会话壳 = 固定左栏 + 弹性主区是 IM 类应用最标准的两栏布局（钉钉/飞书/Slack
 * 同构）。外壳只管「左栏 + 主区」的 flex 骨架，会话列表与聊天内容各自独立组件，
 * 单一职责、易测试、SH-03 组合时只需 `<ChatShell {...}><ChatPanel/></ChatShell>`。
 */
interface ChatShellProps {
  /** 全部群组（=会话），透传给 SessionList 渲染。 */
  groups: Group[]
  /** 群组列表加载中态，透传给 SessionList 显示 Spin。 */
  loading?: boolean
  /** 新建会话入口回调，透传给 SessionList 顶部按钮。 */
  onNewSession?: () => void
  /** 主聊天区内容（SH-03 传入 ChatPanel）。 */
  children?: ReactNode
}

export default function ChatShell({ groups, loading, onNewSession, children }: ChatShellProps) {
  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>
      {/* 左：会话列表（固定 240px，自含高亮当前 + 切群走全局 active group） */}
      <SessionList groups={groups} loading={loading} onNewSession={onNewSession} />

      {/* 右：主聊天区（弹性填充，背景 #fafafa 与原 GroupPage 对话区一致） */}
      <main
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          background: '#fafafa',
          overflow: 'hidden',
        }}
      >
        {children}
      </main>
    </div>
  )
}
