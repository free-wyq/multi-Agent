//! Agent 命令 —— greenfield 重写
//! 参数名 camelCase（与前端 api.ts 对齐）

use crate::core::engine::registry;
use crate::core::store;
use crate::core::types::*;

#[tauri::command(rename_all = "camelCase")]
pub fn list_agents() -> Vec<AgentDefinition> {
    store::store().list_agents()
}

#[tauri::command(rename_all = "camelCase")]
pub fn get_agent(id: String) -> Option<AgentDefinition> {
    store::store().get_agent(&id)
}

#[tauri::command(rename_all = "camelCase")]
pub fn create_agent(payload: AgentCreatePayload) -> AgentDefinition {
    let now = store::now_iso();
    let agent = AgentDefinition {
        id: store::new_id("agent"),
        name: payload.name,
        role: payload.role,
        system_prompt: payload.system_prompt.unwrap_or_default(),
        skills: payload.skills,
        extra_skills: payload.extra_skills,
        allowed_tools: vec![],
        denied_tools: vec![],
        startup_strategy: "on_demand".into(),
        model: "glm-5.1".into(),
        max_turns: 50,
        description: payload.description,
        metadata: None,
        created_at: now.clone(),
        updated_at: now,
    };
    store::store().upsert_agent(agent)
}

#[tauri::command(rename_all = "camelCase")]
pub fn update_agent(id: String, payload: serde_json::Value) -> Option<AgentDefinition> {
    let agent = store::store().get_agent(&id)?;
    let mut merged = serde_json::to_value(&agent).ok()?;
    if let serde_json::Value::Object(map) = &mut merged {
        if let serde_json::Value::Object(patch) = payload {
            for (k, v) in patch {
                map.insert(k.clone(), v.clone());
            }
        }
        map.insert("updated_at".into(), serde_json::json!(store::now_iso()));
    }
    let updated: AgentDefinition = serde_json::from_value(merged).ok()?;
    Some(store::store().upsert_agent(updated))
}

#[tauri::command(rename_all = "camelCase")]
pub fn delete_agent(id: String) -> bool {
    store::store().delete_agent(&id)
}

/// 供 group 命令启动引擎用（内部）
#[allow(dead_code)]
pub fn ensure_engine(group_id: &str, agent_id: &str) {
    if let Some(agent) = store::store().get_agent(agent_id) {
        registry().add_engine(group_id, &agent);
    }
}
