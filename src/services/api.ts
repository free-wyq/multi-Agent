/**
 * API 层：HTTP fetch + WebSocket（替代 Tauri invoke/listen）
 *
 * 后端为 Python FastAPI（localhost:8000）。所有接口签名与返回类型保持不变，
 * 页面组件零改。24 个后端 endpoint 见 backend/api/。
 */

const API_BASE = 'http://localhost:8000'

// ── 类型定义 ──────────────────────────────────────────────────────

export interface AgentDefinition {
  id: string
  name: string
  role: string
  extra_skills?: string[]
  skills?: string[]
  mounted_skills?: string[]
  /** AG-08/AG-09: 已挂载的 MCP 连接 id 列表（mount_mcp/unmount_mcp 维护）。 */
  mounted_mcp?: string[]
  system_prompt?: string
  model?: string
  max_turns?: number
  description?: string | null
  /** AG-05: 工具权限白名单/黑名单（后端 AgentDefinition 字段，当前种子为空）。 */
  allowed_tools?: string[]
  denied_tools?: string[]
  created_at: string
  updated_at: string
}

export interface AgentCreatePayload {
  name: string
  role: string
  extra_skills?: string[]
  skills?: string[]
  system_prompt?: string
  description?: string
  /** AD-02: agentApi.update 透传的工具权限白/黑名单（后端 AgentCreatePayload extra="allow"
   *  + AgentEntity 有 allowed_tools/denied_tools 列，update_agent model_dump(exclude_unset)
   *  + setattr 落库）。create 时通常不传（挂载是独立动作），仅 update 用。 */
  allowed_tools?: string[]
  denied_tools?: string[]
  /** AD-02: agentApi.update 透传的运行参数（后端 AgentEntity model/max_turns 列）。 */
  model?: string
  max_turns?: number
}

/**
 * AG-11: 预设角色模板（「角色模板广场」浏览项，非落库 Agent）。
 *
 * 与 AgentDefinition 有意不同：模板是「可被发现待雇佣」的对象，尚未落本地库——
 *  - 用 `template_id` 标识（形如 `tpl:backend-engineer`），而非 Agent 的 `id`；
 *  - 无 mounted_skills/mounted_mcp/allowed_tools/denied_tools——挂载是独立用户动作
 *    （AG-08/AG-09），模板只承载角色身份（是谁、能做什么），与 AG-01 生成同立场；
 *  - 带 UI 元数据 `category`/`icon_emoji` 供广场分组与徽标渲染。
 *
 * 字段 snake_case 对齐后端 AgentTemplate 模型（backend/agent_templates.py）与
 * 既有 api.ts 约定。AG-12 雇佣时用 template_id 调 POST /api/agents/templates/{id}/hire
 * 落库为本地 AgentDefinition。
 */
export interface AgentTemplate {
  template_id: string
  name: string
  role: string
  description: string
  system_prompt: string
  skills: string[]
  extra_skills: string[]
  category: string
  icon_emoji: string
}

export interface Group {
  id: string
  name: string
  coordinator_id: string
  description: string | null
  status: string
  /**
   * 群组级动态配置（后端 GroupEntity.config JSON 列，mirror of models.GroupConfig）。
   * 由后端按 key 约定写入 + 内联读出：auto_confirm（PL-02/03 计划确认开关）、
   * leader_strategy（MT-03 Leader 指挥策略，群设置 Modal 写入 → coordinator 注入 prompt）。
   * 后端 update_group 对 config 做 key 级 merge（不整体替换），故前端 PUT 时传整个 config
   * dict 也安全——新 key 合并、已有 key 覆盖。可为 null（群无配置）。
   */
  config?: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface GroupMember {
  id: string
  group_id: string
  agent_id: string
  alias: string | null
  joined_at: string
  agent_name: string
  agent_role: string
  // 后端返回平铺结构（agent_name/agent_role join 平铺），
  // member 嵌套字段保留以兼容（可能存在也可能不存在）
  member?: {
    id: string
    group_id: string
    agent_id: string
    alias: string | null
    joined_at: string
  }
}

export interface GroupFile {
  name: string
  size: number
  modified_at: string
}

export interface GroupCreatePayload {
  name: string
  coordinator_id?: string
  description?: string
  member_ids?: string[]
  /**
   * MT-03: 群组级动态配置（镜像 Group.config）。PUT /api/groups/{id} 时传整个
   * config dict——后端 update_group 对 config 做 key 级 merge（不整体替换），
   * 故传 `{ leader_strategy: '...' }` 仅覆盖 leader_strategy，保留共存键
   * （如 auto_confirm）。GroupCreatePayload.extra='allow' 容纳此未声明字段。
   */
  config?: Record<string, unknown>
}

export type TaskStatus = 'submitted' | 'working' | 'completed' | 'failed' | 'canceled' | 'input_required'

export interface Task {
  id: string
  group_id: string
  parent_task_id: string | null
  title: string
  description: string | null
  status: TaskStatus
  assigned_agent_id: string | null
  instance_id: string | null
  dependencies: string[]
  artifact_path: string | null
  artifact: Record<string, unknown> | null
  exit_code: number | null
  error_message: string | null
  result_summary: string | null
  dag_order: number | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface TaskCreatePayload {
  group_id: string
  title: string
  description?: string
  assigned_agent_id?: string
  dependencies?: string[]
  dag_order?: number
}

export interface Message {
  id: string
  group_id: string
  task_id: string | null
  sender_id: string
  receiver_id: string
  type: string
  content: string | null
  data: Record<string, unknown> | null
  created_at: string
}

export interface MessageCreatePayload {
  group_id: string
  task_id?: string
  sender_id: string
  receiver_id?: string
  type?: string
  content?: string
  data?: Record<string, unknown>
  /**
   * @收束 回合收敛开关（converge-turn-design）。一次性开关：开启后下条消息以收束回合发
   * （@某 agent → 该 agent 回一句即 END 不 handoff，回合自然收敛），发完自动灭。仅 @mention
   * 路径有意义；开关亮但消息无 @ → 后端 400 拒绝「收束必须 @ 收口对象」。
   * 默认 false，向后兼容既有调用方。后端透传到 invoke_turn(converge=True) 注入 GroupState。
   */
  converge?: boolean
}

// ── HTTP 工具 ────────────────────────────────────────────────

async function http<T>(
  method: string,
  path: string,
  body?: unknown,
  params?: Record<string, string>,
): Promise<T> {
  const url = new URL(API_BASE + path)
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v)
    })
  }
  const resp = await fetch(url.toString(), {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!resp.ok) {
    throw new Error(`API ${resp.status}: ${await resp.text()}`)
  }
  // 空主体（DELETE 返回 boolean 时 FastAPI 仍返回 JSON）
  const text = await resp.text()
  return (text ? JSON.parse(text) : null) as T
}

// ── Agent API ────────────────────────────────────────────────

