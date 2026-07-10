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
  system_prompt?: string
  model?: string
  max_turns?: number
  description?: string | null
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
}

export interface Group {
  id: string
  name: string
  coordinator_id: string
  description: string | null
  status: string
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
  update: (id: string, body: Partial<AgentCreatePayload>) =>
    http<AgentDefinition | null>('PUT', `/api/agents/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/agents/${id}`),
}

// ── Group API ────────────────────────────────────────────────

export const groupApi = {
  list: () => http<Group[]>('GET', '/api/groups'),
  get: (id: string) => http<Group | null>('GET', `/api/groups/${id}`),
  create: (body: GroupCreatePayload) => http<Group>('POST', '/api/groups', body),
  update: (id: string, body: Partial<GroupCreatePayload>) =>
    http<Group | null>('PUT', `/api/groups/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/groups/${id}`),
  listMembers: (id: string) => http<GroupMember[]>('GET', `/api/groups/${id}/members`),
  addMember: (id: string, agent_id: string, alias?: string) =>
    http<GroupMember>('POST', `/api/groups/${id}/members`, { agentId: agent_id, alias }),
  removeMember: (id: string, memberId: string) =>
    http<boolean>('DELETE', `/api/groups/${id}/members/${memberId}`),
  listFiles: (id: string) => http<GroupFile[]>('GET', `/api/groups/${id}/files`),
}

// ── Task API ─────────────────────────────────────────────────

export const taskApi = {
  list: (groupId: string) => http<Task[]>('GET', '/api/tasks', undefined, { groupId }),
  get: (id: string) => http<Task | null>('GET', `/api/tasks/${id}`),
  create: (body: TaskCreatePayload) => http<Task>('POST', '/api/tasks', body),
  update: (id: string, body: Partial<TaskCreatePayload>) =>
    http<Task | null>('PUT', `/api/tasks/${id}`, body),
  delete: (id: string) => http<boolean>('DELETE', `/api/tasks/${id}`),
  ready: (groupId: string) => http<Task[]>('GET', '/api/tasks/ready', undefined, { groupId }),
}

// ── Message API ─────────────────────────────────────────────

export const messageApi = {
  listByGroup: (groupId: string, limit = 100) =>
    http<Message[]>('GET', '/api/messages', undefined, { groupId, limit: String(limit) }),
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

/** 监听某群组的总线事件，返回取消监听函数（与旧 UnlistenFn 兼容）。
 *  断线自动重连（指数退避，最多 5 次），保证长任务期间 WS 不丢。 */
export function onBusEvent(
  groupId: string,
  callback: (data: BusEventData) => void,
): Promise<() => void> {
  let ws: WebSocket | null = null
  let closed = false
  let retry = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  const MAX_RETRIES = 5

  const connect = (resolve?: (fn: () => void) => void) => {
    ws = new WebSocket(`${API_BASE.replace(/^http/, 'ws')}/ws/bus/${groupId}`)

    ws.onopen = () => {
      retry = 0
      if (resolve) resolve(unlisten)
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
