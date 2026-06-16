/**
 * API 层：IPC 调用替代 HTTP fetch
 *
 * 所有接口签名保持不变，页面组件无需修改
 */

// ── 类型定义（保持原有接口不变）──────────────────────────────────

export interface AgentDefinition {
  id: string
  name: string
  role: string
  extra_skills?: string[]
  system_prompt?: string
  created_at: string
  updated_at: string
}

export interface AgentCreatePayload {
  name: string
  role: string
  extra_skills?: string[]
  system_prompt?: string
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

export interface GroupWithDetails extends Group {
  members: GroupMember[]
  tasks: Task[]
}

export interface GroupMember {
  id: string
  group_id: string
  agent_id: string
  alias: string | null
  joined_at: string
  agent_name: string
  agent_role: string
}

export interface GroupFile {
  name: string
  size: number
  modified_at: number
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
  receiver_id: string
  type: string
  content?: string
  data?: Record<string, unknown>
}

// ── Electron IPC 调用 ──────────────────────────────────────────

const api = window.electronAPI

// ── Agent API ────────────────────────────────────────────────

export const agentApi = {
  list: () => api.agentList(),
  get: (id: string) => api.agentGet(id),
  create: (body: AgentCreatePayload) => api.agentCreate(body),
  update: (id: string, body: Partial<AgentCreatePayload>) => api.agentUpdate(id, body),
  delete: (id: string) => api.agentDelete(id),
}

// ── Group API ────────────────────────────────────────────────

export const groupApi = {
  list: () => api.groupList(),
  get: (id: string) => api.groupGet(id),
  create: (body: GroupCreatePayload) => api.groupCreate(body),
  update: (id: string, body: Partial<GroupCreatePayload>) => api.groupUpdate(id, body),
  delete: (id: string) => api.groupDelete(id),
  listMembers: (id: string) => api.groupListMembers(id),
  addMember: (id: string, agent_id: string, alias?: string) => api.groupAddMember(id, agent_id, alias),
  removeMember: (id: string, memberId: string) => api.groupRemoveMember(id, memberId),
  listFiles: (id: string) => api.groupListFiles(id),
}

// ── Task API ─────────────────────────────────────────────────

export const taskApi = {
  list: (groupId: string) => api.taskList(groupId),
  get: (id: string) => api.taskGet(id),
  create: (body: TaskCreatePayload) => api.taskCreate(body),
  update: (id: string, body: Partial<TaskCreatePayload>) => api.taskUpdate(id, body),
  delete: (id: string) => api.taskDelete(id),
  ready: (groupId: string) => api.taskReady(groupId),
}

// ── Message API ─────────────────────────────────────────────

export const messageApi = {
  listByGroup: (groupId: string, limit = 100) => api.messageListByGroup(groupId, limit),
  listByTask: (taskId: string, limit = 100) => api.messageListByTask(taskId, limit),
  send: (body: MessageCreatePayload) => api.messageSend(body),
  clearByGroup: (groupId: string) => api.messageClearByGroup(groupId),
}
