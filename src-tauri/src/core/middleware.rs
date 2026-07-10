//! 中间件系统（AgentScope 启发）—— greenfield 重写
//!
//! inbound 管道：消息/任务到达 engine 前的横切处理（日志、校验）。
//! outbound 管道：engine 产出回复后的横切处理（mention 路由）。
//! 消除旧代码散落的 route_mentions 调用 + 两份重复 @mention 扫描器。
//!
//! mention 扫描器统一在此（旧 engine.rs::regex_find_mentions 与
//! commands/message.rs::find_mentions 字节相同，现合并为一份）。
//!
//! v1 仅 outbound（mention 路由）实际挂载；inbound 钩子
//! （inbound_log/make_log_message/notify_preview/preview）为 v2 预留接缝，
//! 未挂载，故整文件 allow(dead_code)。
#![allow(dead_code)]

use crate::core::inbox::{hub, PushTaskParams};
use crate::core::store::store;
use crate::core::types::{GroupMember, Message, NotifyQueueItem};

/// 提取内容中的 @mention token（去尾部标点，不去重）
pub fn find_mentions(content: &str) -> Vec<String> {
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

/// 解析一个 mention token 到目标 agent_id：
/// (a) 匹配群成员 agent_id；(b) 匹配群成员所属 agent 的 name；(c) 匹配含该 token 的 alias。
fn resolve_mention(members: &[GroupMember], mention: &str) -> Option<String> {
    let agents = store().list_agents();
    // (a) agent_id 直接命中成员
    if let Some(m) = members.iter().find(|m| m.agent_id == mention) {
        return Some(m.agent_id.clone());
    }
    // (b) name 命中某成员所属 agent
    for a in &agents {
        if a.name == mention && members.iter().any(|m| m.agent_id == a.id) {
            return Some(a.id.clone());
        }
    }
    // (c) alias 包含该 token
    for m in members {
        if let Some(alias) = &m.alias {
            if alias.contains(mention) {
                return Some(m.agent_id.clone());
            }
        }
    }
    None
}

/// outbound：扫描回复内容中的 @mention，向被提及成员 push_task（30s 去重防环）。
/// 由 engine 持有 recent_routes 状态传入。
pub fn route_mentions(
    group_id: &str,
    sender_id: &str,
    sender_name: &str,
    content: &str,
    recent_routes: &mut std::collections::HashMap<String, f64>,
) {
    let mentions = find_mentions(content);
    if mentions.is_empty() {
        return;
    }
    let now = now_secs();
    // 清理 30s 前的记录
    recent_routes.retain(|_, t| now - *t < 30.0);

    let members = store().list_group_members(group_id);
    for mention in &mentions {
        // 跳过自己（按 id 或 name）
        if mention == sender_id || mention == sender_name {
            continue;
        }
        let target_id = match resolve_mention(&members, mention) {
            Some(t) => t,
            None => continue,
        };
        if target_id == sender_id {
            continue;
        }
        // 30s 内同 sender→同 target 只路由一次（防环）
        let key = format!("{sender_id}->{target_id}");
        if recent_routes.contains_key(&key) {
            log::debug!("[middleware] mention route suppressed (anti-loop): {key}");
            continue;
        }
        recent_routes.insert(key, now);
        hub().push_task(PushTaskParams {
            group_id: group_id.to_string(),
            sender_id: sender_id.to_string(),
            receiver_id: target_id,
            content: content.to_string(),
            data: None,
        });
    }
}

fn now_secs() -> f64 {
    chrono::Utc::now().timestamp() as f64
}

// ── inbound 管道钩子（预留接缝；v1 仅做日志）──────────────────

/// inbound 预处理：记录到达日志。返回是否继续处理。
/// v2 预留接缝，v1 未挂载。
pub fn inbound_log(kind: &str, group_id: &str, agent_id: &str, item: &str) -> bool {
    log::info!("[middleware:inbound] {kind} -> {agent_id} @ {group_id}: {}", preview(item, 50));
    true
}

fn preview(s: &str, n: usize) -> String {
    let t = s.trim();
    if t.chars().count() <= n {
        t.to_string()
    } else {
        let head: String = t.chars().take(n).collect();
        format!("{head}...")
    }
}

/// 把一条用户消息按 @mention 路由（用户发消息时用，替代旧 trigger_a2a_routing）。
/// 有 @mention → 扔字条给被 @ 的 agent；否则 → 扔给 coordinator。
pub fn route_user_message(group_id: &str, content: &str) {
    let mentions = find_mentions(content);
    if !mentions.is_empty() {
        let members = store().list_group_members(group_id);
        for mention in &mentions {
            if let Some(tid) = resolve_mention(&members, mention) {
                hub().push_notify(crate::core::inbox::PushNotifyParams {
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
    // 无命中 mention：扔给 coordinator
    let group = match store().get_group(group_id) {
        Some(g) => g,
        None => return,
    };
    if group.coordinator_id.is_empty() {
        return;
    }
    hub().push_notify(crate::core::inbox::PushNotifyParams {
        group_id: group_id.to_string(),
        kind: "coordinator_reply".into(),
        sender_id: "user".into(),
        receiver_id: group.coordinator_id,
        content: content.to_string(),
        data: None,
    });
}

/// 消息预览，供 engine 落库日志消息时复用
/// v2 预留接缝，v1 未挂载。
pub fn make_log_message(group_id: &str, sender_id: &str, task_id: Option<&str>, content: &str) -> Message {
    Message {
        id: crate::core::store::new_id("msg"),
        group_id: group_id.to_string(),
        task_id: task_id.map(|s| s.to_string()),
        sender_id: sender_id.to_string(),
        receiver_id: "broadcast".to_string(),
        kind: "task_log".to_string(),
        content: Some(content.to_string()),
        data: None,
        created_at: crate::core::store::now_iso(),
    }
}

/// 消息预览（NotifyQueueItem 的 content 预览），用于日志
/// v2 预留接缝，v1 未挂载。
pub fn notify_preview(n: &NotifyQueueItem, n_chars: usize) -> String {
    preview(&n.content, n_chars)
}
