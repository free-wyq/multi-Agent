/**
 * API 层：Tauri invoke 调用（替代 Electron IPC）
 *
 * 所有接口签名与旧 electronAPI 保持一致，页面组件尽量少改。
 * 命令名对应 Rust 端 #[tauri::command] 的函数名（snake_case）。
 */

import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'

// ── 类型定义 ──────────────────────────────────────────────────────

export interface AgentDefinition {
  id: string
  name: string
  role: string
  extra_skills?: string[]
  skills?: string[]
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
  // Rust 端 GroupMemberWithAgent 用 #[serde(flatten)]，前端平铺访问即可，
  // member 字段保留以兼容（可能存在也可能不存在）
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

// ── Agent API ────────────────────────────────────────────────

export const agentApi = {
  list: () => invoke<AgentDefinition[]>('list_agents'),
  get: (id: string) => invoke<AgentDefinition | null>('get_agent', { id }),
  create: (body: AgentCreatePayload) => invoke<AgentDefinition>('create_agent', { payload: body }),
  update: (id: string, body: Partial<AgentCreatePayload>) =>
    invoke<AgentDefinition | null>('update_agent', { id, payload: body }),
  delete: (id: string) => invoke<boolean>('delete_agent', { id }),
}

// ── Group API ────────────────────────────────────────────────

export const groupApi = {
  list: () => invoke<Group[]>('list_groups'),
  get: (id: string) => invoke<Group | null>('get_group', { id }),
  create: (body: GroupCreatePayload) => invoke<Group>('create_group', { payload: body }),
  update: (id: string, body: Partial<GroupCreatePayload>) =>
    invoke<Group | null>('update_group', { id, payload: body }),
  delete: (id: string) => invoke<boolean>('delete_group', { id }),
  listMembers: (id: string) => invoke<GroupMember[]>('group_list_members', { groupId: id }),
  addMember: (id: string, agent_id: string, alias?: string) =>
    invoke<GroupMember>('group_add_member', { groupId: id, agentId: agent_id, alias }),
  removeMember: (id: string, memberId: string) =>
    invoke<boolean>('group_remove_member', { groupId: id, memberId }),
  listFiles: (id: string) => invoke<GroupFile[]>('group_list_files', { groupId: id }),
}

// ── Task API ─────────────────────────────────────────────────

export const taskApi = {
  list: (groupId: string) => invoke<Task[]>('list_tasks', { groupId }),
  get: (id: string) => invoke<Task | null>('get_task', { id }),
  create: (body: TaskCreatePayload) => invoke<Task>('create_task', { payload: body }),
  update: (id: string, body: Partial<TaskCreatePayload>) =>
    invoke<Task | null>('update_task', { id, payload: body }),
  delete: (id: string) => invoke<boolean>('delete_task', { id }),
  ready: (groupId: string) => invoke<Task[]>('task_ready', { groupId }),
}

// ── Message API ─────────────────────────────────────────────

export const messageApi = {
  listByGroup: (groupId: string, limit = 100) =>
    invoke<Message[]>('list_messages', { groupId, limit }),
  listByTask: (taskId: string, limit = 100) =>
    invoke<Message[]>('list_messages_by_task', { taskId, limit }),
  send: (body: MessageCreatePayload) => invoke<Message>('send_message', { payload: body }),
  clearByGroup: (groupId: string) => invoke<boolean>('clear_messages_by_group', { groupId }),
}

// ── 实时事件：Tauri events ──────────────────────────────────

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

/** 监听某群组的总线事件，返回取消监听函数 */
export function onBusEvent(
  groupId: string,
  callback: (data: BusEventData) => void,
): Promise<UnlistenFn> {
  return listen<BusEventData>(`bus-event:${groupId}`, (event) => {
    callback(event.payload)
  })
}
