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
import { createElement } from 'react'

import ModelCard from '../components/ModelCard'
import SessionsCard from '../components/SessionsCard'
import SkillsCard from '../components/SkillsCard'
import StatusCard from '../components/StatusCard'
import ToolsCard from '../components/ToolsCard'
import { configApi, groupApi, messageApi, slashApi, skillApi } from '../services/api'
import type { AgentStatusInfo, PlanStep } from '../services/api'

// SC-04 handleModel 用到 LlmConfig（configApi 返回类型）——已由 ModelCard 内部 import，
// 此处仅为 handler 注释引用，不直接用类型故不重复 import（避免 noUnusedLocals 报错）。

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
 * SC-03 `/new` handler：开始新对话——清空本地视图 + 服务端清理，不散伙。
 *
 * 执行顺序（前端先清、后端兜底）：
 *  1. `ctx.clearChat()` —— 立即清空本地消息流 + 关补全下拉（乐观清，用户零等待）。
 *  2. `messageApi.clearByGroup(groupId)` —— DELETE /api/messages 清服务端持久化消息
 *     （与 reset-session 第①步同源；重复清是为双保险：reset-session 万一失败，消息也
 *     已被 DELETE 清掉，UI 不会留旧消息。两者写同一张 messages 表，幂等）。
 *  3. `groupApi.resetSession(groupId)` —— POST /reset-session 清引擎内存态 + 广播空 plan
 *     （方案 B 不重启引擎只清 _memory/_dispatch_plan/_pending_tasks；推 coordinator_plan=[]
 *     让 GroupPage/MonitorPage/ChatPanel 丢弃驻留计划卡）。
 *
 * 群组判空：未选群（null）时仅清空本地视图 + 友情提示——此时无服务端会话可重置
 * （用户可能正看「未选会话」占位态，/new 仍应清掉本地残留而非报错）。
 *
 * 错误处理：服务端清理失败不抛中断用户——经 ctx.renderCard 推一张 ⚠️ 卡片告知，
 * 但仍认为本地已清（用户体感是「新对话已开始」）。/new 是「重新开始」语义，失败也不应阻断
 * （handler 本身无 antd message 访问，错误信息只走 renderCard；ChatPanel 顶层 try/catch
 * 兜底未捕获异常会 message.error，但本 handler 自行 try/catch 不让异常外溢）。
 *
 * 反馈卡片：成功后推一张轻量系统卡片（字符串 ReactNode）说明清掉了多少引擎 +
 * 是否清了消息——给用户「确实重置了」的确认感（非静默清空）。失败时卡片也展示后端错误。
 */
async function handleNew(ctx: SlashCommandContext): Promise<void> {
  // 1. 本地视图立即清空（乐观）
  ctx.clearChat()

  // 未选群：无服务端会话可清，仅清本地即可
  if (!ctx.groupId) {
    ctx.renderCard('已清空本地视图（未选会话，无服务端会话可重置）')
    return
  }

  try {
    // 2. 服务端消息清理（DELETE /api/messages，与 reset-session 同源双保险）
    await messageApi.clearByGroup(ctx.groupId)
    // 3. 引擎内存态重置 + 广播空 plan（POST /reset-session）
    const resp = await groupApi.resetSession(ctx.groupId)
    ctx.renderCard(
      `✅ 已开始新对话（引擎重置 ${resp.engines_reset} 个` +
        `${resp.messages_cleared ? '，消息已清空' : ''}）`,
    )
  } catch (e) {
    // 服务端清理失败：本地已清，告知但不中断
    ctx.renderCard(
      `⚠️ 本地视图已清空，但服务端重置失败：${e instanceof Error ? e.message : String(e)}`,
    )
  }
}