export const agentApi = {
  list: () => http<AgentDefinition[]>('GET', '/api/agents'),
  get: (id: string) => http<AgentDefinition | null>('GET', `/api/agents/${id}`),
  create: (body: AgentCreatePayload) => http<AgentDefinition>('POST', '/api/agents', body),
  /**
   * AG-01: 自然语言生成完整智能体配置。
   *
   * POST /api/agents/generate body={description}。后端调 LLM 按提示词生成
   * name/role/system_prompt/skills/extra_skills/description 六字段，role 经
   * snake_case 规整，落库返回 AgentDefinition（mounted_skills/mcp/allowed/denied
   * 全空——挂载是独立用户动作 AG-08）。LLM 失败时后端 fallback 裸配置，仍返回 agent。
   *
   * 与 skillApi.generate 同构（description 单参数 + JSON body + 返回落库对象）。
   * body 字段 description 同名无 snake/camel 转换问题。
   */
  generate: (description: string) =>
    http<AgentDefinition>('POST', '/api/agents/generate', { description }),
  update: (id: string, body: Partial<AgentCreatePayload>) =>
    http<AgentDefinition | null>('PUT', `/api/agents/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/agents/${id}`),
  /**
   * AG-11: 列出预设角色模板（供「角色模板广场」浏览）。
   *
   * GET /api/agents/templates?category=。后端委托 agent_templates.list_templates：
   * catalog 是模块级静态常量恒可用（无网络/DB 依赖），air-gapped 或未配 LLM 也能渲染。
   * category 留空返回全部（当前 10 模板），给定则精确匹配分类（固定中文标签如「开发」
   * 「测试」），未知分类返回空数组（200 + []，非 404）。
   *
   * 与 skillApi.searchMarket 同构（GET + 可选 query 筛选 + 返回静态 catalog 列表），
   * 但 listTemplates 不做模糊搜索——模板分类是固定枚举，按 Tab 传精确分类即可。
   * 返回 AgentTemplate[]（含 template_id 供 AG-12 雇佣、category/icon_emoji 供广场渲染）。
   */
  listTemplates: (category?: string) =>
    http<AgentTemplate[]>('GET', '/api/agents/templates', undefined, category ? { category } : undefined),
  /**
   * AG-12: 雇佣预设角色模板，落库为本地员工。
   *
   * POST /api/agents/templates/{templateId}/hire body={name?}。后端按 template_id 解析
   * catalog 全配置（role/system_prompt/skills/extra_skills/description 原样），构造
   * AgentCreatePayload 落库返回 AgentDefinition（与 list/create 同类型，直接进员工列表）。
   * name 可选覆盖（雇佣时个性化改名，如「后端开发工程师」→「小后端」）；不传或空串回退
   * 模板名。未知 template_id → 404（catalog 无此条目）。雇佣的 agent 无 mounted_skills/
   * mcp/tools（挂载是独立用户动作 AG-08/AG-09，与 AG-01 生成、AG-11 模板同立场）。
   *
   * 与 skillApi.installMarket 同构（按 id 解析 catalog/市场配置落库为本地对象，仅传 id
   * 而非全文——配置真源在后端 agent_templates._CATALOG，前端传全文会与服务端漂移）。
   * template_id 形如 `tpl:backend-engineer`，`:` 是 RFC3986 path-safe 字符（pchar），
   * 直接拼路径不经 encodeURIComponent（与 get(id)/delete(id) 处理 agent id 同风格），
   * fetch + FastAPI {template_id} 转换器均正常吃下。
   *
   * body 始终传对象（name 有值传 {name}，否则传 {}）：{} 是合法 JSON body，Pydantic
   * HireTemplateBody 全字段可选，解析为 name=None → 后端回退模板名；比传 undefined
   * 更显式（避免「无 body 是否被当缺参 422」的歧义，curl 验证 body={} 返 200）。
   */
  hireTemplate: (templateId: string, name?: string) =>
    http<AgentDefinition>(
      'POST',
      `/api/agents/templates/${templateId}/hire`,
      name ? { name } : {},
    ),
}

// ── Group API ────────────────────────────────────────────────

/** POST /api/groups/{id}/reset-session 响应（/new slash 命令后端，BE-02）。 */
export interface ResetSessionResponse {
  ok: boolean
  group_id: string
  /** 是否真的清掉了消息（幂等：已无消息时返 false）。 */
  messages_cleared: boolean
  /** 被重置内存态的引擎实例数（coordinator + workers；冷启动群为 0）。 */
  engines_reset: number
}

export const groupApi = {
  list: () => http<Group[]>('GET', '/api/groups'),
  get: (id: string) => http<Group | null>('GET', `/api/groups/${id}`),
  create: (body: GroupCreatePayload) => http<Group>('POST', '/api/groups', body),
  /**
   * MT-04: 根据已选群主 + 成员 roster 自动生成团队名称和描述。
   *
   * POST /api/groups/generate-name body={coordinator_id?, member_ids?}。后端解析
   * 成员 name/role → LLM 综合出项目向团队名 + 一句话描述 → 返回 {name, description}。
   * LLM 失败时 fallback 用 roster 成员名拼接「XX团队」（永不抛错，创建流始终拿到可用建议）。
   *
   * 调用时机：用户在新建群组 Modal 选完群主+成员后点「自动生成」，回填 name/description
   * 字段供用户审核编辑（建议性，用户可改）。与 agentApi.generate 同构（LLM 生成 + JSON 解析 +
   * fallback），但不落库——只返回建议，落库走后续 groupApi.create。
   */
  generateNameDesc: (coordinatorId?: string, memberIds: string[] = []) =>
    http<{ name: string; description: string }>('POST', '/api/groups/generate-name', {
      coordinator_id: coordinatorId,
      member_ids: memberIds,
    }),
  update: (id: string, body: Partial<GroupCreatePayload>) =>
    http<Group | null>('PUT', `/api/groups/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/groups/${id}`),
  /**
   * SC-03 /new 的服务端清理：POST /api/groups/{id}/reset-session（BE-02）。
   *
   * 不解散团队、只重置会话——三件事：① 清该群持久化消息（与 DELETE /api/messages
   * 同源，reload 后不复发）；② 清驻留引擎实例的内存态（_memory/_dispatch_plan/
   * _recent_routes/_pending_tasks，方案 B 不重启引擎只清跨调用状态；在跑的任务先 cancel）；
   * ③ 广播空 coordinator_plan（plan=[]）让所有连接客户端（GroupPage/MonitorPage/ChatPanel）
   * 立即丢弃驻留计划卡。
   *
   * 幂等：冷启动群无引擎（engines_reset=0）但消息仍清；已无消息返 messages_cleared=false。
   * 永不抛错（未知/冷群也 200）——/new 是「重新开始」语义，失败也不应阻断用户开新对话。
   */
  resetSession: (id: string) =>
    http<ResetSessionResponse>('POST', `/api/groups/${id}/reset-session`),
  listMembers: (id: string) => http<GroupMember[]>('GET', `/api/groups/${id}/members`),
  addMember: (id: string, agent_id: string, alias?: string) =>
    http<GroupMember>('POST', `/api/groups/${id}/members`, { agentId: agent_id, alias }),
  removeMember: (id: string, memberId: string) =>
    http<boolean>('DELETE', `/api/groups/${id}/members/${memberId}`),
  listFiles: (id: string) => http<GroupFile[]>('GET', `/api/groups/${id}/files`),
  /**
   * PL-12: 下载群组工作区产物文件。返回可直接触发浏览器下载的 URL
   * （后端 FileResponse 流式回传，带 Content-Disposition filename）。
   *
   * 不走通用 http<T> —— 下载是二进制流不是 JSON，http 会 JSON.parse 报错。
   * 改为返回 URL，由调用方决定如何消费：
   *  - 直接 window.open(url) 触发浏览器下载（最简，浏览器处理 Content-Disposition）；
   *  - 或 fetch(url) 拿 Blob 再 saveAs（更可控，可加 loading 态/错误处理）。
   *
   * fileName 是工作区相对 POSIX 路径（来自 Task.artifact_path 或
   * Task.artifact.files[].path），可能含子目录（如 `login-api/index.js`）。
   * 每段单独 encodeURIComponent 后用 `/` 拼，避免裸 `/` 被 URL 当成路径段
   * 分割（虽后端 {name:path} 转换器能吃斜杠，但显式 encode 各段最稳，对
   * 含空格/中文/特殊字符的文件名也安全）。
   */
  downloadFileUrl: (groupId: string, fileName: string): string => {
    const encoded = fileName
      .split('/')
      .map((seg) => encodeURIComponent(seg))
      .join('/')
    return `${API_BASE}/api/groups/${groupId}/files/${encoded}`
  },
  /**
   * PL-12: 下载产物文件为 Blob（用于前端可控的下载，可加 loading/错误提示）。
   * 失败时抛 Error（http 状态 + 后端文案），调用方 catch 后 message.error。
   */
  downloadFile: async (groupId: string, fileName: string): Promise<Blob> => {
    const resp = await fetch(groupApi.downloadFileUrl(groupId, fileName))
    if (!resp.ok) {
      throw new Error(`下载失败 (${resp.status}): ${await resp.text()}`)
    }
    return resp.blob()
  },
  /**
   * StopSignal UI 停止：POST /api/groups/{id}/stop-turn（task-23 后端端点）。
   *
   * 群图回合级停止——经后端 GroupRuntime.cancel_turn 双层停止：
   *  ① ``_stop_event.set()`` 协作让步（任何将启动的节点 yield Command(goto=END)）；
   *  ② ``_current_task.cancel()`` 硬切断流（CancelledError 传入流式 LLM async for
   *     mid-stream 断流）。
   * cancel 后后端 emit agent_status(idle) 给 Leader，UI 自动从「执行中」归位（cancelled
   * 回合自己的 invoke_turn cancel 分支重抛不 emit idle，故本端点 owns 终端 UI 状态）。
   *
   * 区别 taskApi.stop（PL-11 驻留引擎 per-task 经 stop_task_by_id/request_cancel）——
   * 本方法是群图整回合停止（StopTaskButton / busy-input 打断走它）。返回的 message
   * 字段供 UI toast（task-26 StopTaskButton 保留 message 契约），cancelled 诊断字段
   * 区分「真停了活跃回合」vs「无活跃回合 no-op」（不与自然完成竞态，两者都 200）。
   */
  stopTurn: (groupId: string) =>
    http<GroupStopTurnResponse>('POST', `/api/groups/${groupId}/stop-turn`),
}

