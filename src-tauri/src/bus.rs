//! 事件总线 —— 对应 TS `main/bus/event-bus.ts`
//! Tauri 下用 app.emit 推送给前端，前端用 @tauri-apps/api/event listen

use crate::store::types::Message;
use tauri::{AppHandle, Emitter};
use tokio::sync::OnceCell;

static APP_HANDLE: OnceCell<AppHandle> = OnceCell::const_new();

/// 初始化：在 Tauri setup 阶段注入 app handle
pub fn init(app: &AppHandle) {
    let _ = APP_HANDLE.set(app.clone());
}

pub fn app_handle() -> Option<&'static AppHandle> {
    APP_HANDLE.get()
}

/// 总线事件频道名：bus-event:{groupId}
fn channel(group_id: &str) -> String {
    format!("bus-event:{group_id}")
}

/// 推送一条消息事件给前端
pub async fn publish_message(group_id: &str, msg: &Message) {
    let payload = serde_json::json!({
        "id": msg.id,
        "group_id": msg.group_id,
        "task_id": msg.task_id,
        "sender_id": msg.sender_id,
        "receiver_id": msg.receiver_id,
        "type": msg.kind,
        "content": msg.content,
        "data": msg.data,
        "timestamp": msg.created_at,
    });
    if let Some(app) = app_handle() {
        let _ = app.emit(&channel(group_id), payload);
    }
}

/// 推送 agent 回复（无 Message 对象时的便捷方法）
pub async fn publish_agent_reply(group_id: &str, content: &str) {
    let payload = serde_json::json!({
        "id": crate::store::new_id("evt"),
        "group_id": group_id,
        "task_id": null,
        "sender_id": "coordinator",
        "receiver_id": "broadcast",
        "type": "agent_reply",
        "content": content,
        "data": null,
        "timestamp": crate::store::now_iso(),
    });
    if let Some(app) = app_handle() {
        let _ = app.emit(&channel(group_id), payload);
    }
}

/// 推送任务日志（运行时闭包内同步调用，best-effort）
pub fn try_publish_log(group_id: &str, task_id: Option<&str>, sender_id: &str, line: &str) {
    let payload = serde_json::json!({
        "id": crate::store::new_id("evt"),
        "group_id": group_id,
        "task_id": task_id,
        "sender_id": sender_id,
        "receiver_id": "broadcast",
        "type": "task_log",
        "content": line,
        "data": null,
        "timestamp": crate::store::now_iso(),
    });
    if let Some(app) = app_handle() {
        let _ = app.emit(&channel(group_id), payload);
    }
}
