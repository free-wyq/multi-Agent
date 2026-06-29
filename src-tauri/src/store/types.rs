//! 数据模型 —— 与 TS `main/store/types.ts` 一一对应
//! 优先保证 serde 序列化格式与现有 data/*.json 兼容

use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;
use std::collections::HashMap;

// ── AgentDefinition ─────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentDefinition {
    pub id: String,
    pub name: String,
    pub role: String,
    pub system_prompt: String,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub extra_skills: Vec<String>,
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    #[serde(default)]
    pub denied_tools: Vec<String>,
    pub startup_strategy: String,
    pub model: String,
    #[serde(default)]
    pub max_turns: i64,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default, rename = "metadata_")]
    pub metadata: Option<JsonValue>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AgentCreatePayload {
    pub name: String,
    pub role: String,
    #[serde(default)]
    pub system_prompt: Option<String>,
    #[serde(default)]
    pub extra_skills: Vec<String>,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub description: Option<String>,
}

// ── AgentInstance ────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentInstance {
    pub id: String,
    pub definition_id: String,
    #[serde(default)]
    pub session_id: Option<String>,
    pub status: String,
    #[serde(default)]
    pub current_task_id: Option<String>,
    #[serde(default)]
    pub work_dir: Option<String>,
    #[serde(default, rename = "metadata_")]
    pub metadata: Option<JsonValue>,
    pub created_at: String,
    #[serde(default)]
    pub stopped_at: Option<String>,
}

// ── Group ────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Group {
    pub id: String,
    pub name: String,
    pub coordinator_id: String,
    #[serde(default)]
    pub description: Option<String>,
    pub status: String,
    #[serde(default)]
    pub config: Option<JsonValue>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GroupCreatePayload {
    pub name: String,
    #[serde(default)]
    pub coordinator_id: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub member_ids: Vec<String>,
}

// ── GroupMember ──────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupMember {
    pub id: String,
    pub group_id: String,
    pub agent_id: String,
    #[serde(default)]
    pub alias: Option<String>,
    pub joined_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupMemberWithAgent {
    #[serde(flatten)]
    pub member: GroupMember,
    pub agent_name: String,
    pub agent_role: String,
}

// ── GroupFile ────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupFile {
    pub name: String,
    pub size: u64,
    pub modified_at: String,
}

// ── Task ─────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub group_id: String,
    #[serde(default)]
    pub parent_task_id: Option<String>,
    pub title: String,
    #[serde(default)]
    pub description: Option<String>,
    pub status: String,
    #[serde(default)]
    pub assigned_agent_id: Option<String>,
    #[serde(default)]
    pub instance_id: Option<String>,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default)]
    pub artifact_path: Option<String>,
    #[serde(default)]
    pub artifact: Option<JsonValue>,
    #[serde(default)]
    pub exit_code: Option<i64>,
    #[serde(default)]
    pub error_message: Option<String>,
    #[serde(default)]
    pub result_summary: Option<String>,
    #[serde(default)]
    pub dag_order: Option<i64>,
    pub created_at: String,
    #[serde(default)]
    pub started_at: Option<String>,
    #[serde(default)]
    pub completed_at: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TaskCreatePayload {
    pub group_id: String,
    pub title: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub assigned_agent_id: Option<String>,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default)]
    pub dag_order: Option<i64>,
}

// ── Message ─────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub id: String,
    pub group_id: String,
    #[serde(default)]
    pub task_id: Option<String>,
    pub sender_id: String,
    pub receiver_id: String,
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub data: Option<JsonValue>,
    pub created_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct MessageCreatePayload {
    pub group_id: String,
    #[serde(default)]
    pub task_id: Option<String>,
    pub sender_id: String,
    #[serde(default)]
    pub receiver_id: Option<String>,
    #[serde(default, rename = "type")]
    pub kind: Option<String>,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub data: Option<JsonValue>,
}

// ── LLM Config ───────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    #[serde(default)]
    pub apiKey: String,
    #[serde(default)]
    pub baseUrl: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub temperature: f64,
    #[serde(default)]
    pub maxTokens: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppSettings {
    pub llm: LlmConfig,
    #[serde(default)]
    pub claudeCodePath: Option<String>,
}

// ── Shared State (A2A 队列) ───────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskQueueItem {
    pub id: String,
    pub group_id: String,
    pub sender_id: String,
    pub receiver_id: String,
    pub content: String,
    #[serde(default)]
    pub data: Option<JsonValue>,
    pub created_at: String,
    pub status: String,
    #[serde(default)]
    pub claimed_by: Option<String>,
    #[serde(default)]
    pub result: Option<String>,
    #[serde(default)]
    pub result_data: Option<JsonValue>,
    #[serde(default)]
    pub completed_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotifyQueueItem {
    pub id: String,
    pub group_id: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub sender_id: String,
    pub receiver_id: String,
    pub content: String,
    #[serde(default)]
    pub data: Option<JsonValue>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupQueueSnapshot {
    pub group_id: String,
    pub tasks: Vec<TaskQueueItem>,
    pub notifies: Vec<NotifyQueueItem>,
}

/// 持久化容器：与 TS 版各 JSON 文件一一对应
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct PersistedData {
    #[serde(default)]
    pub agents: Vec<AgentDefinition>,
    #[serde(default)]
    pub groups: Vec<Group>,
    #[serde(default)]
    pub members: Vec<GroupMember>,
    #[serde(default)]
    pub tasks: Vec<Task>,
    #[serde(default)]
    pub messages: Vec<Message>,
    #[serde(default)]
    pub queues: HashMap<String, GroupQueueSnapshot>,
}

/// 类型别名工具：用于避免重复
pub type SettingsMap = HashMap<String, JsonValue>;