/** POST /api/groups/{id}/stop-turn 响应（StopSignal UI 停止，task-23）。 */
export interface GroupStopTurnResponse {
  ok: boolean
  group_id: string
  /** true=有活跃回合且已硬切（cancel_turn issued task.cancel）；false=无活跃回合 no-op。 */
  cancelled: boolean
  /** 人类可读结果文案，前端可直接 toast 提示（保留 message 字段契约供 StopTaskButton）。 */
  message: string
}

// ── Plan API (M12 PL-02/PL-03 计划确认闭环) ─────────────────

/** 计划修改时单步的 patch（除 step 外皆可选，仅传需改字段）。 */
export interface PlanModifyStep {
  step: number
  agent_id?: string
  agent_name?: string
  instruction?: string
  depends_on?: number[]
}

/** /plan/confirm 与 /plan/direct 的响应。 */
export interface PlanActionResponse {
  ok: boolean
  group_id: string
  coordinator_id: string
  mode: 'confirm' | 'direct' | 'modify'
  /** /direct 专属：是否已把 group config.auto_confirm 置 True。 */
  auto_confirm?: boolean
  /** /direct 专属：是否有驻留计划被一并唤醒派发。 */
  resumed_resident_plan?: boolean
}

/** /plan/modify 的响应（带修改后的完整计划）。 */
export interface PlanModifyResponse extends PlanActionResponse {
  plan: PlanStep[]
}

/** GET /api/groups/{id}/plan 的响应（当前驻留计划，PL-10 重连重拉）。 */
export interface PlanGetResponse {
  ok: boolean
  group_id: string
  coordinator_id: string
  plan: PlanStep[]
}

export const planApi = {
  /** 确认继续：唤醒驻留计划按原样派发。 */
  confirm: (groupId: string) =>
    http<PlanActionResponse>('POST', `/api/groups/${groupId}/plan/confirm`),
  /** 直接干：把 group 切到 auto_confirm=True 并（若有）唤醒驻留计划。 */
  directRun: (groupId: string) =>
    http<PlanActionResponse>('POST', `/api/groups/${groupId}/plan/direct`),
  /** 修改计划：patch 指定步骤后重广播 + 确认派发。 */
  modify: (groupId: string, steps: PlanModifyStep[]) =>
    http<PlanModifyResponse>('POST', `/api/groups/${groupId}/plan/modify`, { steps }),
  /** 拉取当前驻留计划（PL-10 WS 重连后重拉，对齐后端 _dispatch_plan 真源）。 */
  getPlan: (groupId: string) =>
    http<PlanGetResponse>('GET', `/api/groups/${groupId}/plan`),
}

// ── Task API ─────────────────────────────────────────────────

/** POST /api/tasks/{id}/stop 的响应（PL-11 停止任务）。 */
export interface TaskStopResponse {
  ok: boolean
  task_id: string
  /** true=当前 executing 的任务已被 cancel（_worker_task 中断，引擎回 idle）。 */
  executing: boolean
  /** true=队列/backlog 中的任务已打 cancelled 标记（_handle_task 将跳过执行）。 */
  queued: boolean
  group_id: string | null
  agent_id: string | null
  /** 人类可读结果文案，前端可直接 toast 提示。 */
  message: string
}

export const taskApi = {
  list: (groupId: string) => http<Task[]>('GET', '/api/tasks', undefined, { groupId }),
  get: (id: string) => http<Task | null>('GET', `/api/tasks/${id}`),
  create: (body: TaskCreatePayload) => http<Task>('POST', '/api/tasks', body),
  update: (id: string, body: Partial<TaskCreatePayload>) =>
    http<Task | null>('PUT', `/api/tasks/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/tasks/${id}`),
  ready: (groupId: string) => http<Task[]>('GET', '/api/tasks/ready', undefined, { groupId }),
  /** PL-11：停止任务。后端双保险——executing 的 cancel _worker_task，
   *  queued 的打 cancelled 标记；都不是则 200 no-op（可能已完成，非错误）。
   *  groupId 可选（tq_ 全局唯一），传了可缩小引擎扫描范围。 */
  stop: (id: string, groupId?: string) =>
    http<TaskStopResponse>('POST', `/api/tasks/${id}/stop`, undefined, groupId ? { groupId } : undefined),
}

// ── Message API ─────────────────────────────────────────────

export const messageApi = {
  listByGroup: (groupId: string, limit = 100) =>
    http<Message[]>('GET', '/api/messages', undefined, { groupId, limit: String(limit) }),
  /**
   * SC-08 /sessions：不带 groupId 一次拉所有群组的消息（后端 list_messages(groupId=None)
   * 不加 group 过滤，按 created_at 排序后取最近 limit 条）。前端按 group_id 聚合统计会话。
   *
   * 与 listByGroup 并存（不破坏后者）——listByGroup 是「拉某群消息流」单群语义，
   * listAll 是「跨群聚合统计」全量语义。传较大 limit（默认 1000）尽量覆盖所有会话的
   * 最后一条消息做预览；会话数极多时仍可能漏早期会话的最后消息（预览降级为无预览，
   * 但会话本身仍由 groupApi.list 列出，不会丢会话）。
   */
  listAll: (limit = 1000) =>
    http<Message[]>('GET', '/api/messages', undefined, { limit: String(limit) }),
  listByTask: (taskId: string, limit = 100) =>
    http<Message[]>('GET', `/api/messages/by-task/${taskId}`, undefined, { limit: String(limit) }),
  send: (body: MessageCreatePayload) => http<Message>('POST', '/api/messages', body),
  clearByGroup: (groupId: string) =>
    http<boolean>('DELETE', '/api/messages', undefined, { groupId }),
}

// ── Skill API ───────────────────────────────────────────────

export interface Skill {
  id: string
  name: string
  description: string | null
  source: 'builtin' | 'market' | 'custom' | string
  installed: boolean
  content: string | null
  tags: string[]
  // 阶段四·可执行技能 frontmatter（task31 起，纯文档技能为空数组）
  requires_tools?: string[]
  triggers?: string[]
  outputs?: string[]
  assets?: string[]
  mounted_to: string[]
  created_at: string
  updated_at: string
}

export interface SkillCreatePayload {
  name: string
  description?: string
  content?: string
  source?: string
  tags?: string[]
  // 阶段四·可执行技能 frontmatter（皆可选，默认空数组向后兼容）
  requires_tools?: string[]
  triggers?: string[]
  outputs?: string[]
}

