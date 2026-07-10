//! 状态查询命令 —— Agent 实时状态（greenfield 重写）
//! 参数名 camelCase（与前端 api.ts 对齐）
//! status 字符串：idle | executing | offline（删除旧 Thinking 死状态）

use crate::core::engine::{registry, EngineStatus};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentStatus {
    pub agent_id: String,
    pub agent_name: String,
    pub status: String,
    pub current_task_id: Option<String>,
}

#[tauri::command(rename_all = "camelCase")]
pub fn get_agent_status(group_id: String, agent_id: String) -> Option<AgentStatus> {
    let engine = registry().get_engine(&group_id, &agent_id)?;
    let (status, task_id) = engine.status();
    Some(AgentStatus {
        agent_id: engine.id.clone(),
        agent_name: engine.name.clone(),
        status: engine_status_str(&status).to_string(),
        current_task_id: task_id,
    })
}

#[tauri::command(rename_all = "camelCase")]
pub fn list_agent_statuses(group_id: String) -> Vec<AgentStatus> {
    let engines = registry().list_group_engines(&group_id);
    engines
        .into_iter()
        .map(|(_, engine)| {
            let (status, task_id) = engine.status();
            AgentStatus {
                agent_id: engine.id.clone(),
                agent_name: engine.name.clone(),
                status: engine_status_str(&status).to_string(),
                current_task_id: task_id,
            }
        })
        .collect()
}

fn engine_status_str(status: &EngineStatus) -> &'static str {
    status.as_str()
}
