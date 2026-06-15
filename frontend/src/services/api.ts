const API_BASE = 'http://localhost:8000/api/v1'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json() as Promise<T>
}

// ── AgentDefinition ────────────────────────────────────────────

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

export const agentApi = {
  list: () => request<AgentDefinition[]>('/agents'),
  get: (id: string) => request<AgentDefinition>(`/agents/${id}`),
  create: (body: AgentCreatePayload) =>
    request<AgentDefinition>('/agents', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: Partial<AgentCreatePayload>) =>
    request<AgentDefinition>(`/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: (id: string) =>
    request<{ success: boolean }>(`/agents/${id}`, { method: 'DELETE' }),
}

// ── Groups ─────────────────────────────────────────────────────

export interface Group {
  id: string
  name: string
  coordinator_agent_id: string | null
  created_at: string
}

export interface GroupWithDetails extends Group {
  members: GroupMember[]
  tasks: Task[]
}

export interface GroupMember {
  id: string
  agent_id: string
  alias: string | null
  agent?: AgentDefinition
}

export interface GroupCreatePayload {
  name: string
  coordinator_agent_id?: string
}

export const groupApi = {
  list: () => request<Group[]>('/groups'),
  get: (id: string) => request<GroupWithDetails>(`/groups/${id}`),
  create: (body: GroupCreatePayload) =>
    request<Group>('/groups', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: Partial<GroupCreatePayload>) =>
    request<Group>(`/groups/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: (id: string) =>
    request<{ success: boolean }>(`/groups/${id}`, { method: 'DELETE' }),
  listMembers: (id: string) => request<GroupMember[]>(`/groups/${id}/members`),
  addMember: (id: string, agent_id: string, alias?: string) =>
    request<GroupMember>(`/groups/${id}/members`, {
      method: 'POST',
      body: JSON.stringify({ agent_id, alias }),
    }),
  removeMember: (id: string, memberId: string) =>
    request<{ success: boolean }>(`/groups/${id}/members/${memberId}`, { method: 'DELETE' }),
}

// ── Tasks ──────────────────────────────────────────────────────

export type TaskStatus = 'submitted' | 'working' | 'completed' | 'failed' | 'canceled' | 'input_required'

export interface Task {
  id: string
  group_id: string
  agent_id: string | null
  title: string
  description: string | null
  status: TaskStatus
  depends_on: string[]
  output_path: string | null
  created_at: string
  updated_at: string
}

export interface TaskCreatePayload {
  group_id: string
  title: string
  description?: string
  agent_id?: string
  depends_on?: string[]
}

export const taskApi = {
  list: (groupId: string) => request<Task[]>(`/tasks?group_id=${groupId}`),
  get: (id: string) => request<Task>(`/tasks/${id}`),
  create: (body: TaskCreatePayload) =>
    request<Task>('/tasks', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: Partial<TaskCreatePayload>) =>
    request<Task>(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: (id: string) =>
    request<{ success: boolean }>(`/tasks/${id}`, { method: 'DELETE' }),
  ready: (groupId: string) => request<Task[]>(`/tasks/group/${groupId}/ready`),
}

// ── Messages ───────────────────────────────────────────────────

export interface Message {
  id: string
  group_id: string
  task_id: string | null
  agent_id: string | null
  content: string
  msg_type: string
  created_at: string
}

export const messageApi = {
  listByGroup: (groupId: string, limit = 100) =>
    request<Message[]>(`/messages/group/${groupId}?limit=${limit}`),
  listByTask: (taskId: string, limit = 100) =>
    request<Message[]>(`/messages/task/${taskId}?limit=${limit}`),
}