/**
 * SK-10: 技能市场条目（来自内置市场 catalog + 可选远程 Hub，非本地入库技能）。
 *
 * 与 Skill 类型有意不同：市场条目是「可被发现待安装」的对象，尚未落本地库——
 *  - 用 `entry_id` 标识（catalog 条目形如 `catalog:db-migration`），而非 Skill 的 `id`；
 *  - `content` 可空（catalog 条目自带全文；远程条目可能仅带 source_url 待安装时拉取）；
 *  - 带 provenance 字段 `hub`/`author`/`version`/`source_url` 让用户区分来源，本地 Skill 无这些。
 *
 * 字段 snake_case 对齐后端 MarketEntry 模型与既有 api.ts 约定（Skill/GroupMember 同风格）。
 * SK-12 安装时用 entry_id 调 POST /api/skills/market/install 落库为本地 Skill（source=market）。
 */
export interface SkillMarketEntry {
  entry_id: string
  name: string
  description: string
  tags: string[]
  content: string | null
  hub: string
  author: string
  version: string
  source_url: string | null
}

export const skillApi = {
  list: () => http<Skill[]>('GET', '/api/skills'),
  get: (id: string) => http<Skill>('GET', `/api/skills/${id}`),
  create: (body: SkillCreatePayload) => http<Skill>('POST', '/api/skills', body),
  generate: (description: string) =>
    http<Skill>('POST', '/api/skills/generate', { description }),
  update: (id: string, body: SkillCreatePayload) =>
    http<Skill>('PUT', `/api/skills/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/skills/${id}`),
  mount: (id: string, agentId: string) =>
    http<AgentDefinition>('POST', `/api/skills/${id}/mount`, { agentId }),
  unmount: (id: string, agentId: string) =>
    http<AgentDefinition>('POST', `/api/skills/${id}/unmount`, { agentId }),
  /**
   * SK-05: 上传 SKILL.md 文件（或技能目录 zip）作为技能入库。multipart/form-data：
   * file（文件本体）+ name/description/source/tags（元数据 form 字段）。
   *
   * 不走通用 http<T> —— http 设 Content-Type: application/json 并
   * JSON.stringify body；multipart 必须用 FormData 让浏览器自动设带 boundary
   * 的 multipart Content-Type（手动设会缺 boundary 导致后端解析失败，与
   * downloadFile 同理：二进制/非 JSON 交互需独立 fetch）。
   *
   * 两种上传形态（task34 起后端按文件扩展名自动分派，签名不变·加性兼容）：
   *  - 单文件 SKILL.md：file content → Skill.content（原行为）
   *  - 技能目录 zip（.zip）：解包 SKILL.md→content + scripts/+templates/→assets
   *    （Claude Skills 一技能一目录自包含布局）
   *
   * tags 是 string[]，后端 form 字段扁平需 JSON 编码字符串（后端 json.loads 解析）；
   * 空数组不 append（后端缺省 []）。name 缺省时后端回退文件 stem（去 .md/.zip 扩展名），
   * 故 name/description/source/tags 皆可选，仅 file 必填。
   */
  upload: async (
    file: File,
    opts?: { name?: string; description?: string; source?: string; tags?: string[] },
  ): Promise<Skill> => {
    const fd = new FormData()
    fd.append('file', file)
    if (opts?.name) fd.append('name', opts.name)
    if (opts?.description) fd.append('description', opts.description)
    if (opts?.source) fd.append('source', opts.source)
    if (opts?.tags && opts.tags.length > 0) {
      fd.append('tags', JSON.stringify(opts.tags))
    }
    const resp = await fetch(`${API_BASE}/api/skills/upload`, {
      method: 'POST',
      body: fd,
      // 不设 headers：浏览器自动加 multipart/form-data; boundary=...
    })
    if (!resp.ok) {
      throw new Error(`API ${resp.status}: ${await resp.text()}`)
    }
    const text = await resp.text()
    return (text ? JSON.parse(text) : null) as Skill
  },
  /**
   * SK-10: 搜索技能市场（内置市场 catalog + 可选远程 Hub overlay）。
   *
   * GET /api/skills/market?q=&limit=。空 q 返回全部（受 limit 封顶）。后端
   * search_market 始终返回列表（remote Hub 失败静默回退 catalog-only，永不抛错）。
   *
   * 搜索语义与本地 SK-09 一致：大小写不敏感子串匹配 name/description/tags。
   * 返回 SkillMarketEntry[]（含 entry_id 供 SK-12 安装、content 可空）。
   *
   * limit 默认 50，与后端 _DEFAULT_LIMIT 对齐；上限 200（后端 le 校验，越界 422）。
   */
  searchMarket: (q: string = '', limit: number = 50) =>
    http<SkillMarketEntry[]>('GET', '/api/skills/market', undefined, {
      q,
      limit: String(limit),
    }),
  /**
   * SK-12: 一键安装市场技能到本地技能库。
   *
   * POST /api/skills/market/install body={entry_id}。后端按 entry_id 解析市场条目：
   *  - catalog 条目自带 content 全文，直接落库；
   *  - remote 条目仅 source_url 时后端 best-effort 拉取正文（失败 409）；
   *  - 未知/已下架 entry_id → 404；空 entry_id → 400。
   * 落库后返回本地 Skill（source="market"），与 list/create 同类型，可直接进「我的技能」。
   *
   * 仅传 entry_id 而非全文——content 真源在后端（skill_hub catalog / remote Hub），
   * 前端传全文会与服务端漂移且 remote 条目前端根本拿不到 content。entry_id 路径统一
   * 覆盖 catalog+remote 两种来源。返回 Skill 复用既有 http<Skill>（标准 JSON 往返）。
   */
  installMarket: (entryId: string) =>
    http<Skill>('POST', '/api/skills/market/install', { entry_id: entryId }),
  /**
   * 阶段四·task38: 运行一个可执行技能（带受控工具 + 沙箱），流式回传执行过程。
   *
   * POST /api/skills/{id}/run body={prompt?, max_turns?}。后端起临时 agent（技能
   * content 作 system prompt + requires_tools 受控工具绑该技能沙箱 workspace），
   * 跑 create_react_agent agentic loop，产物落 output/。
   *
   * 返回 SSE 流（text/event-stream），每事件一行 `data: <json>`：
   *   {kind: 'token'|'tool_start'|'tool_end'|'think'|'answer'|'log', content, data?}
   *   ... 最后 {kind: 'done', ok, run_id, output_path, products?, error?}
   *
   * 不走通用 http<T>（设 JSON Content-Type）——SSE 是流式响应，需 fetch + getReader
   * 逐 chunk 解析（与 upload/downloadFile 同属非 JSON 交互独立 fetch）。回调式 onEvent
   * 消费每条 SSE 事件，返回一个 stop 函数供调用方中止流。
   *
   * 安全契约：仅 requires_tools 非空技能可运行（纯文档技能后端 400）；不污染群聊
   * GroupState（独立执行，非群图回合）。
   */
  run: (
    id: string,
    onEvent: (ev: SkillRunEvent) => void,
    opts?: { prompt?: string; maxTurns?: number },
  ): (() => void) => {
    const controller = new AbortController()
    void (async () => {
      let resp: Response
      try {
        resp = await fetch(`${API_BASE}/api/skills/${id}/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify({ prompt: opts?.prompt, max_turns: opts?.maxTurns }),
          signal: controller.signal,
        })
      } catch (e) {
        onEvent({ kind: 'done', ok: false, run_id: '', output_path: null, error: String(e) })
        return
      }
      if (!resp.ok || !resp.body) {
        const text = await resp.text().catch(() => '')
        onEvent({
          kind: 'done', ok: false, run_id: '', output_path: null,
          error: `API ${resp.status}: ${text}`,
        })
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          // SSE 事件以 \n\n 分隔
          let idx: number
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            const raw = buf.slice(0, idx)
            buf = buf.slice(idx + 2)
            for (const line of raw.split('\n')) {
              const s = line.trim()
              if (s.startsWith('data: ')) {
                try {
                  onEvent(JSON.parse(s.slice(6)) as SkillRunEvent)
                } catch {
                  /* ignore malformed SSE line */
                }
              }
            }
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          onEvent({ kind: 'done', ok: false, run_id: '', output_path: null, error: String(e) })
        }
      }
    })()
    return () => controller.abort()
  },
}

/**
 * 阶段四·task39: 技能运行 SSE 事件类型（skillApi.run 回调参数）.
 *
 * 与后端 api/skills.py run_skill 的 SSE 事件一一对应：
 *  - token/tool_start/tool_end/think/answer/log：执行过程事件（content + 可选 data）
 *  - done：收尾事件（ok + run_id + output_path + 可选 products/error）
 */
export interface SkillRunEvent {
  kind: 'token' | 'tool_start' | 'tool_end' | 'think' | 'answer' | 'log' | 'done'
  content?: string
  data?: Record<string, unknown> | null
  // done 事件专属字段
  ok?: boolean
  run_id?: string
  output_path?: string | null
  products?: string[]
  error?: string
}

// ── MCP API (MC-01~06 MCP 工具集成) ──────────────────────────

/**
 * MC-01: MCP 连接（外部工具源，PL-07 执行期 agent 可调）。
 *
 * 两种传输（MC-02）：
 *  - stdio：spawn 本地命令（command + args + env）——如 `npx -y @modelcontextprotocol/server-filesystem`；
 *  - sse：连远程 SSE 端点（url + headers）。
 *
 * 连接按 id 挂载到 Agent（AgentDefinition.mounted_mcp），执行期引擎经
 * langchain-mcp-adapters MultiServerMCPClient 解析为 LangChain 工具。
 *
 * 字段 snake_case 对齐后端 McpConnection 模型（backend/models/mcp.py）与
 * 既有 api.ts 约定。stdio 专属字段（command/args/env）与 sse 专属字段
 * （url/headers）在同类型上并存——transport 决定哪些字段生效，前端表单
 * 按 transport 切换显隐（MC-02）。
 */
export interface McpConnection {
  id: string
  name: string
  transport: 'stdio' | 'sse'
  // stdio 传输
  command: string
  args: string[]
  env: Record<string, string> | null
  // sse 传输
  url: string
  headers: Record<string, unknown> | null
  enabled: boolean
  created_at: string
  updated_at: string
}

/**
 * MC-02: 创建/更新 MCP 连接的 payload（transport + 该传输所需字段）。
 *
 * stdio 必填 command（args/env 可选）；sse 必填 url（headers 可选）。
 * 后端 McpConnectionCreatePayload 全字段可选 + extra=allow，前端按 transport
 * 传相关字段即可（不相关字段传 undefined 不经 http 序列化）。
 */
export interface McpConnectionCreatePayload {
  name: string
  transport: 'stdio' | 'sse'
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  headers?: Record<string, unknown>
  enabled?: boolean
}

/** GET /api/mcp/{id}/tools 返回的工具预览项（MCP 自省，前端展示用）。 */
export interface McpToolInfo {
  name: string
  description?: string
  inputSchema?: Record<string, unknown>
  [key: string]: unknown
}

export const mcpApi = {
  /** MC-01: 列出全部 MCP 连接（按 created_at 排序）。 */
  list: () => http<McpConnection[]>('GET', '/api/mcp'),
  /** MC-01: 单读 MCP 连接（404 返 null）。 */
  get: (id: string) => http<McpConnection | null>('GET', `/api/mcp/${id}`),
  /** MC-02: 创建 MCP 连接（transport + 该传输字段），落库返回 McpConnection。 */
  create: (body: McpConnectionCreatePayload) =>
    http<McpConnection>('POST', '/api/mcp', body),
  /** 更新 MCP 连接（全量替换 payload 字段）。 */
  update: (id: string, body: McpConnectionCreatePayload) =>
    http<McpConnection | null>('PUT', `/api/mcp/${id}`, body),
  /** MC-04: 删除 MCP 连接（后端级联从所有 agent.mounted_mcp 移除引用）。 */
  delete: (id: string) => http<boolean>('DELETE', `/api/mcp/${id}`),
  /** MC-03: 启用连接（set_mcp_enabled True）。 */
  enable: (id: string) => http<McpConnection | null>('POST', `/api/mcp/${id}/enable`),
  /** MC-03: 禁用连接（set_mcp_enabled False，禁用后 tools 预览返空）。 */
  disable: (id: string) => http<McpConnection | null>('POST', `/api/mcp/${id}/disable`),
  /**
   * MC-06: 挂载 MCP 连接到 Agent（agent.mounted_mcp 加 mcp_id）。
   *
   * body={agentId}（camelCase，与 skillApi.mount 同风格，后端 MountBody 用 agentId）。
   * 返回更新后的 AgentDefinition（mounted_mcp 含新 mcp_id），执行期引擎据此加载工具。
   */
  mount: (id: string, agentId: string) =>
    http<AgentDefinition | null>('POST', `/api/mcp/${id}/mount`, { agentId }),
  /**
   * MC-06: 从 Agent 卸载 MCP 连接（agent.mounted_mcp 移除 mcp_id）。
   *
   * body={agentId}，返回更新后的 AgentDefinition（mounted_mcp 不再含 mcp_id）。
   */
  unmount: (id: string, agentId: string) =>
    http<AgentDefinition | null>('POST', `/api/mcp/${id}/unmount`, { agentId }),
  /**
   * MC-01: 预览 MCP 连接暴露的工具列表（自省，前端展示用）。
   *
   * 后端 list_mcp_tools([mcp_id]) 经 langchain-mcp-adapters 加载连接自省工具；
   * 只加载 enabled 的连接，禁用连接返回空列表。返回 McpToolInfo[]。
   */
  tools: (id: string) =>
    http<McpToolInfo[]>('GET', `/api/mcp/${id}/tools`),
}

// ── Scheduled Task API (M8: PRD 3.5 定时任务 TM-01~07) ────────────

/** 调度类型：cron（cron 表达式）/ interval（定间隔秒数）/ once（一次性定时）。 */
export type ScheduleType = 'cron' | 'interval' | 'once'

/**
 * ScheduledTask：定时任务实体（后端 models/scheduled_task.py 镜像，snake_case）。
 *
 * fire 时 scheduler 向 agent 的 inbox push 一个任务，复用常驻引擎 agentic loop，
 * 即定时执行与交互派发走同一条智能体回路。ScheduledTaskRun 记录每次执行历史。
 */
export interface ScheduledTask {
  id: string
  name: string
  /** 每次调度触发时向 agent 发送的 prompt */
  content: string
  /** 目标 agent id */
  agent_id: string
  /** 调度类型：cron | interval | once */
  schedule_type: ScheduleType
  /** cron 表达式（schedule_type=cron 时生效，如 "0 8 * * *"） */
  cron: string
  /** 间隔秒数（schedule_type=interval 时生效，如 3600） */
  interval_seconds: number
  /** ISO8601 触发时刻（schedule_type=once 时生效） */
  run_at: string
  /** agent 所属群组 id（scheduler 据此 push 到正确引擎 inbox） */
  group_id: string
  enabled: boolean
  created_at: string
  updated_at: string
}

/**
 * 创建/更新定时任务 payload（后端 ScheduledTaskCreatePayload 镜像）。
 *
 * name/content/agent_id/group_id 必填；schedule_type 三选一，对应字段
 * （cron/interval_seconds/run_at）按 schedule_type 传相关值，不相关字段可不传
 * （后端全可选，前端按类型表单收集）。enabled 默认 True。
 */
export interface ScheduledTaskCreatePayload {
  name: string
  content: string
  agent_id: string
  group_id: string
  schedule_type: ScheduleType
  cron?: string
  interval_seconds?: number
  run_at?: string
  enabled?: boolean
}

/**
 * ScheduledTaskRun：单次执行历史记录（TM-07 执行历史视图）。
 *
 * status 流转：pending → running → success | failed。
 */
export interface ScheduledTaskRun {
  id: string
  scheduled_task_id: string
  status: 'pending' | 'running' | 'success' | 'failed'
  result: string | null
  started_at: string
  finished_at: string
}

/** TM-01: 定时任务绑定——1:1 映射后端 /api/scheduled-tasks 路由表。 */
export const scheduledTaskApi = {
  /** TM-01: 列出全部定时任务。 */
  list: () => http<ScheduledTask[]>('GET', '/api/scheduled-tasks'),
  /** TM-01: 单读定时任务（404 返 null）。 */
  get: (id: string) => http<ScheduledTask | null>('GET', `/api/scheduled-tasks/${id}`),
  /**
   * TM-02/03: 创建定时任务。后端 create 后若 enabled 自动 add_job 注册调度。
   * 返回落库的 ScheduledTask（含 id/created_at）。
   */
  create: (body: ScheduledTaskCreatePayload) =>
    http<ScheduledTask>('POST', '/api/scheduled-tasks', body),
  /** 更新定时任务（后端 remove_job 后按新配置重建 job）。 */
  update: (id: string, body: ScheduledTaskCreatePayload) =>
    http<ScheduledTask | null>('PUT', `/api/scheduled-tasks/${id}`, body),
  /** TM-06: 删除定时任务（后端先 remove_job 再删库）。 */
  delete: (id: string) => http<boolean>('DELETE', `/api/scheduled-tasks/${id}`),
  /** TM-04: 立即执行（强制触发，即使 paused 也跑，跳过调度直接 fire）。 */
  runNow: (id: string) => http<{ ok: boolean }>('POST', `/api/scheduled-tasks/${id}/run`),
  /** TM-05: 暂停（set_enabled(False) + remove_job）。返回更新后的任务。 */
  pause: (id: string) => http<ScheduledTask | null>('POST', `/api/scheduled-tasks/${id}/pause`),
  /** TM-05: 恢复（set_enabled(True) + add_job 重新注册调度）。返回更新后的任务。 */
  resume: (id: string) => http<ScheduledTask | null>('POST', `/api/scheduled-tasks/${id}/resume`),
  /** TM-07: 执行历史列表（按时间倒序的 ScheduledTaskRun[]）。 */
  history: (id: string) => http<ScheduledTaskRun[]>('GET', `/api/scheduled-tasks/${id}/runs`),
}

// ── 实时事件：WebSocket ──────────────────────────────────

export interface BusEventData {
  id: string
  group_id: string
  task_id: string | null
  sender_id: string
  receiver_id: string
  type: string
  content: string | null
  data: unknown
  timestamp: string
}

// ── System API (M11: agent status) ────────────────────────────

export const systemApi = {
  listStatus: (groupId: string) => http<AgentStatusInfo[]>('GET', `/api/status/${groupId}`),
  // SA-03: 一次拉全所有群组所有 agent 状态，避免前端 N+1 轮询
  // 对应后端 GET /api/status（无 group_id 段），返回 {group_id: AgentStatusInfo[]}
  listAllStatus: () => http<Record<string, AgentStatusInfo[]>>('GET', `/api/status`),
}

/**
 * LLM 配置（CF-04 GET /api/config 返回的脱敏形态）。
 *
 * 后端 config.get_config_public() 把真实 api_key 换成首尾各 3 字符的 mask 预览，
 * 原始密钥永不离开进程；has_key 让 UI 显示「已配置」而不暴露密钥本身。
 * provider/base_url 由环境变量驱动（只读），model 可经 PUT 热切换（set_config 写回
 * os.environ，下次 engine invoke 生效，无需重启——CF-05）。
 */
export interface LlmConfig {
  provider: string
  model: string
  base_url: string
  /** 脱敏密钥预览（首 3 + 尾 3），非原始密钥。 */
  api_key: string
  has_key: boolean
  temperature: number
  max_tokens: number
}

// ── Config API (CF-04: LLM 模型查看/热切换) ───────────────────

export const configApi = {
  /** GET /api/config：当前 LLM 配置（密钥脱敏）。 */
  get: () => http<LlmConfig>('GET', '/api/config'),
  /** PUT /api/config body={model}：热切换模型（写回 os.environ，下次 invoke 生效，无需重启）。
   *  model 为空时 no-op（echo 当前状态）。返回脱敏的 post-write 配置。 */
  put: (model: string) => http<LlmConfig>('PUT', '/api/config', { model }),
}

// ── LLM Provider API (多模型服务商配置) ──────────────────────

/**
 * LLM 服务商配置（后端 GET /api/providers 返回的脱敏形态）。
 *
 * 后端 store/crud._provider_to_model 把真实 api_key 换成首尾各 3 字符的 mask 预览，
 * 原始密钥永不离开进程；has_key 让 UI 显示「已配置」而不暴露密钥本身。
 * is_active 标识当前生效的 provider（同一时刻只有一个 active）。
 */
/**
 * 一个服务商拥有的某个模型条目（多模型目录）。
 *
 * 后端 models.llm_provider.LlmModel 的 TS 镜像。model_id 是发给上游
 * /chat/completions 的 `model` 字段值；display_name 是 UI 显示名；能力元数据
 * （context_window / 4 个 supports 布尔）让 UI / 引擎判断模型适用场景；
 * is_default 标识该 provider 当前生效的模型（单 default 不变量，每个 provider
 * 至多一个 is_default=true）。
 *
 * active model 解析：is_default → 匹配 provider.model 列 → catalog 首个 →
 * provider.model 列（见后端 config.select_active_model 5 级 fallback）。
 */
export interface LlmModel {
  /** 发给上游的模型 id（如 "deepseek-chat" / "gpt-4o"）。 */
  model_id: string
  /** UI 显示名（未配置时 fallback 到 model_id）。 */
  display_name: string
  /** 上下文窗口大小（token 数）；0 = 未知。 */
  context_window: number
  /** 是否支持 function calling / tool use。 */
  supports_function_calling: boolean
  /** 是否支持视觉输入（图片）。 */
  supports_vision: boolean
  /** 是否支持流式输出（SSE）。 */
  supports_streaming: boolean
  /** 是否该 provider 的当前默认模型（单 default 不变量）。 */
  is_default: boolean
}

export interface LlmProvider {
  id: string
  name: string
  provider: string
  /** 旧扁平 model 列（向后兼容）；active model 由 models 优先解析，fallback 到此列。 */
  model: string
  base_url: string
  /** 脱敏密钥预览（首 3 + 尾 3），非原始密钥。 */
  api_key: string
  has_key: boolean
  temperature: number
  max_tokens: number
  is_active: boolean
  created_at: string
  updated_at: string
  /** 多模型目录（provider 拥有 N 个模型，恰好 1 个 is_default）。空数组 = legacy 模式。 */
  models: LlmModel[]
  // ── 连接级配置（作用于端点，所有模型共享）──
  /** API 版本（Anthropic 等需 x-api-version 的端点用）；空串 = 未配置。 */
  api_version: string
  /** OpenAI 组织 id（部分端点用 org 头路由计费）；空串 = 未配置。 */
  organization: string
  /** 自定义请求头（合并到 Authorization 之外）；null = 不附加。 */
  extra_headers: Record<string, string> | null
  /** 单请求超时秒数（默认 120）。 */
  request_timeout: number
  /** 失败重试次数（默认 2）。 */
  max_retries: number
  /** HTTP 代理地址；空串 = 直连。 */
  proxy: string
}

/**
 * 创建/更新服务商的 payload。api_key 可选——更新时留空表示「不修改」。
 *
 * 多模型目录 + 连接级字段镜像 LlmProvider。全部 optional（`?`）以支持
 * partial update——更新时只传改动的字段，未传字段保持不变（后端
 * exclude_unset=True 只落显式提供的字段）。models 特别区分：
 * - undefined（不传）= 不动 catalog；
 * - []（显式空数组）= 清空 catalog；
 * - 非空数组 = 替换 catalog（后端校验单 default，多 default 时保留首个）。
 */
export interface LlmProviderPayload {
  name: string
  provider?: string
  model?: string
  base_url?: string
  api_key?: string
  temperature?: number
  max_tokens?: number
  is_active?: boolean
  /** 模型目录：undefined 不动 / [] 清空 / 非空替换。 */
  models?: LlmModel[]
  // ── 连接级配置（未传 = 不变更）──
  api_version?: string
  organization?: string
  extra_headers?: Record<string, string> | null
  request_timeout?: number
  max_retries?: number
  proxy?: string
}

export const providerApi = {
  /** GET /api/providers：列出所有服务商（密钥脱敏）。 */
  list: () => http<LlmProvider[]>('GET', '/api/providers'),
  /** POST /api/providers：新增服务商。is_active=true 时自动设为当前。 */
  create: (payload: LlmProviderPayload) => http<LlmProvider>('POST', '/api/providers', payload),
  /** PUT /api/providers/{id}：更新服务商。api_key 留空不修改。 */
  update: (id: string, payload: LlmProviderPayload) =>
    http<LlmProvider>('PUT', `/api/providers/${id}`, payload),
  /** DELETE /api/providers/{id}：删除服务商。删 active 时自动选下一个。 */
  remove: (id: string) => http<{ ok: boolean }>('DELETE', `/api/providers/${id}`),
  /** POST /api/providers/{id}/activate：设为当前服务商。 */
  activate: (id: string) => http<LlmProvider>('POST', `/api/providers/${id}/activate`),
  /** POST /api/providers/{id}/test：探测连通性（UI「测试连通」按钮）。
   *  发最小 /chat/completions，返回 {ok, latency_ms, error, status_code}，永不 500。 */
  test: (id: string) =>
    http<{ ok: boolean; latency_ms: number; error: string; status_code: number | null }>(
      'POST',
      `/api/providers/${id}/test`,
    ),
  /** GET /api/providers/{id}/models：拉取上游模型目录（UI「拉取模型」按钮）。
   *  GET {base_url}/models 归一化为 LlmModel[]（首个 is_default）。返回的 models
   *  不持久化——前端接受后调 update(id, {models}) 保存。 */
  fetchModels: (id: string) =>
    http<{ ok: boolean; models: LlmModel[]; error: string; status_code: number | null }>(
      'GET',
      `/api/providers/${id}/models`,
    ),
  /** GET /api/providers/catalog：预设服务商目录（UI「预设选择器」）。
   *  返回 7 个预设（OpenAI/DeepSeek/Anthropic/Kimi/GLM/Qwen/Ollama），每个含
   *  base_url + 默认连接配置 + 预置 models + note。无 api_key（用户填）。 */
  catalog: () => http<ProviderPreset[]>('GET', '/api/providers/catalog'),
}

// ── Provider preset catalog (多模型服务商 · 预设目录) ────────────────────

/**
 * 预设服务商模板（GET /api/providers/catalog 返回项）。
 *
 * 后端 llm_provider_catalog.ProviderPreset 的 TS 镜像。预设是「编辑器加载的模板」
 * 而非「可直接创建的行」——不含 api_key/is_active/id/timestamps（这些由
 * crud.create_provider 分配）。用户选预设后填 api_key，再 POST /api/providers 创建。
 */
export interface ProviderPreset {
  /** 稳定 slug（如 "openai" / "deepseek"，catalog 路由键）。 */
  slug: string
  name: string
  provider: string
  base_url: string
  // ── 默认连接级配置（作用于端点）──
  api_version: string
  organization: string
  extra_headers: Record<string, string> | null
  request_timeout: number
  max_retries: number
  proxy: string
  // ── 默认采样参数 ──
  temperature: number
  max_tokens: number
  /** 预置模型目录（含能力元数据，恰好 1 个 is_default）。 */
  models: LlmModel[]
  /** UI 提示（如「需自备 API Key」）。 */
  note: string
}

// ── Slash helper API (BE-01: 后端代解析前端无法独立完成的 slash 命令) ────

/** 单条工具预览（/tools 聚合结果项，name + 截断 description）。 */
export interface ToolPreviewItem {
  name: string
  description: string
}

/** POST /api/slash command=tools 的响应（BE-01 _slash_tools）。 */
export interface SlashToolsResult {
  ok: boolean
  command: string
  /** 失败时后端给的中文可读错误（MCP 加载失败等）。 */
  error?: string
  /** agent_id（请求时传的，回显；未传则为 null/undefined）。 */
  agent_id?: string | null
  group_id?: string | null
  tools: {
    /** 内置工具（engine.tools.tools_for_group，workspace 无关）。 */
    internal: ToolPreviewItem[]
    /** 各 mounted_mcp 暴露的工具（langchain-mcp-adapters 自省，flattened）。 */
    mcp: ToolPreviewItem[]
  }
  total: number
}

export const slashApi = {
  /** POST /api/slash command=tools：聚合内置工具 + agent 已挂载 MCP 工具（BE-01）。
   *  agentId/groupId 可选——agentId 决定查哪个 agent 的 mounted_mcp，groupId 决定 workspace
   *  绑定（内置工具 closure 捕获 group_id）。两者皆空仍返回内置 roster（workspace 无关）。
   *  undefined 字段在 http() 内不被序列化，故不传 agent_id/group_id 时后端用默认 None。 */
  tools: (agentId?: string, groupId?: string) =>
    http<SlashToolsResult>('POST', '/api/slash', {
      command: 'tools',
      ...(agentId ? { agent_id: agentId } : {}),
      ...(groupId ? { group_id: groupId } : {}),
    }),
}

// ── M11 黑盒透明化类型 ────────────────────────────────────

export interface TraceEvent {
  id: string
  kind: string
  agentId: string
  agentName: string
  taskId: string | null
  content: string | null
  data: any
  timestamp: number
}

export interface AgentStatusInfo {
  id: string
  name: string
  role: string
  status: 'idle' | 'executing' | 'offline'
  current_task_id: string | null
}

export interface PlanStep {
  step: number
  agent_id: string
  agent_name: string
  instruction: string
  depends_on: number[]
  status: string
  result?: string | null
  task_id?: string | null
}

/** 协调者/worker 流式回复的运行统计——elapsed_ms/tokens/model/reasoning_tokens +
 *  phase（仅流式 WS 事件用，持久化 agent_reply.data 无此 key）。
 *
 *  两个来源同形（B18 抽 parseStats 单一真源前是两处重复 Number() 守卫）：
 *   - WS ``coordinator_stats`` 事件 data（useBusEvent coordStats[reply_id]）——含 phase，
 *     streaming/done 两种，elapsed_ms/tokens 在 streaming 阶段可为 0（节流中间值）。
 *   - 持久化 agent_reply.data（ChatPanel 定稿气泡 extractCoordStats）——无 phase，
 *     elapsed_ms>0 是真实值（后端 time.monotonic 墙钟，见 vg2 [C7]）。
 *  字段集：``CoordStats`` 是超集（含 phase），``FinalizedStats`` 是子集（无 phase）。
 *  parseStats 默认不强求 elapsed_ms>0（流式中间值 0 合法），strictElapsed 守卫仅
 *  定稿气泡用（无 elapsed_ms 的 announce 类回复返 null 不渲染状态行，见 A8/vg2）。
 */
export interface CoordStats {
  elapsed_ms: number
  tokens: number
  phase: string
  model?: string
  reasoning_tokens?: number
}

/** 定稿气泡的统计子集（无 phase——持久化 agent_reply.data 不带 phase）。
 *  ChatPanel extractCoordStats 返回此形（去掉 phase 后给定稿状态行用）。 */
export interface FinalizedStats {
  elapsed_ms: number
  tokens: number
  model?: string
  reasoning_tokens?: number
}

/** B18 共享解析器：从 raw data（WS 事件 dd 或持久化 message.data）提取统计字段，
 *  统一 Number()/Number.isFinite/typeof string 守卫（原 useBusEvent coordinator_stats
 *  分支与 ChatPanel.extractCoordStats 两处重复，抽此单一真源）。
 *
 *  字段守卫口径（与原两处逐字对齐，行为零变）：
 *   - elapsed_ms: ``Number(raw.elapsed_ms ?? 0)`` —— WS 路径传 0 兜底（streaming 中间
 *     值合法）；``strictElapsed=true`` 时非有限/<=0 返 null（定稿气泡用，见 A8/vg2）。
 *   - tokens: ``Number(raw.tokens ?? 0)``，非有限降 0（原 useBusEvent 隐式，原 ChatPanel
 *     显式 ``Number.isFinite(tokens) ? tokens : 0``——口径统一为显式 finite? :0）。
 *   - model: 仅非空 string 才取（``typeof === 'string' && raw.model``）。
 *   - reasoning_tokens: ``Number(raw.reasoning_tokens ?? 0)``，finite & >0 才取，否则
 *     undefined（不渲染「含 N 推理」段，原两处同口径）。
 *   - phase: ``String(raw.phase ?? 'streaming')`` —— WS 路径默认 streaming（done 时
 *     后端显式传 'done'）；定稿气泡不读 phase（``withPhase=false`` 不返回此 key）。
 *
 *  ``withPhase``（默认 true）: WS 流式路径返回 ``CoordStats``（含 phase）；定稿气泡路径
 *  传 ``false`` 返回 ``FinalizedStats``（无 phase）——同一函数两种返回形，对应两个调用方。
 *  ``strictElapsed``（默认 false）: true 时 elapsed_ms 非有限/<=0 返 null（定稿气泡用，
 *  防渲染「0 耗时」假状态行——announce 类回复无 elapsed_ms 应不渲染状态行）。
 *
 *  返回 null 的两种情况：① raw 非 object（WS 事件 data 缺失/异常）；② strictElapsed 且
 *  elapsed_ms 非有限/<=0。其他情况必返对象（即便 tokens=0 / model=undefined 也返，
 *  流式阶段这些是合法中间值）。 */
export function parseStats(
  raw: unknown,
  opts?: { withPhase?: boolean; strictElapsed?: boolean },
): CoordStats | FinalizedStats | null {
  const withPhase = opts?.withPhase ?? true
  const strictElapsed = opts?.strictElapsed ?? false
  // B20：null-guard+类型守卫走 safeRecord 单一真源（原 if (!raw || typeof raw !== 'object')）。
  const dd = safeRecord(raw)
  if (!dd) return null

  const elapsedMsNum = Number(dd['elapsed_ms'] ?? 0)
  const elapsedMs = Number.isFinite(elapsedMsNum) ? elapsedMsNum : 0
  if (strictElapsed && (!Number.isFinite(elapsedMsNum) || elapsedMs <= 0)) return null

  const tokensNum = Number(dd['tokens'] ?? 0)
  const tokens = Number.isFinite(tokensNum) ? tokensNum : 0

  const modelRaw = dd['model']
  const model = typeof modelRaw === 'string' && modelRaw ? modelRaw : undefined

  const reasoningTokensNum = Number(dd['reasoning_tokens'] ?? 0)
  const reasoning_tokens =
    Number.isFinite(reasoningTokensNum) && reasoningTokensNum > 0
      ? reasoningTokensNum
      : undefined

  const base = { elapsed_ms: elapsedMs, tokens, model, reasoning_tokens }
  if (!withPhase) return base as FinalizedStats
  const phase = String(dd['phase'] ?? 'streaming')
  return { ...base, phase } as CoordStats
}

/** B20 共享 null-guard+类型守卫入口：把 ``unknown`` data（WS 事件 d.data / 持久化
 *  message.data / TraceEvent.data）归一为 ``Record<string, unknown> | null``。
 *
 *  原三个 extractor（ChatPanel extractCoordStats / extractCoordReasoning /
 *  extractFinalizedArtifacts）各自重复 ``if (!data) return ...`` + ``typeof data !==
 *  'object'`` 守卫——口径虽相同但散在三处，任一处漂移就守卫不一致。抽此单一真源：
 *  ``unknown`` → null（非 object / null / undefined）或 ``Record<string, unknown>``
 *  （object）。数组本不是 record（extractFinalizedArtifacts 的 data.artifact manifest
 *  可能是数组？实测 bus.py emit_task_completed 的 data.artifact 是 dict {files:[...]}`，
 *  数组走 safeRecord 返 null 是对的——artifact manifest 非对象应返空）。
 *
 *  与 parseStats 关系：parseStats 内部已用同一守卫（``if (!raw || typeof raw !==
 *  'object') return null``）——B18 抽 parseStats 时已固化此守卫。B20 抽 safeRecord
 *  让 parseStats 复用（守卫单一真源），同时 extractCoordReasoning / extractFinalizedArtifacts
 *  也复用——四个 extractor 入口统一走 safeRecord。
 *
 *  返回 ``Record<string, unknown> | null``：调用方用 ``if (!dd) return <default>`` 兜底，
 *  ``dd`` 已是 narrowed record（TS 类型收窄，无需再 ``as Record<string, unknown>``）。 */
export function safeRecord(data: unknown): Record<string, unknown> | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  return data as Record<string, unknown>
}

/** 监听某群组的总线事件，返回取消监听函数（与旧 UnlistenFn 兼容）。
 *  断线自动重连（指数退避，最多 5 次），保证长任务期间 WS 不丢。
 *
 *  PL-10：重连成功后调 `onReconnect` 通知上层重拉历史。
 *  - 首次连接只 resolve Promise（上层初始数据加载由其自己的 seeding effect 负责，
 *    不走 onReconnect，避免首连重复触发重拉）。
 *  - 断后重连（非首次 onopen）才调 onReconnect——此时连接曾中断，WS 期间错过的
 *    事件（状态迁移/计划/消息）上层状态已过期，需重拉历史补齐。
 *  - onReconnect 是可选的：现有两参调用方零回归；useBusEvent 传第三个参挂真实重拉。
 *  - onReconnect 异常被吞掉，避免回调 throw 干扰 WS 生命周期。 */
export function onBusEvent(
  groupId: string,
  callback: (data: BusEventData) => void,
  onReconnect?: () => void,
): Promise<() => void> {
  let ws: WebSocket | null = null
  let closed = false
  let retry = 0
  // 区分首次连接 vs 断后重连：首次 onopen resolve Promise，后续 onopen 调 onReconnect。
  // 不用 retry 计数判断（retry 在 onopen 时已重置为 0，无法区分首连与重连）。
  let firstOpen = true
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  const MAX_RETRIES = 5

  const connect = (resolve?: (fn: () => void) => void) => {
    ws = new WebSocket(`${API_BASE.replace(/^http/, 'ws')}/ws/bus/${groupId}`)

    ws.onopen = () => {
      retry = 0
      if (firstOpen) {
        // 首次连接：解除 await onBusEvent(...)，保持原契约。
        firstOpen = false
        if (resolve) resolve(unlisten)
      } else if (onReconnect) {
        // 断后重连：连接曾中断，上层状态可能过期，通知重拉历史补齐缺漏事件。
        try {
          onReconnect()
        } catch {
          /* 回调异常不影响 WS 生命周期，吞掉 */
        }
      }
    }
    ws.onmessage = (event) => {
      try {
        callback(JSON.parse(event.data))
      } catch {
        /* ignore parse errors */
      }
    }
    ws.onclose = () => {
      if (closed) return
      if (retry < MAX_RETRIES) {
        const delay = Math.min(1000 * 2 ** retry, 16000) // 1s,2s,4s,8s,16s
        retry += 1
        reconnectTimer = setTimeout(() => connect(), delay)
      }
    }
    ws.onerror = () => {
      ws?.close() // trigger onclose → reconnect
    }
  }

  const unlisten = () => {
    closed = true
    if (reconnectTimer) clearTimeout(reconnectTimer)
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      ws.close()
    }
  }

  return new Promise((resolve) => connect(resolve))
}
