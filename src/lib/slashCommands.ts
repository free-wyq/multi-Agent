/**
 * SC-01 slash 命令注册表 + 自动补全源。
 *
 * 聊天输入框输入 `/` 触发 slash 命令体系（类 Slack/Discord）：
 *  - SC-02 SlashAutocomplete 输入 `/` 弹补全下拉，数据源 = 本文件 SLASH_COMMANDS；
 *  - SC-11 ChatPanel 输入框接入 slash 拦截：回车时若首词是 /name 则走对应 handler 而非默认发送；
 *  - SC-03~SC-10 逐个实现各命令 handler（替换下方 stub）。
 *
 * 设计：
 *  - 注册表是单一真源：name/description/usage（自动补全元数据）+ handler（执行体）。
 *    自动补全源即 SLASH_COMMANDS 本身——SC-02 直接 map 渲染，无需第二份数据。
 *  - handler 签名 `(ctx) => void | Promise<void>`：handler 不返回卡片，而是经 ctx.renderCard
 *    把输出推入聊天流（/model /tools 等渲染系统卡片）、ctx.clearChat 清空视图（/new）、
 *    ctx.busState 读共享状态（/status 纯本地聚合）。这样 handler 是纯函数 + 副作用经 ctx，
 *    可被 ChatPanel 统一接入（ChatPanel 构造 ctx 注入这些回调）。
 *  - ctx 不持 api 单例：handler 按需 `import { ... } from '../services/api'` 直接调
 *    （api 是模块级单例，无需经 ctx 透传）。ctx 只放「handler 无法自行获取」的 UI 耦合能力
 *    （renderCard/clearChat）+ 运行时上下文（groupId/args/busState）。
 *  - 本文件是 .ts（非 .tsx），不可写 JSX。故 stub handler 用 renderCard(字符串) 推占位
 *    （string 是合法 ReactNode）。SC-03~SC-10 实现富卡片时，handler 可 import .tsx 渲染器
 *    或在本文件用 React.createElement——届时该任务自决，本轮只保证注册表完整可编译。
 *
 * 命令清单（与 .task.md SC-03~SC-10 一一对应）：
 *  /new       SC-03  清空会话 + reset-session
 *  /model     SC-04  查看/切换 LLM 模型（configApi.get/put）
 *  /tools     SC-05  聚合内置工具 + 各 mounted_mcp 工具
 *  /skills    SC-06  浏览已装技能（skillApi.list）
 *  /status    SC-07  纯本地聚合（模型/token/各 agent 状态，来自 busState）
 *  /sessions  SC-08  按 group 聚合列历史会话
 *  /agent     SC-09  打开 AgentDetailPanel 聚合卡片
 *  /mcp       SC-10  内联 MCP 连接列表卡片
 *  /schedule  SC-10  内联定时任务列表卡片
 */
import type { ReactNode } from 'react'

import type { AgentStatusInfo, PlanStep } from '../services/api'

/**
 * handler 运行时上下文：由 ChatPanel（SC-11）在拦截到 slash 命令时构造注入。
 *
 * - groupId/args：命令执行所需输入（当前群组 + /name 之后的剩余参数文本）。
 * - renderCard：把一张系统卡片（ReactNode）推入聊天流尾部——/model /tools 等的输出载体。
 * - clearChat：清空本地聊天视图（/new 用：清消息流，handler 内自行再调 messageApi.clearByGroup
 *   + groupApi.resetSession 做服务端清理）。
 * - busState：BusEventContext 共享状态的只读快照——/status 纯本地聚合所需（agentStatuses/
 *   plan/streaming），不调 LLM 不调 api，直接读快照渲染。
 */
export interface SlashCommandContext {
  /** 当前聚焦群组 id（null = 未选群；/new /status 等需群组上下文的命令应判空提示）。 */
  groupId: string | null
  /** 命令参数：输入框中 `/name` 之后的剩余文本（已 trim，可为空串）。如 `/model gpt-4` → "gpt-4"。 */
  args: string
  /** 推一张系统卡片进聊天流尾部。node 可以是字符串（stub 占位）或富卡片（SC-03~10 实现）。 */
  renderCard: (node: ReactNode) => void
  /** 清空本地聊天视图（仅清前端消息流 state，不调后端；后端清理由 handler 自行调 api）。 */
  clearChat: () => void
  /** BusEventContext 共享状态只读快照（/status 纯本地聚合用）。 */
  busState: {
    agentStatuses: Record<string, AgentStatusInfo>
    plan: PlanStep[] | null
    streaming: Record<string, string>
  }
}

/** 命令处理器签名：同步或异步，无返回值（输出经 ctx.renderCard 推送）。 */
export type SlashCommandHandler = (ctx: SlashCommandContext) => void | Promise<void>

/**
 * 单条 slash 命令定义。
 *
 * - name：不含前导 `/`，小写（如 `new`/`model`）。自动补全匹配键。
 * - description：一句话说明，补全下拉展示。
 * - usage：用法串（含 `/`），如 `/model [模型名]`，补全下拉副标题。
 * - handler：执行体。SC-01 阶段为 stub（推占位卡片），SC-03~SC-10 替换为真实实现。
 */
