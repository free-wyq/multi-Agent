//! JSON 持久化 —— greenfield 重写
//!
//! 保留语义：每实体一个 JSON 文件（裸数组）+ queues/<group>.json 快照；
//! 写入 500ms 防抖 + 原子写（.tmp → rename）；加载时缺失/损坏返回空。
//! byte 兼容旧 data/*.json。

use crate::core::types::*;
use parking_lot::Mutex;
use serde::de::DeserializeOwned;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::Duration;

static DEBOUNCE_TIMERS: LazyLock<Mutex<std::collections::HashMap<String, DebounceEntry>>> =
    LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[derive(Clone)]
struct DebounceEntry {
    handle_id: u64,
    payload: serde_json::Value,
    kind: DebounceKind,
}

#[derive(Clone, Copy, PartialEq)]
enum DebounceKind {
    Entity,
    Queue,
}

/// 全局自增 seq，标记最新一次防抖（旧的失效）
static DEBOUNCE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

static RUNTIME_DATA_DIR: LazyLock<Mutex<PathBuf>> = LazyLock::new(|| Mutex::new(PathBuf::from(".")));

pub fn init(data_dir: &Path) {
    set_data_dir(data_dir);
    ensure_dirs(data_dir);
}

fn set_data_dir(p: &Path) {
    *RUNTIME_DATA_DIR.lock() = p.to_path_buf();
}

pub fn data_dir() -> PathBuf {
    RUNTIME_DATA_DIR.lock().clone()
}

fn ensure_dirs(base: &Path) {
    std::fs::create_dir_all(base).ok();
    std::fs::create_dir_all(base.join("group_files")).ok();
    std::fs::create_dir_all(base.join("queues")).ok();
    std::fs::create_dir_all(base.join("logs")).ok();
}

fn atomic_write(file_path: &Path, data: &serde_json::Value) -> std::io::Result<()> {
    let tmp = file_path.with_extension("json.tmp");
    let json = serde_json::to_string_pretty(data)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(&tmp, json)?;
    std::fs::rename(&tmp, file_path)?;
    Ok(())
}

fn read_json_file<T: DeserializeOwned>(file_path: &Path) -> Vec<T> {
    match std::fs::read_to_string(file_path) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

fn read_queue_file(file_path: &Path) -> Option<GroupQueueSnapshot> {
    let content = std::fs::read_to_string(file_path).ok()?;
    serde_json::from_str(&content).ok()
}

pub fn load_all_queues() -> Vec<GroupQueueSnapshot> {
    let dir = data_dir().join("queues");
    let mut out = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("json") {
                if let Some(snap) = read_queue_file(&p) {
                    out.push(snap);
                }
            }
        }
    }
    out
}

pub fn load_all() -> PersistedData {
    let base = data_dir();
    ensure_dirs(&base);
    PersistedData {
        agents: read_json_file(&base.join("agents.json")),
        groups: read_json_file(&base.join("groups.json")),
        members: read_json_file(&base.join("members.json")),
        tasks: read_json_file(&base.join("tasks.json")),
        messages: read_json_file(&base.join("messages.json")),
        queues: load_all_queues()
            .into_iter()
            .map(|q| (q.group_id.clone(), q))
            .collect(),
    }
}

fn schedule_save(kind: DebounceKind, key: String, data: serde_json::Value) {
    let seq = DEBOUNCE_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    {
        let mut timers = DEBOUNCE_TIMERS.lock();
        timers.insert(
            key.clone(),
            DebounceEntry {
                handle_id: seq,
                payload: data,
                kind,
            },
        );
    }
    let key_cloned = key;
    let _ = tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(500)).await;
        flush_one(&key_cloned, seq);
    });
}

pub fn schedule_save_entity(entity_name: &str, data: serde_json::Value) {
    schedule_save(DebounceKind::Entity, entity_name.to_string(), data);
}

pub fn schedule_save_queue(group_id: &str, data: serde_json::Value) {
    schedule_save(DebounceKind::Queue, group_id.to_string(), data);
}

fn flush_one(key: &str, seq: u64) {
    let entry = {
        let mut timers = DEBOUNCE_TIMERS.lock();
        match timers.get(key).map(|e| (e.handle_id == seq, e.clone())) {
            Some((is_latest, entry)) if is_latest => {
                timers.remove(key);
                Some(entry)
            }
            _ => None,
        }
    };
    if let Some(entry) = entry {
        let base = data_dir();
        let path = match entry.kind {
            DebounceKind::Entity => base.join(format!("{key}.json")),
            DebounceKind::Queue => {
                let d = base.join("queues");
                std::fs::create_dir_all(&d).ok();
                d.join(format!("{key}.json"))
            }
        };
        if let Err(e) = atomic_write(&path, &entry.payload) {
            log::error!("[persistence] save {key} failed: {e}");
        }
    }
}

pub fn flush_all() {
    let entries: Vec<(String, DebounceEntry)> = DEBOUNCE_TIMERS.lock().drain().collect();
    let base = data_dir();
    for (key, entry) in entries {
        let path = match entry.kind {
            DebounceKind::Entity => base.join(format!("{key}.json")),
            DebounceKind::Queue => base.join("queues").join(format!("{key}.json")),
        };
        let _ = atomic_write(&path, &entry.payload);
    }
}

/// 列出群组工作目录下的文件（供 group_list_files 命令）
pub fn list_files(group_id: &str) -> Vec<GroupFile> {
    let dir = data_dir().join("group_files").join(group_id);
    let mut out = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if let Ok(meta) = entry.metadata() {
                if meta.is_file() {
                    let modified = meta
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| {
                            chrono::DateTime::<chrono::Utc>::from_timestamp(d.as_secs() as i64, 0)
                                .map(|dt| dt.to_rfc3339())
                                .unwrap_or_default()
                        })
                        .unwrap_or_default();
                    out.push(GroupFile {
                        name: p
                            .file_name()
                            .and_then(|n| n.to_str())
                            .unwrap_or("")
                            .to_string(),
                        size: meta.len(),
                        modified_at: modified,
                    });
                }
            }
        }
    }
    out
}

/// 群组工作目录
pub fn group_work_dir(group_id: &str) -> PathBuf {
    data_dir().join("group_files").join(group_id)
}
