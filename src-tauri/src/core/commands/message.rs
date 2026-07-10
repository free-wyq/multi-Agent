//! Message 命令 —— greenfield 重写
//! 参数名 camelCase（与前端 api.ts 对齐）
//!
//! send_message：存 store + 发 MessageAdded 事件；sender=user 时按 @mention 路由
//! （统一走 middleware::route_user_message，替代旧 trigger_a2a_routing + find_mentions）。

use crate::core::event::{self, DomainEvent};
use crate::core::middleware;
use crate::core::store;
use crate::core::types::*;

#[tauri::command(rename_all = "camelCase")]
pub fn list_messages(group_id: String, limit: Option<usize>) -> Vec<Message> {
    let mut msgs = store::store().list_messages_by_group(&group_id);
    msgs.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    if let Some(l) = limit {
        let start = msgs.len().saturating_sub(l);
        msgs.drain(start..);
    }
    msgs
}

#[tauri::command(rename_all = "camelCase")]
pub fn list_messages_by_task(task_id: String, limit: Option<usize>) -> Vec<Message> {
    let all = store::store().list_all_messages();
    let mut msgs: Vec<Message> = all
        .into_iter()
        .filter(|m| m.task_id.as_deref() == Some(&task_id))
        .collect();
    msgs.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    if let Some(l) = limit {
        let start = msgs.len().saturating_sub(l);
        msgs.drain(start..);
    }
    msgs
}

#[tauri::command(rename_all = "camelCase")]
pub fn send_message(payload: MessageCreatePayload) -> Message {
    let msg = store::store().add_message(Message {
        id: store::new_id("msg"),
        group_id: payload.group_id.clone(),
        task_id: payload.task_id,
        sender_id: payload.sender_id.clone(),
        receiver_id: payload.receiver_id.unwrap_or_else(|| "broadcast".into()),
        kind: payload.kind.unwrap_or_else(|| "user_input".into()),
        content: payload.content,
        data: payload.data,
        created_at: store::now_iso(),
    });

    event::emit(DomainEvent::MessageAdded(msg.clone()));

    if msg.sender_id == "user" {
        let group_id = msg.group_id.clone();
        let content = msg.content.clone().unwrap_or_default();
        let _ = tauri::async_runtime::spawn(async move {
            middleware::route_user_message(&group_id, &content);
        });
    }

    msg
}

#[tauri::command(rename_all = "camelCase")]
pub fn clear_messages_by_group(group_id: String) -> bool {
    store::store().clear_messages_by_group(&group_id);
    true
}
