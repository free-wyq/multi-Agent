//! JSON 持久化 —— 与 TS `main/store/persistence.ts` 语义一致
//! - 每个实体一个 JSON 文件
//! - 写入 500ms 防抖 + 原子写入（先 .tmp 再 rename）
//! - 启动时加载填充内存

use crate::store::types::*;
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

/// 全局自增 id，用于标记最新一次防抖（旧的失效）
static DEBOUNCE_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

// 运行时数据目录（init 时设置）
static RUNTIME_DATA_DIR: LazyLock<Mutex<PathBuf>> =
    LazyLock::new(|| Mutex::new(PathBuf::from(".")));

/// 初始化数据目录（在应用启动时调用一次）
pub fn init(data_dir: &Path) {
    set_data_dir(data_dir);
    ensure_dirs(data_dir);
}

fn set_data_dir(p: &Path) {
    let mut guard = RUNTIME_DATA_DIR.lock();
    *guard = p.to_path_buf();
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

/// 原子写入 JSON 文件：先写 .tmp 再 rename
fn atomic_write(file_path: &Path, data: &serde_json::Value) -> std::io::Result<()> {
    let tmp = file_path.with_extension("json.tmp");
    let json = serde_json::to_string_pretty(data)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(&tmp, json)?;
    std::fs::rename(&tmp, file_path)?;
    Ok(())
}

/// 读取 JSON 文件并反序列化；文件不存在或损坏时返回空 Vec
fn read_json_file<T: DeserializeOwned>(file_path: &Path) -> Vec<T> {
    match std::fs::read_to_string(file_path) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

/// 读取单个群组队列快照
fn read_queue_file(file_path: &Path) -> Option<GroupQueueSnapshot> {
    let content = std::fs::read_to_string(file_path).ok()?;
    serde_json::from_str(&content).ok()
}

/// 读取所有队列快照
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

/// 启动时加载所有实体数据
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

/// 调度一次防抖写入（500ms）。kind 决定写实体文件还是队列文件。
pub fn schedule_save(kind: DebounceKind, key: String, data: serde_json::Value) {
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
    let key_cloned = key.clone();
    // 委托给 Tauri 全局 async runtime 派发防抖刷盘
    let _ = tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(500)).await;
        flush_one(&key_cloned, seq);
    });
}

/// 实体防抖保存
pub fn schedule_save_entity(entity_name: &str, data: serde_json::Value) {
    schedule_save(DebounceKind::Entity, entity_name.to_string(), data);
}

/// 队列防抖保存
pub fn schedule_save_queue(group_id: &str, data: serde_json::Value) {
    schedule_save(DebounceKind::Queue, group_id.to_string(), data);
}

/// 执行单条防抖刷盘：仅当 seq 仍是最新时才写
fn flush_one(key: &str, seq: u64) {
    let entry = {
        let mut timers = DEBOUNCE_TIMERS.lock();
        let take = timers.get(key).map(|e| (e.handle_id == seq, e.clone()));
        match take {
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
            log::error!("Failed to save {key}: {e}");
        }
    }
}

/// 列出群组文件
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

/// 立即刷盘：清空所有防抖并写入
pub fn flush_all() {
    let entries: Vec<(String, DebounceEntry)> = {
        let mut timers = DEBOUNCE_TIMERS.lock();
        timers.drain().collect()
    };
    for (key, entry) in entries {
        let base = data_dir();
        let path = match entry.kind {
            DebounceKind::Entity => base.join(format!("{key}.json")),
            DebounceKind::Queue => base.join("queues").join(format!("{key}.json")),
        };
        let _ = atomic_write(&path, &entry.payload);
    }
}

/// 群组工作目录（跨平台）
pub fn group_work_dir(group_id: &str) -> PathBuf {
    data_dir().join("group_files").join(group_id)
}