/**
 * SC-04 `/model [模型名]` handler：查看 / 切换 LLM 模型，结果以 ModelCard 渲染进聊天。
 *
 * 两种形态：
 *  - 无参（`/model`）→ GET /api/config 拉 当前配置 → renderCard(ModelCard) 展示（switched=false）。
 *  - 有参（`/model glm-4.6`）→ PUT /api/config body={model} 热切换 → renderCard(ModelCard) 展示
 *    post-write 配置（switched=true，紫色边框 + ✅ 标题给「切换成功」即时反馈）。
 *
 * 后端 set_config 把新 model 写回 os.environ，下次 engine invoke 即生效（CF-05 无需重启）。
 * 返回的脱敏配置（api_key 仅首尾 3 字符预览）直接喂给 ModelCard——密钥真实值永不离开进程，
 * 前端只展示脱敏预览 + has_key 配置状态。
 *
 * 错误处理：GET/PUT 失败推 ⚠️ 字符串卡片告知（网络错 / 后端 500 / 模型名非法等），不中断
 * （/model 是查看类命令，失败仅是「没看到」，不应阻断聊天流）。
 *
 * 参数 trim：`/model  glm-4.6 `（多空格）→ args="glm-4.6"（parseSlashCommand 已 trim，双保险）。
 * 空串判定：parseSlashCommand 把 `/model`（无参）解析为 args=''，故 `!args` 判走 GET 分支。
 *
 * ModelCard 是独立 .tsx 组件（本文件 .ts 不可写 JSX），import 进来 renderCard 推 ReactNode。
 */
async function handleModel(ctx: SlashCommandContext): Promise<void> {
  try {
    if (!ctx.args) {
      // 无参：查看当前配置
      const config = await configApi.get()
      ctx.renderCard(createElement(ModelCard, { config }))
    } else {
      // 有参：热切换模型（写回 os.environ，下次 invoke 生效）
      const config = await configApi.put(ctx.args)
      ctx.renderCard(createElement(ModelCard, { config, switched: true }))
    }
  } catch (e) {
    ctx.renderCard(
      `⚠️ 获取/切换模型失败：${e instanceof Error ? e.message : String(e)}`,
    )
  }
}

/**
 * SC-05 `/tools [智能体名]` handler：聚合内置工具 + 各 mounted_mcp 工具，ToolsCard 渲染。
 *
 * 调 BE-01 `POST /api/slash` command=tools——后端是单一真源：内置工具定义在
 * engine.tools.tools_for_group（前端硬编码会漂移），MCP 工具需 async 自省（前端逐连接
 * GET /api/mcp/{id}/tools 是 N+1 且看不到合并集）。一次调用返回 internal + mcp 两段。
 *
 * agentId 解析：`/tools` 无参 → 不传 agentId（后端只返回内置 roster，不查 mounted_mcp）；
 * `/tools 智能体名` → 在 agents 里按 name 匹配定位 agent_id，传给后端查该 agent 的 mounted_mcp。
 * 无参时不查 MCP 是合理的默认——内置工具 workspace 无关总能列，MCP 工具依赖具体 agent 挂载，
 * 用户想看某 agent 的全套工具时显式传名（/tools 小后端）。
 *
 * groupId：始终传 ctx.groupId（内置工具 closure 捕获 group_id 绑定 workspace；虽 tools_for_group
 * 对空 group_id 也返回 roster，传了更准确）。未选群仍可调（workspace 无关部分照常）。
 *
 * 错误处理：网络错 / 后端 500 推 ⚠️ 字符串卡片；后端返 ok=false（MCP 加载失败）不抛错——
 * result.ok=false 时 ToolsCard 顶部红色 Alert 展示 error，仍展示已加载的 internal 段。
 */
async function handleTools(ctx: SlashCommandContext): Promise<void> {
  try {
    const result = await slashApi.tools(ctx.args || undefined, ctx.groupId || undefined)
    ctx.renderCard(createElement(ToolsCard, { result }))
  } catch (e) {
    ctx.renderCard(
      `⚠️ 获取工具列表失败：${e instanceof Error ? e.message : String(e)}`,
    )
  }
}

/**
 * SC-06 `/skills` handler：浏览已安装技能列表，SkillsCard 渲染。
 *
 * 调 `GET /api/skills`（skillApi.list）——本地已落库技能（builtin/custom/market 三来源），
 * 区别于 SkillPage 的「市场浏览」（SkillMarketEntry，待安装未落库）。/skills 聚焦「已装的有什么」。
 *
 * 无参无群组依赖——技能是全局资源（不绑群组），skillApi.list() 全量返回。args 忽略
 * （/skills 不接受参数，未来若加 /skills <name> 过滤可在此解析 args，本轮保持全量）。
 *
 * 错误处理：拉取失败推 ⚠️ 字符串卡片（网络错 / 后端 500），不中断聊天流。
 */
