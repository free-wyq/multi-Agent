// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod core;

use std::sync::Arc;
use tauri::Manager;

/// 全局应用状态
pub struct AppState {
    pub data_dir: std::path::PathBuf,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 项目根 .env（LLM 配置）。tauri dev 的 cwd 是 src-tauri/，需向上找一级。
    // CARGO_MANIFEST_DIR 指向 src-tauri/，其父目录即项目根。
    let env_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(".env");
    dotenvy::from_path(&env_path).ok();

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
    core::persistence::init(&data_dir);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(state)
        .setup(|app| {
            // 注入 app handle 给事件总线
            core::event::init(app.handle());

            // 加载持久化数据 + 恢复 A2A 队列 + 启动所有引擎
            core::store::store().load();
            core::inbox::load_all_queues();
            core::engine::registry().load_from_store();

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
            core::commands::agent::list_agents,
            core::commands::agent::get_agent,
            core::commands::agent::create_agent,
            core::commands::agent::update_agent,
            core::commands::agent::delete_agent,
            // ── 群组 ──
            core::commands::group::list_groups,
            core::commands::group::get_group,
            core::commands::group::create_group,
            core::commands::group::update_group,
            core::commands::group::delete_group,
            core::commands::group::group_list_members,
            core::commands::group::group_add_member,
            core::commands::group::group_remove_member,
            core::commands::group::group_list_files,
            // ── 任务 ──
            core::commands::task::list_tasks,
            core::commands::task::get_task,
            core::commands::task::create_task,
            core::commands::task::update_task,
            core::commands::task::delete_task,
            core::commands::task::task_ready,
            // ── 消息 ──
            core::commands::message::list_messages,
            core::commands::message::list_messages_by_task,
            core::commands::message::send_message,
            core::commands::message::clear_messages_by_group,
            // ── 系统 ──
            core::commands::system::ping,
            core::commands::system::get_data_dir,
            // ── 状态 ──
            core::commands::status::get_agent_status,
            core::commands::status::list_agent_statuses,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|_app_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } = event {
            log::info!("[lib] 应用退出，flush 持久化 + 关闭引擎...");
            core::persistence::flush_all();
            core::engine::registry().shutdown_all();
        }
    });
}
