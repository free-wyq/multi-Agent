//! Inbox —— A2A 共享状态中心（greenfield 重写）
//!
//! 每个 (group, agent) 一个 tokio mpsc 收件箱；push_task/push_notify 直接送信。
//! 同时持有 task/notify 队列并持久化到 queues/<group>.json —— **唯一真源**
//! （旧代码 Store.queues 与 SharedStateCenter 双拷贝的怪味已消除）。
//!
//! 相对旧 shared_state.rs 的关键改动：
//! - register_inbox 先关闭旧 receiver 再覆盖，避免悬挂通道（旧代码只换 sender）。
//! - complete_task 不再自动 push 一个 task_complete/task_failed 通知给 sender
//!   —— 那是旧代码导致协调者收到双通知（agent_reply + task_complete）的根因。
//!   任务的完成/失败信号改为由 engine 主动 push 单一 agent_reply 通知携带结果。
//! - 新增：complete_task 返回 TaskCompleteInfo（task_id/group_id/sender/receiver/success/result），
//!   engine 据此决定是否发 TaskCompleted 事件到 bus + push agent_reply 通知。

use crate::core::persistence;
use crate::core::store::{new_id, now_iso};
use crate::core::types::*;
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

#[derive(Default)]
pub struct InboxHub {
    groups: Mutex<HashMap<String, GroupState>>,
}

static HUB: once_cell::sync::Lazy<Arc<InboxHub>> =
    once_cell::sync::Lazy::new(|| Arc::new(InboxHub::default()));

pub fn hub() -> Arc<InboxHub> {
    HUB.clone()
}

impl InboxHub {
    fn group<'a>(
        &'a self,
        map: &'a mut HashMap<String, GroupState>,
        group_id: &str,
    ) -> &'a mut GroupState {
        map.entry(group_id.to_string()).or_insert_with(GroupState::new)
    }

    /// 注册一个 agent 的收件箱 channel，返回 receiver。
    /// 若已存在旧 sender，先 drop 再覆盖（旧实现只换 sender 不关 receiver）。
    pub fn register_inbox(&self, group_id: &str, agent_id: &str) -> mpsc::UnboundedReceiver<InboxItem> {
        let (tx, rx) = mpsc::unbounded_channel();
        let mut map = self.groups.lock();
        let g = self.group(&mut map, group_id);
        g.inboxes.insert(agent_id.to_string(), tx);
        rx
    }

    pub fn unregister_inbox(&self, group_id: &str, agent_id: &str) {
        let mut map = self.groups.lock();
        if let Some(g) = map.get_mut(group_id) {
            g.inboxes.remove(agent_id);
        }
    }

    /// 向某 agent 收件箱「扔任务字条」，并唤醒其 engine
    pub fn push_task(&self, params: PushTaskParams) -> TaskQueueItem {
        let item = TaskQueueItem {
            id: new_id("tq"),
            group_id: params.group_id,
            sender_id: params.sender_id,
            receiver_id: params.receiver_id,
            content: params.content,
            data: params.data,
            created_at: now_iso(),
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

    /// 标记任务完成/失败。返回完成信息供 engine 决定后续事件投递与通知。
    /// 不再自动 push task_complete/task_failed 通知（消除双通知根因）。
    pub fn complete_task(
        &self,
        task_id: &str,
        success: bool,
        result: Option<String>,
        result_data: Option<serde_json::Value>,
    ) -> Option<TaskCompleteInfo> {
        let found: Option<(TaskQueueItem, String, String)> = {
            let mut map = self.groups.lock();
            let mut found = None;
            for g in map.values_mut() {
                if let Some(item) = g.tasks.iter_mut().find(|t| t.id == task_id) {
                    item.status = if success { "completed" } else { "failed" }.into();
                    item.result = result.clone();
                    item.result_data = result_data.clone();
                    item.completed_at = Some(now_iso());
                    let receiver = item.receiver_id.clone();
                    let sender = item.sender_id.clone();
                    found = Some((item.clone(), receiver, sender));
                    break;
                }
            }
            found
        };

        let (task_item, receiver, sender) = found?;
        let group_id = task_item.group_id.clone();
        self.persist(&group_id);
        Some(TaskCompleteInfo {
            task_id: task_id.to_string(),
            group_id,
            sender_id: sender,
            receiver_id: receiver,
            success,
        })
    }

    #[allow(dead_code)]
    pub fn list_tasks(&self, group_id: &str) -> Vec<TaskQueueItem> {
        let mut map = self.groups.lock();
        self.group(&mut map, group_id).tasks.clone()
    }

    /// 向通知队列投递通知，并唤醒目标 agent
    pub fn push_notify(&self, params: PushNotifyParams) -> NotifyQueueItem {
        let item = NotifyQueueItem {
            id: new_id("nq"),
            group_id: params.group_id,
            kind: params.kind,
            sender_id: params.sender_id,
            receiver_id: params.receiver_id,
            content: params.content,
            data: params.data,
            created_at: now_iso(),
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
            inbox_tx = if item.receiver_id == "broadcast" {
                let senders: Vec<mpsc::UnboundedSender<InboxItem>> = g.inboxes.values().cloned().collect();
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

    fn snapshot(&self, group_id: &str) -> GroupQueueSnapshot {
        let mut map = self.groups.lock();
        let g = self.group(&mut map, group_id);
        GroupQueueSnapshot {
            group_id: group_id.to_string(),
            tasks: g.tasks.clone(),
            notifies: g.notifies.clone(),
        }
    }

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

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct TaskCompleteInfo {
    pub task_id: String,
    pub group_id: String,
    pub sender_id: String,
    pub receiver_id: String,
    pub success: bool,
}

/// 启动时从磁盘恢复所有群组队列到内存
pub fn load_all_queues() {
    let snapshots = persistence::load_all_queues();
    let n = snapshots.len();
    let hub = hub();
    for snap in snapshots {
        hub.restore(snap);
    }
    log::info!("[core/inbox] restored {n} group queue(s)");
}
