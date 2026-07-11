import { useState } from 'react'
import { Drawer, Tabs } from 'antd'
import type { TabsProps } from 'antd'
import {
  RobotOutlined,
  TeamOutlined,
  DashboardOutlined,
  MessageOutlined,
  AppstoreAddOutlined,
  ApiOutlined,
  ScheduleOutlined,
} from '@ant-design/icons'

import AgentPage from '../pages/AgentPage'
import GroupPage from '../pages/GroupPage'
import TaskPage from '../pages/TaskPage'
import MonitorPage from '../pages/MonitorPage'
import SkillPage from '../pages/SkillPage'
import McpPage from '../pages/McpPage'
import SchedulePage from '../pages/SchedulePage'

/**
 * SH-06 SettingsDrawer：右上角抽屉，Tab 承载现有资源页。
 *
 * 聊天为默认主页（SH-07）后，原 7 个独立页面（智能体/群组/任务/监控/技能/MCP/定时）
 * 降级为本抽屉内的 Tab——聊天常驻，资源管理按需从右上角入口拉开抽屉访问。
 *
 * 设计：
 *  - 大幅右抽屉（width 88%，响应式），body 去默认 padding 让各页自适应（AgentPage 等自带
 *    max-width 居中、h2 标题，抽屉内保留其原始视觉）。
 *  - Tabs 横向标签，icon + 中文 label，图标与 Layout 旧菜单一致（视觉延续，零学习成本）。
 *  - activeKey 支持受控（activeKey + onActiveKeyChange）与非受控（defaultActiveKey）两种用法：
 *    SH-07 顶部按钮非受控打开即可；SC-09 /agent slash 命令受控打开定位到 agent tab。
 *  - 默认（destroyInactiveTabPane 不设）= antd Tabs 懒挂载 + 持久化：用户访问过的 tab 保持
 *    挂载（切回不重拉数据），未访问的 tab 不挂载（不预载）——资源页偶尔访问，避免一开抽屉
 *    就同时挂载 7 页（GroupPage 全聊天 + MonitorPage/TaskPage 各自数据拉取）造成瞬时负担。
 *    WS 仍走 BusEventContext 全局共享一条，Tab 切换不新增 WS。
 *
 * 各页 zero-prop 直接渲染（签名都是零参 `export default function XxxPage()`）——本抽屉只做
 * 容器，不改各页内部逻辑。SH-05 各页降级适配（如去掉自带页面级 h2 避免与 Tab 标题重复）留
 * 该任务处理；当前直接承载，Tab 标题 + 页内 h2 短期共存可接受。
 */
export type SettingsTabKey =
  | 'agents'
  | 'groups'
  | 'tasks'
  | 'monitor'
  | 'skills'
  | 'mcp'
  | 'schedule'

interface SettingsDrawerProps {
  /** 抽屉开关。 */
  open: boolean
  /** 关闭回调。 */
  onClose: () => void
  /** 受控当前激活 tab（提供则受控，否则用内部 state 走 defaultActiveKey）。 */
  activeKey?: SettingsTabKey
  /** 激活 tab 变化回调（受控/非受控都会调，供 SC-09 等同步外部状态）。 */
  onActiveKeyChange?: (key: SettingsTabKey) => void
  /** 非受控初始 tab（默认 agents）。 */
  defaultActiveKey?: SettingsTabKey
}

/** Tab 配置：icon + label + 内容页。顺序沿用 Layout 旧菜单，降低迁移认知成本。 */
const TAB_ITEMS: TabsProps['items'] = [
  { key: 'agents', label: <span><RobotOutlined /> 智能体</span>, children: <AgentPage /> },
  { key: 'groups', label: <span><TeamOutlined /> 群组</span>, children: <GroupPage /> },
  { key: 'tasks', label: <span><DashboardOutlined /> 任务</span>, children: <TaskPage /> },
  { key: 'monitor', label: <span><MessageOutlined /> 监控</span>, children: <MonitorPage /> },
  { key: 'skills', label: <span><AppstoreAddOutlined /> 技能市场</span>, children: <SkillPage /> },
  { key: 'mcp', label: <span><ApiOutlined /> MCP 工具</span>, children: <McpPage /> },
  { key: 'schedule', label: <span><ScheduleOutlined /> 定时任务</span>, children: <SchedulePage /> },
]

export default function SettingsDrawer({
  open,
  onClose,
  activeKey,
  onActiveKeyChange,
  defaultActiveKey = 'agents',
}: SettingsDrawerProps) {
  // 非受控内部 state；受控时（activeKey !== undefined）用外部值，忽略内部 state。
  const [internalKey, setInternalKey] = useState<SettingsTabKey>(defaultActiveKey)
  const isControlled = activeKey !== undefined
  const current = isControlled ? (activeKey as SettingsTabKey) : internalKey

  const handleChange = (key: string) => {
    const k = key as SettingsTabKey
    if (!isControlled) setInternalKey(k)
    onActiveKeyChange?.(k)
  }

  return (
    <Drawer
      title="资源管理"
      placement="right"
      open={open}
      onClose={onClose}
      width="88%"
      styles={{ body: { padding: 0 } }}
    >
      <Tabs
        // 受控/非受控统一经 current + handleChange；activeKey 始终传确定值。
        activeKey={current}
        onChange={handleChange}
        // tabPosition="left" 纵向标签在大幅抽屉里更像「设置中心」，但横向更省高度留给内容；
        // 选 top（默认）——抽屉宽度足够横向排开 7 标签，内容区高度最大化。
        tabPosition="top"
        items={TAB_ITEMS}
        // 抽屉内嵌各资源页：给 Tab 内容容器加 padding，让各页内容有呼吸（Drawer body padding 已清零）。
        // 各页自带 max-width 居中 + h2，外层只补水平 padding。
        tabBarStyle={{ paddingLeft: 16, paddingRight: 16 }}
      />
    </Drawer>
  )
}