export interface SlashCommand {
  name: string
  description: string
  usage: string
  handler: SlashCommandHandler
}

/**
 * stub handler 工厂：SC-03~SC-10 未实现前，命令被调用时推一张占位卡片。
 *
 * 用字符串（合法 ReactNode）而非 JSX——本文件是 .ts 不可写 JSX；真实 handler 实现时
 * 由对应 SC 任务改写（可直接在本文件用 React.createElement，或 import .tsx 渲染器）。
 * 占位让「补全 → 选中 → 回车」链路立即可验证，不阻塞 SC-02/SC-11 接入。
 */
function stub(name: string): SlashCommandHandler {
  return (ctx) => {
    ctx.renderCard(`「/${name}」命令开发中…`)
  }
}

/**
 * slash 命令注册表（单一真源）。SC-02 自动补全直接消费本数组。
 *
 * 顺序即补全下拉默认展示顺序——高频/入门命令靠前（new/model/status 常用），
 * 资源浏览类靠后。SC-03~SC-10 实现各 handler 时仅需替换对应 `handler: stub('xxx')`
 * 为真实函数，不动 name/description/usage（自动补全元数据稳定）。
 */
export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: 'new',
    description: '清空当前会话并重置引擎内存态，开始新对话',
    usage: '/new',
    handler: stub('new'),
  },
  {
    name: 'model',
    description: '查看当前 LLM 模型，或传名称切换',
    usage: '/model [模型名]',
    handler: stub('model'),
  },
  {
    name: 'status',
    description: '查看运行状态（模型/各智能体状态，纯本地聚合）',
    usage: '/status',
    handler: stub('status'),
  },
  {
    name: 'tools',
    description: '查看当前可用工具（内置 + 已挂载 MCP）',
    usage: '/tools',
    handler: stub('tools'),
  },
  {
    name: 'skills',
    description: '浏览已安装的技能列表',
    usage: '/skills',
    handler: stub('skills'),
  },
  {
    name: 'sessions',
    description: '查看按群组聚合的历史会话',
    usage: '/sessions',
    handler: stub('sessions'),
  },
  {
    name: 'agent',
    description: '查看智能体详情聚合卡片',
    usage: '/agent [名称]',
    handler: stub('agent'),
  },
  {
    name: 'mcp',
    description: '查看 MCP 连接列表',
    usage: '/mcp',
    handler: stub('mcp'),
  },
  {
    name: 'schedule',
    description: '查看定时任务列表',
    usage: '/schedule',
    handler: stub('schedule'),
  },
]

/**
 * 精确查找命令（按 name）。SC-11 输入框拦截到 `/name args` 后用此查 handler 执行。
 * @returns 命中返回 SlashCommand，未注册返回 undefined（调用方按「未知命令」处理）。
 */
export function getSlashCommand(name: string): SlashCommand | undefined {
  return SLASH_COMMANDS.find((cmd) => cmd.name === name)
}

/**
 * 自动补全过滤：按 name 前缀匹配，返回候选列表（SC-02 SlashAutocomplete 数据源）。
 *
 * - query 为空串 → 返回全部命令（输入 `/` 立即展示完整菜单）；
 * - query 非空 → 仅保留 name 以 query 开头的命令（大小写不敏感）；
 * - 保持注册表原序（高频在前），不做额外排序——稳定可预期。
 *
 * @param query 输入框中 `/` 之后的已输入文本，如 `mo`（不含前导 `/`）。
 */
export function matchSlashCommands(query: string): SlashCommand[] {
  const q = query.trim().toLowerCase()
  if (!q) return SLASH_COMMANDS
  return SLASH_COMMANDS.filter((cmd) => cmd.name.toLowerCase().startsWith(q))
}

/**
 * 解析整行输入是否为 slash 命令（SC-11 回车拦截用）。
 *
 * - 输入须以 `/` 开头且其后非空格才算命令（`/` 单独不算命令，避免误拦）；
 * - 返回 { name, args }：name = 首个 token 去 `/`，args = 其后剩余文本 trim；
 * - 非 slash 输入返回 null（调用方走默认发送）。
 *
 * 例：
 *   '/model gpt-4'      → { name: 'model', args: 'gpt-4' }
 *   '/status'           → { name: 'status', args: '' }
 *   '/ mcp'              → null（`/` 后紧跟空格，非命令）
 *   'hello /world'       → null（非 `/` 开头）
 *   '/'                  → null（仅 `/`）
 */
export function parseSlashCommand(
  input: string,
): { name: string; args: string } | null {
  const text = input.trimStart()
  if (!text.startsWith('/')) return null
  const rest = text.slice(1)
  if (rest.length === 0 || rest[0] === ' ') return null
  const spaceIdx = rest.search(/\s/)
  const name = spaceIdx === -1 ? rest : rest.slice(0, spaceIdx)
  const args = spaceIdx === -1 ? '' : rest.slice(spaceIdx + 1).trim()
  return { name, args }
}
