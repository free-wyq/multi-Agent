//! SharedStateCenter —— A2A 共享状态中心
//!
//! 对应 TS `main/store/shared-state.ts`：
//! - TaskQueue: 向某个 agent 收件箱「扔任务字条」
//! - NotifyQueue: 任务完成/状态变更通知
//! - pollInbox: 接收者主动轮询取信
//!
//! 真消息驱动改造：每个 (group, agent) 对应一个 tokio mpsc 收件箱 channel，
//! push_* 直接 send，引擎端 select! recv，不再 100ms 轮询。
//! 同时保留 pollInbox 兼容语义与 JSON 持久化。

use crate::store::persistence;
use crate::store::types::*;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;

/// 收件箱消息（任务字条 或 通知字条）
#[derive(Debug, Clone)]
pub enum InboxItem {
    Task(TaskQueueItem),
    Notify(NotifyQueueItem),
}

/// 一个 group 的共享状态（任务队列 + 通知队列 + 各 agent 的 mpsc sender）
struct GroupState {
    tasks: Vec<TaskQueueItem>,
    notifies: Vec<NotifyQueueItem>,
    /// 每个 agent 的收件箱 sender（消息驱动推送）
    inboxes: HashMap<String, mpsc::UnboundedSender<InboxItem>>,
}

impl GroupState {
    fn new() -> Self {
        Self {
            tasks: vec![],
            notifies: vec![],
            inboxes: HashMap::new(),
        }
    }
}

/// 全局共享状态中心
#[derive(Default)]
pub struct SharedStateCenter {
    groups: Mutex<HashMap<String, GroupState>>,
}

static SHARED: once_cell::sync::Lazy<Arc<SharedStateCenter>> =
    once_cell::sync::Lazy::new(|| Arc::new(SharedStateCenter::default()));

pub fn shared_state() -> Arc<SharedStateCenter> {
    SHARED.clone()
}

impl SharedStateCenter {
    fn group<'a>(&'a self, map: &'a mut HashMap<String, GroupState>, group_id: &str) -> &'a mut GroupState {
        map.entry(group_id.to_string()).or_insert_with(GroupState::new)
    }

    /// 注册一个 agent 的收件箱 channel，返回 receiver
    pub fn register_inbox(
        &self,
        group_id: &str,
        agent_id: &str,
    ) -> mpsc::UnboundedReceiver<InboxItem> {
        let (tx, rx) = mpsc::unbounded_channel();
        {
            let mut map = self.groups.lock();
            let g = self.group(&mut map, group_id);
            g.inboxes.insert(agent_id.to_string(), tx);
        }
        rx
    }

    /// 注销收件箱
    pub fn unregister_inbox(&self, group_id: &str, agent_id: &str) {
        let mut map = self.groups.lock();
        if let Some(g) = map.get_mut(group_id) {
            g.inboxes.remove(agent_id);
        }
    }

    /// 向某 agent 收件箱「扔任务字条」，并唤醒其 engine
    pub fn push_task(&self, params: PushTaskParams) -> TaskQueueItem {
        let item = TaskQueueItem {
            id: crate::store::new_id("tq"),
            group_id: params.group_id,
            sender_id: params.sender_id,
            receiver_id: params.receiver_id,
            content: params.content,
            data: params.data,
            created_at: crate::store::now_iso(),
            status: "pending".into(),
            claimed_by: None,
            result: None,
            result_data: None,
            completed_at: None,
        };
        let inbox_tx;
        {
            let mut map = self.groups.lock();
            let g = self.group(&mut map, &item.group_id);
            g.tasks.push(item.clone());
            // 截断防止无限增长
            if g.tasks.len() > 2000 {
                let drop_n = g.tasks.len() - 2000;
                g.tasks.drain(0..drop_n);
            }
            inbox_tx = g.inboxes.get(&item.receiver_id).cloned();
        }
        self.persist(&item.group_id);
        if let Some(tx) = inbox_tx {
            let _ = tx.send(InboxItem::Task(item.clone()));
        }
        item
    }

    /// Agent 认领一个属于自己的 pending 任务
    pub fn claim_task(&self, group_id: &str, agent_id: &str, instance_id: &str) -> Option<TaskQueueItem> {
        let mut map = self.groups.lock();
        let g = self.group(&mut map, group_id);
        let idx = g
            .tasks
            .iter()
            .position(|t| t.receiver_id == agent_id && t.status == "pending")?;
        g.tasks[idx].status = "claimed".into();
        g.tasks[idx].claimed_by = Some(instance_id.to_string());
        let item = g.tasks[idx].clone();
        drop(map);
        self.persist(group_id);
        Some(item)
    }

