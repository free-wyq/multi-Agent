import { createHashRouter } from 'react-router-dom'

import Layout from './components/Layout'
import ChatPage from './pages/ChatPage'
import AgentPage from './pages/AgentPage'
import SkillPage from './pages/SkillPage'
import McpPage from './pages/McpPage'
import SchedulePage from './pages/SchedulePage'
import TaskPage from './pages/TaskPage'
import MonitorPage from './pages/MonitorPage'

/**
 * L1-02 应用路由（react-router-dom v7，createHashRouter）。
 *
 * 选 createHashRouter 而非 createBrowserRouter：本项目是 Electron 桌面应用，打包后以
 * file:// 协议加载 index.html。BrowserRouter 的 history.pushState 在 file:// 下会把 URL
 * 推成 file:///agents，刷新或深链即 404（无 server fallback 兜底）。HashRouter 用 #/agents
 * 完全规避——桌面 SPA 标准做法（VS Code 等 Electron 应用同构）。dev 浏览器模式
 * （localhost:5173）hash 路由同样正常工作，两种场景零差异。
 *
 * 结构：Layout 作外层 layout route（element: <Layout/>，其 Content 渲染 <Outlet/>——见
 * L1-02 对 Layout.tsx 的同步改动），7 个子路由经 Outlet 渲染。聊天主页为 index 路由，
 * 6 个资源/监控页全屏路由。Layout 常驻（Header + 后续 Statusbar），路由切换只换 Outlet
 * 内容，不重挂 Layout——BusEventProvider 在 App 层包裹 RouterProvider，WS 连接跨路由不中断。
 *
 * 路由清单（与 plan 功能保留对照表一致；各页均为 zero-prop default export，直接挂载）：
 *   /          ChatPage     聊天主页（默认 index）
 *   /agents    AgentPage    智能体管理（全屏）
 *   /skills    SkillPage    技能市场（全屏）
 *   /mcp       McpPage      MCP 工具（全屏）
 *   /schedule  SchedulePage 定时任务（全屏）
 *   /tasks     TaskPage     任务 DAG（全屏，L1-04 修 ReactFlow 写死高度）
 *   /monitor   MonitorPage  执行监控（全屏，后续阶段拆进右栏）
 *
 * 不在此处加 errorElement：保持路由定义纯净，错误边界留待后续按需增强（无人值守模式下
 * 各页自身已有 try/catch + antd message 兜底，路由级 errorBoundary 非必要不引入复杂度）。
 */
export const router = createHashRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <ChatPage /> },
      { path: 'agents', element: <AgentPage /> },
      { path: 'skills', element: <SkillPage /> },
      { path: 'mcp', element: <McpPage /> },
      { path: 'schedule', element: <SchedulePage /> },
      { path: 'tasks', element: <TaskPage /> },
      { path: 'monitor', element: <MonitorPage /> },
    ],
  },
])
