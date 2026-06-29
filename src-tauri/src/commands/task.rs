//! Task 命令 —— 对应 TS task.handlers.ts

use crate::store::{self, types::*};

#[tauri::command]
pub fn list_tasks(group_id: Option<String>) -> Vec<Task> {
    match group_id {
        Some(gid) => store::store().list_tasks_by_group(&gid),
        None => store::store().list_tasks(),
    }
}

#[tauri::command]
pub fn get_task(id: String) -> Option<Task> {
    store::store().get_task(&id)
}

#[tauri::command]
pub fn create_task(payload: TaskCreatePayload) -> Task {
    let now = store::now_iso();
    let task = Task {
        id: store::new_id("task"),
        group_id: payload.group_id,
        parent_task_id: None,
        title: payload.title,
        description: payload.description,
        status: "submitted".into(),
        assigned_agent_id: payload.assigned_agent_id,
        instance_id: None,
        dependencies: payload.dependencies,
        artifact_path: None,
        artifact: None,
        exit_code: None,
        error_message: None,
        result_summary: None,
        dag_order: payload.dag_order,
        created_at: now,
        started_at: None,
        completed_at: None,
    };
    store::store().upsert_task(task)
}

#[tauri::command]
pub fn update_task(id: String, payload: serde_json::Value) -> Option<Task> {
    let task = store::store().get_task(&id)?;
    let mut merged = serde_json::to_value(&task).ok()?;
    if let serde_json::Value::Object(map) = &mut merged {
        if let serde_json::Value::Object(patch) = payload {
            // 状态转换自动设置时间戳
            if let Some(status) = patch.get("status").and_then(|v| v.as_str()) {
                if status == "working" && task.started_at.is_none() {
                    map.insert("started_at".into(), serde_json::json!(store::now_iso()));
                }
                if matches!(status, "completed" | "failed" | "canceled") && task.completed_at.is_none() {
                    map.insert("completed_at".into(), serde_json::json!(store::now_iso()));
                }
            }
            for (k, v) in patch {
                map.insert(k.clone(), v.clone());
            }
        }
    }
    let updated: Task = serde_json::from_value(merged).ok()?;
    Some(store::store().upsert_task(updated))
}

#[tauri::command]
pub fn delete_task(id: String) -> bool {
    store::store().delete_task(&id)
}

#[tauri::command]
pub fn task_ready(group_id: String) -> Vec<Task> {
    // getReadyTasks：status=submitted 且依赖全部 completed
    let group_tasks = store::store().list_tasks_by_group(&group_id);
    group_tasks
        .into_iter()
        .filter(|t| {
            if t.status != "submitted" {
                return false;
            }
            if t.dependencies.is_empty() {
                return true;
            }
            t.dependencies.iter().all(|dep| {
                store::store()
                    .get_task(dep)
                    .map(|d| d.status == "completed")
                    .unwrap_or(false)
            })
        })
        .collect()
}
