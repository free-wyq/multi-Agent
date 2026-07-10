//! Group 命令 —— greenfield 重写
//! 参数名 camelCase（与前端 api.ts 对齐）

use crate::core::engine::registry;
use crate::core::store;
use crate::core::types::*;

#[tauri::command(rename_all = "camelCase")]
pub fn list_groups() -> Vec<Group> {
    store::store().list_groups()
}

#[tauri::command(rename_all = "camelCase")]
pub fn get_group(id: String) -> Option<Group> {
    store::store().get_group(&id)
}

#[tauri::command(rename_all = "camelCase")]
pub fn create_group(payload: GroupCreatePayload) -> Group {
    let now = store::now_iso();
    let group = Group {
        id: store::new_id("group"),
        name: payload.name,
        coordinator_id: payload.coordinator_id.unwrap_or_default(),
        description: payload.description,
        status: "active".into(),
        config: None,
        created_at: now.clone(),
        updated_at: now,
    };
    let group = store::store().upsert_group(group);

    if !group.coordinator_id.is_empty() {
        if let Some(coord) = store::store().get_agent(&group.coordinator_id) {
            registry().add_engine(&group.id, &coord);
        }
    }
    for agent_id in &payload.member_ids {
        if let Some(agent) = store::store().get_agent(agent_id) {
            registry().add_engine(&group.id, &agent);
        }
    }
    group
}

#[tauri::command(rename_all = "camelCase")]
pub fn update_group(id: String, payload: serde_json::Value) -> Option<Group> {
    let group = store::store().get_group(&id)?;
    let mut merged = serde_json::to_value(&group).ok()?;
    if let serde_json::Value::Object(map) = &mut merged {
        if let serde_json::Value::Object(patch) = payload {
            for (k, v) in patch {
                map.insert(k.clone(), v.clone());
            }
        }
        map.insert("updated_at".into(), serde_json::json!(store::now_iso()));
    }
    let updated: Group = serde_json::from_value(merged).ok()?;
    Some(store::store().upsert_group(updated))
}

#[tauri::command(rename_all = "camelCase")]
pub fn delete_group(id: String) -> bool {
    // 停止该群所有引擎
    let engines = registry().list_group_engines(&id);
    for (agent_id, _) in engines {
        registry().remove_engine(&id, &agent_id);
    }
    store::store().delete_group(&id)
}

// ── Members ────────────────────────────────────────────────

#[tauri::command(rename_all = "camelCase")]
pub fn group_list_members(group_id: String) -> Vec<GroupMemberWithAgent> {
    store::store().list_group_members_with_agent(&group_id)
}

#[tauri::command(rename_all = "camelCase")]
pub fn group_add_member(group_id: String, agent_id: String, alias: Option<String>) -> GroupMember {
    let member = GroupMember {
        id: store::new_id("member"),
        group_id: group_id.clone(),
        agent_id: agent_id.clone(),
        alias,
        joined_at: store::now_iso(),
    };
    let member = store::store().add_member(member);
    if let Some(agent) = store::store().get_agent(&agent_id) {
        registry().add_engine(&group_id, &agent);
    }
    member
}

#[tauri::command(rename_all = "camelCase")]
pub fn group_remove_member(group_id: String, member_id: String) -> bool {
    let members = store::store().list_group_members(&group_id);
    let agent_id = members
        .iter()
        .find(|m| m.id == member_id)
        .map(|m| m.agent_id.clone());
    let removed = store::store().remove_member(&member_id);
    if let Some(aid) = agent_id {
        registry().remove_engine(&group_id, &aid);
    }
    removed
}

// ── Files ───────────────────────────────────────────────────

#[tauri::command(rename_all = "camelCase")]
pub fn group_list_files(group_id: String) -> Vec<GroupFile> {
    crate::core::persistence::list_files(&group_id)
}
