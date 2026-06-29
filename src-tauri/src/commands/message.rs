//! Message 命令 —— 对应 TS message.handlers.ts (A2A 架构)
//!
//! 用户发消息时：存 store + 推 bus，并通过 SharedStateCenter 扔字条路由

use crate::bus;
use crate::store::{self, shared_state, types::*};

#[tauri::command]
pub fn list_messages(group_id: String, limit: Option<usize>) -> Vec<Message> {
    let mut msgs = store::store().list_messages_by_group(&group_id);
    // 按时间排序
    msgs.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    if let Some(l) = limit {
        let start = msgs.len().saturating_sub(l);
        msgs.drain(start..);
    }
    msgs
}

#[tauri::command]
pub fn list_messages_by_task(task_id: String, limit: Option<usize>) -> Vec<Message> {
    let all = store::store().list_all_messages();
    let mut msgs: Vec<Message> = all.into_iter().filter(|m| m.task_id.as_deref() == Some(&task_id)).collect();
    msgs.sort_by(|a, b| a.created_at.cmp(&b.created_at));
    if let Some(l) = limit {
        let start = msgs.len().saturating_sub(l);
        msgs.drain(start..);
    }
    msgs
}

#[tauri::command]
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

    // 推送 bus
    let group_id = msg.group_id.clone();
    bus::publish_message(&group_id, &msg);

    // A2A 路由：sender 是 user 时触发
    if msg.sender_id == "user" {
        let content = msg.content.clone().unwrap_or_default();
        let group_id = msg.group_id.clone();
        // 异步执行路由
        tauri::async_runtime::spawn(async move {
            trigger_a2a_routing(&group_id, &content).await;
        });
    }

    msg
}

#[tauri::command]
pub fn clear_messages_by_group(group_id: String) -> bool {
    store::store().clear_messages_by_group(&group_id);
    true
}

/// A2A 消息路由
/// - @mention → 扔字条到被 @ agent 的收件箱
/// - 否则 → 扔字条到 coordinator 的收件箱
async fn trigger_a2a_routing(group_id: &str, content: &str) {
    let mentions = find_mentions(content);
    if !mentions.is_empty() {
        let members = store::store().list_group_members_with_agent(group_id);
        let agents = store::store().list_agents();

        for mention in mentions {
            let mut target_id: Option<String> = None;

            // 按 name
            if let Some(a) = agents.iter().find(|a| a.name == mention) {
                if members.iter().any(|m| m.member.agent_id == a.id) {
                    target_id = Some(a.id.clone());
                }
            }
            // 按 agent_id
            if target_id.is_none() {
                if members.iter().any(|m| m.member.agent_id == mention) {
                    target_id = Some(mention.clone());
                }
            }

            if let Some(tid) = target_id {
                shared_state::shared_state().push_notify(shared_state::PushNotifyParams {
                    group_id: group_id.to_string(),
                    kind: "agent_reply".into(),
                    sender_id: "user".into(),
                    receiver_id: tid,
                    content: content.to_string(),
                    data: None,
                });
                return; // 路由到被 @ 的 agent，不再走 coordinator
            }
        }
    }

    // 没有 @mention：扔字条到 coordinator
    let group = match store::store().get_group(group_id) {
        Some(g) => g,
        None => return,
    };
    if group.coordinator_id.is_empty() {
        return;
    }
    shared_state::shared_state().push_notify(shared_state::PushNotifyParams {
        group_id: group_id.to_string(),
        kind: "coordinator_reply".into(),
        sender_id: "user".into(),
        receiver_id: group.coordinator_id,
        content: content.to_string(),
        data: None,
    });
}

/// @mention 提取
fn find_mentions(content: &str) -> Vec<String> {
    let mut out = Vec::new();
    let bytes = content.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'@' {
            let start = i + 1;
            let mut j = start;
            while j < bytes.len() {
                let c = bytes[j] as char;
                if c.is_whitespace() || c == '\n' || c == '\r' {
                    break;
                }
                j += 1;
            }
            if j > start {
                let name = content[start..j].trim_end_matches(|c: char| {
                    matches!(c, ',' | '.' | '，' | '。' | ':' | '：' | '!' | '！' | '？' | '?')
                });
                if !name.is_empty() {
                    out.push(name.to_string());
                }
            }
            i = j;
        } else {
            i += 1;
        }
    }
    out
}
