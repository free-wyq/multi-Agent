// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod bus;
mod commands;
mod engine;
mod llm;
mod prompts;
mod rt;
mod runtime;
mod store;

use std::sync::Arc;
use tauri::Manager;
/// 全局应用状态
pub struct AppState {
    pub data_dir: std::path::PathBuf,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .try_init()
        .ok();

    // 确定数据目录（跨平台）：与 TS 版本 userData 语义一致
    let data_dir = dirs::data_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("multi-agent");
    std::fs::create_dir_all(&data_dir).ok();
    log::info!("MULTI_AGENT_DATA_DIR = {}", data_dir.display());

    let state = Arc::new(AppState {
        data_dir: data_dir.clone(),
    });
    store::persistence::init(&data_dir);

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(state)
        .setup(|app| {
            // 注入 app handle 给事件总线
            bus::init(app.handle());

            // 加载持久化数据 + 恢复 A2A 队列 + 启动所有引擎
            store::store().load();
            store::shared_state::load_all_queues();
            engine::registry().load_from_store();

            #[cfg(debug_assertions)]
            {
                if let Some(win) = app.get_webview_window("main") {
                    win.open_devtools();
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            // ── 智能体 ──
            commands::agent::list_agents,
            commands::agent::get_agent,
            commands::agent::create_agent,
            commands::agent::update_agent,
            commands::agent::delete_agent,
            // ── 群组 ──
            commands::group::list_groups,
            commands::group::get_group,
            commands::group::create_group,
            commands::group::update_group,
            commands::group::delete_group,
            commands::group::group_list_members,
            commands::group::group_add_member,
            commands::group::group_remove_member,
            commands::group::group_list_files,
            // ── 任务 ──
            commands::task::list_tasks,
            commands::task::get_task,
            commands::task::create_task,
            commands::task::update_task,
            commands::task::delete_task,
            commands::task::task_ready,
            // ── 消息 ──
            commands::message::list_messages,
            commands::message::list_messages_by_task,
            commands::message::send_message,
            commands::message::clear_messages_by_group,
            // ── 系统 ──
            commands::system::ping,
            commands::system::get_data_dir,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
