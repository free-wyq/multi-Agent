/**
 * 数据类型定义（对应原 SQLAlchemy ORM 模型）
 *
 * PostgreSQL → 内存 Map + JSON 文件
 */

// ── AgentDefinition ─────────────────────────────────────────

export interface AgentDefinition {
  id: string
  name: string
  role: string
  system_prompt: string
  skills: string[]
  extra_skills: string[]
  base_image: string          // 保留字段，后续可移除
  allowed_tools: string[]
  denied_tools: string[]
  startup_strategy: 'on_demand' | 'pooled' | 'always_on'
  model: string
  max_turns: number
  description?: string
  metadata_?: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface AgentCreatePayload {
  name: string
  role: string
  system_prompt?: string
  extra_skills?: string[]
  skills?: string[]
  description?: string
}

// ── AgentInstance ────────────────────────────────────────────

export interface AgentInstance {
  id: string
  definition_id: string
  container_id?: string       // 保留字段，不再用
  container_name?: string     // 保留字段，不再用
  session_id?: string
  status: 'idle' | 'running' | 'error' | 'stopped'
  current_task_id?: string
  work_dir?: string
  metadata_?: Record<string, unknown>
  created_at: string
  stopped_at?: string
}

// ── Group ────────────────────────────────────────────────────

export interface Group {
  id: string
  name: string
  coordinator_id: string
  volume_name?: string        // 保留字段，不再用
  description?: string
  status: 'active' | 'archived'
  config?: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface GroupCreatePayload {
  name: string
  coordinator_id?: string
  description?: string
  member_ids?: string[]       // 建群时自动添加成员
}

// ── GroupMember ──────────────────────────────────────────────

export interface GroupMember {
  id: string
  group_id: string
  agent_id: string
  alias?: string
  joined_at: string
}

export interface GroupMemberWithAgent extends GroupMember {
  agent_name: string
  agent_role: string
}

// ── GroupFile ────────────────────────────────────────────────

export interface GroupFile {
  name: string
  size: number
  modified_at: string
}

// ── Task ─────────────────────────────────────────────────────

export type TaskStatus = 'submitted' | 'working' | 'completed' | 'failed' | 'canceled' | 'input-required'

export interface Task {
  id: string
  group_id: string
  parent_task_id?: string
  title: string
  description?: string
  status: TaskStatus
  assigned_agent_id?: string
  instance_id?: string
  dependencies: string[]
  artifact_path?: string
  artifact?: Record<string, unknown>
  exit_code?: number
  error_message?: string
  result_summary?: string
  dag_order?: number
  created_at: string
  started_at?: string
  completed_at?: string
}

export interface TaskCreatePayload {
  group_id: string
  title: string
  description?: string
  assigned_agent_id?: string
  dependencies?: string[]
  dag_order?: number
}

// ── Message ─────────────────────────────────────────────────

export interface Message {
  id: string
  group_id: string
  task_id?: string
  sender_id: string
  receiver_id: string
  type: string
  content?: string
  data?: Record<string, unknown>
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

// ── Coordinator ──────────────────────────────────────────────

export interface IntentAnalysis {
  analysis: string
  involved_roles: string[]
}

export interface SubTaskDef {
  title: string
  description: string
  assigned_role: string
  depends_on: number[]
}

export interface TaskDecomposition {
  subtasks: SubTaskDef[]
  reasoning: string
}

export interface SubTask {
  title: string
  description: string
  assigned_agent_id: string
  dependencies: number[]
}

export interface CoordinatorState {
  group_id: string
  requirement: string
  intent_analysis: string
  involved_roles: string[]
  subtasks: SubTask[]
  dag_nodes: Record<string, unknown>[]
  dag_edges: Record<string, unknown>[]
  pending_task_ids: string[]
  running_task_ids: string[]
  completed_task_ids: string[]
  failed_task_ids: string[]
  summary: string
  artifacts: Record<string, unknown>[]
}

// ── Brain ────────────────────────────────────────────────────

export interface BrainDecision {
  action: 'chat' | 'execute' | 'ask'
  content: string
  reasoning: string
}

// ── LLM Config ───────────────────────────────────────────────

export interface LLMConfig {
  apiKey: string
  baseUrl: string
  model: string
  temperature: number
  maxTokens: number
}

export interface AppSettings {
  llm: LLMConfig
  claudeCodePath?: string
}