async function handleSkills(ctx: SlashCommandContext): Promise<void> {
  try {
    const skills = await skillApi.list()
    ctx.renderCard(createElement(SkillsCard, { skills }))
  } catch (e) {
    ctx.renderCard(
      `⚠️ 获取技能列表失败：${e instanceof Error ? e.message : String(e)}`,
    )
  }
}

/**
 * SC-07 `/status` handler：纯本地聚合运行状态，StatusCard 渲染（不调 LLM 不调 api）。
 *
 * 数据全部来自 ctx.busState（BusEventContext 快照）——agentStatuses（各 agent 的
 * idle/executing/offline）、plan（驻留计划）、streaming（流式 task_id→文本）。零网络开销，
 * 即时反馈「谁在跑、跑到哪」。
 *
 * /status 是只读快照命令——不调任何 api（区别于 /tools /model /skills 要拉后端），
 * busState 由 ChatPanel 从 useBusEventContext 注入，handler 直接读快照渲染。
 *
 * 群组判空：未选群时 agentStatuses 通常为空（无群组上下文 → 无 agent 状态），StatusCard
 * 展示「未选会话，无状态可聚合」占位。不报错（/status 在任何状态都可调，只是无内容时提示）。
 */
function handleStatus(ctx: SlashCommandContext): void {
  ctx.renderCard(
    createElement(StatusCard, {
      groupId: ctx.groupId,
      agentStatuses: ctx.busState.agentStatuses,
      plan: ctx.busState.plan,
      streaming: ctx.busState.streaming,
    }),
  )
}

/**
 * SC-08 `/sessions` handler：按 group 聚合列历史会话，SessionsCard 渲染。
 *
 * 并发拉两个源：groupApi.list（全部群组 = 会话骨架）+ messageApi.listAll（全部群组最近消息，
 * 跨群聚合做消息数统计 + 最后消息预览）。两源独立无依赖 → Promise.all 并发拉取最短耗时。
 * 前端按 group_id 把消息聚合到各会话：count（消息数）+ last（最后一条做预览）。会话按最后活跃
 * 时间倒序（有消息取 last.created_at，无消息取 group.updated_at）。
 *
 * 当前会话高亮：ctx.groupId 非空时该会话蓝边加粗（与 SessionList 视觉呼应），让用户在历史会话
 * 概览中一眼定位当前所在会话。args 忽略（/sessions 当前不接受参数，无参 = 列全部会话）。
 *
 * 与 SessionList 区别：SessionList 是左侧栏常驻导航（切群用，不拉消息预览避免 N+1，时间用
 * group.updated_at 近似）；/sessions 是命令触发的「历史会话概览」快照——补上消息数 + 最后预览
 * （messageApi.listAll 一次拉全跨群聚合，非 N+1）。用户 /sessions 想快速回看「最近跟哪些群聊过、
 * 各聊了多少、最后说了啥」，不必逐群点开。
 *
 * 错误处理：任一源拉取失败推 ⚠️ 字符串卡片告知（网络错 / 后端 500），不中断聊天流。
 */
async function handleSessions(ctx: SlashCommandContext): Promise<void> {
  try {
    const [groups, messages] = await Promise.all([
      groupApi.list(),
      messageApi.listAll(),
    ])
    ctx.renderCard(
      createElement(SessionsCard, {
        groups,
        messages,
        currentGroupId: ctx.groupId,
      }),
    )
  } catch (e) {
    ctx.renderCard(
      `⚠️ 获取历史会话失败：${e instanceof Error ? e.message : String(e)}`,
    )
  }
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
    handler: handleNew,
  },
  {
    name: 'model',
    description: '查看当前 LLM 模型，或传名称切换',
    usage: '/model [模型名]',
    handler: handleModel,
  },
  {
    name: 'status',
    description: '查看运行状态（模型/各智能体状态，纯本地聚合）',
    usage: '/status',
    handler: handleStatus,
  },
  {
    name: 'tools',
    description: '查看当前可用工具（内置 + 已挂载 MCP）',
    usage: '/tools [智能体名]',
    handler: handleTools,
  },
  {
    name: 'skills',
    description: '浏览已安装的技能列表',
    usage: '/skills',
    handler: handleSkills,
  },
  {
    name: 'sessions',
    description: '查看按群组聚合的历史会话',
    usage: '/sessions',
    handler: handleSessions,
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
