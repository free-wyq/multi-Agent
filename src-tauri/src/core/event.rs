//! 类型化事件系统（AgentScope 启发）—— greenfield 重写
//!
//! 内部用类型化 DomainEvent 流转，边界投影为前端契约的 BusEventData
//! （snake_case payload，channel `bus-event:{group_id}`）。
//!
//! 相对旧 bus.rs 的关键修复：
//! - 旧 publish_dispatch_event 发 `type:"dispatch"`，前端 useBusEvent 只认 `task_dispatch`
//!   → 派工事件从未进入 statusEvents。现统一为 `task_dispatch`。
//! - 旧代码 task_complete/task_failed 只作为通知发给 sender，从不发到 bus →
//!   前端任务状态切换缺失。现 TaskCompleted 投影为 task_complete/task_failed 发到 bus。
//! - 统一所有 emit 走 DomainEvent → project → app.emit 一条路径，消除散落的 json!。

use crate::core::store::{new_id, now_iso};
use crate::core::types::Message;
use serde_json::Value as JsonValue;
use tauri::{AppHandle, Emitter};
use tokio::sync::OnceCell;

static APP_HANDLE: OnceCell<AppHandle> = OnceCell::const_new();

pub fn init(app: &AppHandle) {
    let _ = APP_HANDLE.set(app.clone());
}

pub fn app_handle() -> Option<&'static AppHandle> {
    APP_HANDLE.get()
}

fn channel(group_id: &str) -> String {
    format!("bus-event:{group_id}")
}

/// 类型化领域事件（内部流转）
#[derive(Debug, Clone)]
pub enum DomainEvent {
    /// 一条消息被持久化（含 agent_reply / user_input / task_log / dispatch 等 kind）
    MessageAdded(Message),
    /// 协调者向 worker 派发一步任务
    TaskDispatched {
        group_id: String,
        task_id: String,
        step: i64,
        agent_id: String,
        agent_name: String,
        instruction: String,
    },
    /// 一个任务完成或失败
    TaskCompleted {
        group_id: String,
        task_id: String,
        agent_id: String,
        success: bool,
        result: Option<String>,
        exit_code: Option<i64>,
    },
    /// CLI 的一行日志
    TaskLog {
        group_id: String,
        task_id: Option<String>,
        sender_id: String,
        line: String,
    },
}

impl DomainEvent {
    fn group_id(&self) -> &str {
        match self {
            DomainEvent::MessageAdded(m) => &m.group_id,
            DomainEvent::TaskDispatched { group_id, .. }
            | DomainEvent::TaskCompleted { group_id, .. }
            | DomainEvent::TaskLog { group_id, .. } => group_id,
        }
    }

    /// 投影为前端契约的 BusEventData（snake_case payload）
    fn project(&self) -> JsonValue {
        match self {
            DomainEvent::MessageAdded(msg) => serde_json::json!({
                "id": msg.id,
                "group_id": msg.group_id,
                "task_id": msg.task_id,
                "sender_id": msg.sender_id,
                "receiver_id": msg.receiver_id,
                "type": msg.kind,
                "content": msg.content,
                "data": msg.data,
                "timestamp": msg.created_at,
            }),
            DomainEvent::TaskDispatched {
                group_id,
                task_id,
                step,
                agent_id,
                agent_name,
                instruction,
            } => serde_json::json!({
                "id": new_id("evt"),
                "group_id": group_id,
                "task_id": task_id,
                "sender_id": "coordinator",
                "receiver_id": agent_id,
                "type": "task_dispatch",
                "content": instruction,
                "data": {
                    "step": step,
                    "agent_name": agent_name,
                    "agent_id": agent_id,
                },
                "timestamp": now_iso(),
            }),
            DomainEvent::TaskCompleted {
                group_id,
                task_id,
                agent_id,
                success,
                result,
                exit_code,
            } => serde_json::json!({
                "id": new_id("evt"),
                "group_id": group_id,
                "task_id": task_id,
                "sender_id": agent_id,
                "receiver_id": "broadcast",
                "type": if *success { "task_complete" } else { "task_failed" },
                "content": result,
                "data": serde_json::json!({ "exit_code": exit_code }),
                "timestamp": now_iso(),
            }),
            DomainEvent::TaskLog {
                group_id,
                task_id,
                sender_id,
                line,
            } => serde_json::json!({
                "id": new_id("evt"),
                "group_id": group_id,
                "task_id": task_id,
                "sender_id": sender_id,
                "receiver_id": "broadcast",
                "type": "task_log",
                "content": line,
                "data": null,
                "timestamp": now_iso(),
            }),
        }
    }
}

/// 发布一个领域事件：投影 + app.emit 到 bus-event:{group_id}
pub fn emit(evt: DomainEvent) {
    let group_id = evt.group_id().to_string();
    let payload = evt.project();
    if let Some(app) = app_handle() {
        let _ = app.emit(&channel(&group_id), payload);
    }
}

/// 同步发布日志事件（CLI on_log 回调是同步的）
pub fn emit_log(group_id: &str, task_id: Option<&str>, sender_id: &str, line: &str) {
    emit(DomainEvent::TaskLog {
        group_id: group_id.to_string(),
        task_id: task_id.map(|s| s.to_string()),
        sender_id: sender_id.to_string(),
        line: line.to_string(),
    })
}
