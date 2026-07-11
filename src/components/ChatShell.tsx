import { useState, type ReactNode } from 'react'
import { Button, Tooltip } from 'antd'
import { MenuFoldOutlined, MenuUnfoldOutlined } from '@ant-design/icons'
import type { Group } from '../services/api'
import SessionList from './SessionList'

/** 右栏展开宽度（px）。成员/计划/执行/任务/文件 tabs 的内容区宽度。 */
const RIGHT_RAIL_EXPANDED = 340
/** 右栏折叠宽度（px）。折叠成窄条，只显折叠按钮 + tab 图标列。 */
const RIGHT_RAIL_COLLAPSED = 44

/**
 * L3-01 ChatShell 会话壳布局：三栏（左 SessionList + 中主区 + 右 RightRail slot）。
 *
 * SH-02 时代是两栏（左会话列表 + 右主区）。L3 三栏化：右栏承载会话级上下文
 * （成员/计划/执行/任务/文件），随当前会话切换——会话级上下文不再散落抽屉，
 * 跟随 BusEventContext.groupId（L3-02 RightRail 内部消费 context）。
 *
 * 纯布局外壳——自身不拉数据、不订阅 WS：
 *  - 左 240px：`SessionList`（会话列表 + 新建入口 + 高亮当前）。
 *  - 中 flex:1：主聊天区容器，渲染 `children`（ChatPanel）。
 *  - 右 340↔44：RightRail slot。ChatShell 只提供「右栏容器 + 折叠态 + slot」，
 *    右栏内容由 `rightRail` prop 注入（ChatPage 传 `<RightRail/>`）。折叠态由
 *    ChatShell 自持（collapsed state），折叠按钮在右栏顶部。
 *
 * 折叠设计（已定决策「全栏可折叠」）：
 *  - 展开态 width=340，渲染 rightRail 完整内容 + 顶部折叠按钮（MenuFoldOutlined）。
 *  - 折叠态 width=44，渲染窄条：折叠按钮（MenuUnfoldOutlined）竖排，点击展开。
 *  - 折叠态右栏不卸载内容（rightRail 仍在 DOM，仅容器 width 缩小 + overflow hidden）——
 *    避免反复折叠/展开时 RightRail 内部 state（如 LeaderPanel/WorkerTrace 的 WS 订阅、
 *    tab 激活态）丢失。CSS overflow:hidden + width 收缩即可视觉隐藏。
 *
 * 高度策略：`height: 100%` 填满父（Layout Content 给定 flex:1,minHeight:0 高度）。
 * 三栏 flex row，左/右固定宽 + flexShrink:0，中 flex:1+minWidth:0 弹性填充。
 *
 * 设计依据：三栏 = 左导航 + 中主区 + 右上下文是桌面协作应用标准布局（Slack/钉钉/
 * hermes-agent 同构）。外壳只管三栏 flex 骨架 + 右栏折叠，内容各自独立组件。
 */
interface ChatShellProps {
  /** 全部群组（=会话），透传给 SessionList 渲染。 */
  groups: Group[]
  /** 群组列表加载中态，透传给 SessionList 显示 Spin。 */
  loading?: boolean
  /** 新建会话入口回调，透传给 SessionList 顶部按钮。 */
  onNewSession?: () => void
  /** 主聊天区内容（ChatPanel）。 */
  children?: ReactNode
  /** 右栏内容（RightRail）。不传则不渲染右栏（向后兼容：无右栏时回退两栏）。 */
  rightRail?: ReactNode
}

export default function ChatShell({
  groups,
  loading,
  onNewSession,
  children,
  rightRail,
}: ChatShellProps) {
  // 右栏折叠态（自持）。默认展开——首屏即见会话上下文，用户主动折叠腾出聊天区。
  const [railCollapsed, setRailCollapsed] = useState(false)
  const railWidth = railCollapsed ? RIGHT_RAIL_COLLAPSED : RIGHT_RAIL_EXPANDED

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>
      {/* 左：会话列表（固定 240px，自含高亮当前 + 切群走全局 active group） */}
      <SessionList groups={groups} loading={loading} onNewSession={onNewSession} />

      {/* 中：主聊天区（弹性填充，背景 #fafafa） */}
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

      {/* 右：上下文栏（340↔44 可折叠，rightRail prop 注入内容）。
          rightRail 未传时不渲染——向后兼容两栏场景（如未来某些路由页无需右栏）。 */}
      {rightRail && (
        <aside
          style={{
            width: railWidth,
            flexShrink: 0,
            borderLeft: '1px solid #f0f0f0',
            background: '#fff',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            transition: 'width 0.2s ease',
          }}
        >
          {/* 折叠按钮：展开态在顶部右对齐（MenuFold），折叠态在顶部居中（MenuUnfold） */}
          <div
            style={{
              height: 40,
              flexShrink: 0,
              borderBottom: '1px solid #f0f0f0',
              display: 'flex',
              alignItems: 'center',
              justifyContent: railCollapsed ? 'center' : 'flex-end',
              padding: railCollapsed ? '0' : '0 8px',
            }}
          >
            <Tooltip title={railCollapsed ? '展开上下文栏' : '折叠上下文栏'}>
              <Button
                type="text"
                size="small"
                icon={railCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                onClick={() => setRailCollapsed((v) => !v)}
              />
            </Tooltip>
          </div>

          {/* 右栏内容：折叠态用 overflow:hidden 视觉隐藏（不卸载，保 RightRail 内部 state） */}
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: 'hidden',
              // 折叠态：内容容器不渲染（width 已缩 44px，内容无空间，隐藏避免错位）。
              // 展开态：正常渲染 rightRail。
              display: railCollapsed ? 'none' : 'flex',
              flexDirection: 'column',
            }}
          >
            {rightRail}
          </div>
        </aside>
      )}
    </div>
  )
}
