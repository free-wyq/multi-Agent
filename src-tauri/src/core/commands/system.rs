//! 系统命令 —— ping / 数据目录

use crate::core::persistence;

#[tauri::command]
pub fn ping() -> String {
    "pong".into()
}

#[tauri::command]
pub fn get_data_dir() -> String {
    persistence::data_dir().to_string_lossy().to_string()
}