    /// 完成任务并自动发布 task_complete/task_failed 通知
    pub fn complete_task(
        &self,
        task_id: &str,
        success: bool,
        result: Option<String>,
        result_data: Option<serde_json::Value>,
    ) -> Option<TaskQueueItem> {
        // 先在锁内找到并更新任务，提取通知所需字段
        let updated: Option<(TaskQueueItem, String, String)> = {
            let mut map = self.groups.lock();
            let mut found: Option<(TaskQueueItem, String, String)> = None;
            for g in map.values_mut() {
                if let Some(item) = g.tasks.iter_mut().find(|t| t.id == task_id) {
                    item.status = if success { "completed" } else { "failed" }.into();
                    item.result = result.clone();
                    item.result_data = result_data.clone();
                    item.completed_at = Some(crate::store::now_iso());
                    let notify_receiver = item.sender_id.clone();
                    let notify_sender = item.receiver_id.clone();
                    let task_item = item.clone();
                    found = Some((task_item, notify_sender, notify_receiver));
                    break;
                }
            }
            found
        };

        let (task_item, notify_sender, notify_receiver) = updated?;
        let group_id = task_item.group_id.clone();
        self.persist(&group_id);

        // 自动发布通知给原 sender（锁外执行，避免重入）
        self.push_notify(PushNotifyParams {
            group_id: group_id.clone(),
            kind: if success { "task_complete" } else { "task_failed" }.into(),
            sender_id: notify_sender,
            receiver_id: notify_receiver,
            content: result
                .clone()
                .unwrap_or_else(|| if success { "任务完成".into() } else { "任务失败".into() }),
            data: Some(serde_json::json!({
                "task_id": task_id,
                "extra": result_data.clone().unwrap_or(serde_json::Value::Null),
            })),
        });
        Some(task_item)
    }

    /// 列出某群的所有任务
    pub fn list_tasks(&self, group_id: &str) -> Vec<TaskQueueItem> {
        let mut map = self.groups.lock();
        self.group(&mut map, group_id).tasks.clone()
    }

    /// 向通知队列投递通知，并唤醒目标 agent
    pub fn push_notify(&self, params: PushNotifyParams) -> NotifyQueueItem {
        let item = NotifyQueueItem {
            id: crate::store::new_id("nq"),
            group_id: params.group_id,
            kind: params.kind,
            sender_id: params.sender_id,
            receiver_id: params.receiver_id,
            content: params.content,
            data: params.data,
            created_at: crate::store::now_iso(),
        };
        let inbox_tx;
        {
            let mut map = self.groups.lock();
            let g = self.group(&mut map, &item.group_id);
            g.notifies.push(item.clone());
            if g.notifies.len() > 500 {
                let drop_n = g.notifies.len() - 500;
                g.notifies.drain(0..drop_n);
            }
            // 定向通知：唤醒目标 agent；广播：唤醒群内所有 agent
            inbox_tx = if item.receiver_id == "broadcast" {
                // 收集所有 sender（clone 出来，避免持锁时 send）
                let senders: Vec<mpsc::UnboundedSender<InboxItem>> =
                    g.inboxes.values().cloned().collect();
                // 返回一个标记，外面逐个 send
                drop(map);
                for tx in senders {
                    let _ = tx.send(InboxItem::Notify(item.clone()));
                }
                None
            } else {
                g.inboxes.get(&item.receiver_id).cloned()
            };
        }
        self.persist(&item.group_id);
        if let Some(tx) = inbox_tx {
            let _ = tx.send(InboxItem::Notify(item.clone()));
        }
        item
    }

    /// 轮询收件箱（兼容旧 API）：返回属于该 agent 的 pending 任务 + 定向/广播通知
    pub fn poll_inbox(
        &self,
        group_id: &str,
        agent_id: &str,
        since: Option<&str>,
    ) -> (Vec<TaskQueueItem>, Vec<NotifyQueueItem>) {
        let mut map = self.groups.lock();
        let g = self.group(&mut map, group_id);
        let tasks: Vec<TaskQueueItem> = g
            .tasks
            .iter()
            .filter(|t| t.receiver_id == agent_id && t.status == "pending")
            .cloned()
            .collect();
        let notifies: Vec<NotifyQueueItem> = g
            .notifies
            .iter()
            .filter(|n| {
                if n.receiver_id != "broadcast" && n.receiver_id != agent_id {
                    return false;
                }
                match since {
                    Some(s) => n.created_at.as_str() > s,
                    None => true,
                }
            })
            .cloned()
            .collect();
        (tasks, notifies)
    }

    /// 获取群组快照
    pub fn snapshot(&self, group_id: &str) -> GroupQueueSnapshot {
        let mut map = self.groups.lock();
        let g = self.group(&mut map, group_id);
        GroupQueueSnapshot {
            group_id: group_id.to_string(),
            tasks: g.tasks.clone(),
            notifies: g.notifies.clone(),
        }
    }

    /// 从磁盘恢复某群的队列
    pub fn restore(&self, snap: GroupQueueSnapshot) {
        let mut map = self.groups.lock();
        let g = self.group(&mut map, &snap.group_id);
        g.tasks = snap.tasks;
        g.notifies = snap.notifies;
    }

    fn persist(&self, group_id: &str) {
        let snap = self.snapshot(group_id);
        persistence::schedule_save_queue(group_id, serde_json::to_value(&snap).unwrap());
    }
}

#[derive(Debug, Clone)]
pub struct PushTaskParams {
    pub group_id: String,
    pub sender_id: String,
    pub receiver_id: String,
    pub content: String,
    pub data: Option<serde_json::Value>,
}

#[derive(Debug, Clone)]
pub struct PushNotifyParams {
    pub group_id: String,
    pub kind: String,
    pub sender_id: String,
    pub receiver_id: String,
    pub content: String,
    pub data: Option<serde_json::Value>,
}

/// 从磁盘加载所有队列到内存（启动时）
pub fn load_all_queues() {
    let snapshots = persistence::load_all_queues();
    let ss = shared_state();
    let n = snapshots.len();
    for snap in snapshots {
        ss.restore(snap);
    }
    log::info!("[SharedState] restored {n} group queue(s)");
}
