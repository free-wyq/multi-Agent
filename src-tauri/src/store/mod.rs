//! 内存 Store —— 对应 TS `main/store/store.ts`
//! 内存中的多 Map 索引 + JSON 持久化（防抖 + 原子写）

pub mod persistence;
pub mod shared_state;
pub mod types;

use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::Arc;

use types::*;

/// 全局 store 单例
static STORE: once_cell::sync::Lazy<Arc<Store>> =
    once_cell::sync::Lazy::new(|| Arc::new(Store::default()));

pub fn store() -> Arc<Store> {
    STORE.clone()
}

#[derive(Default)]
pub struct Store {
    agents: RwLock<HashMap<String, AgentDefinition>>,
    groups: RwLock<HashMap<String, Group>>,
    members: RwLock<HashMap<String, GroupMember>>,
    tasks: RwLock<HashMap<String, Task>>,
    messages: RwLock<Vec<Message>>,
    queues: RwLock<HashMap<String, GroupQueueSnapshot>>,
}

impl Store {
    /// 从磁盘加载全部数据到内存
    pub fn load(&self) {
        let data = persistence::load_all();
        {
            let mut g = self.agents.write();
            g.clear();
            for a in data.agents {
                g.insert(a.id.clone(), a);
            }
        }
        {
            let mut g = self.groups.write();
            g.clear();
            for g_ in data.groups {
                g.insert(g_.id.clone(), g_);
            }
        }
        {
            let mut g = self.members.write();
            g.clear();
            for m in data.members {
                g.insert(m.id.clone(), m);
            }
        }
        {
            let mut g = self.tasks.write();
            g.clear();
            for t in data.tasks {
                g.insert(t.id.clone(), t);
            }
        }
        {
            let mut g = self.messages.write();
            g.clear();
            g.extend(data.messages);
        }
        {
            let mut g = self.queues.write();
            g.clear();
            for (k, v) in data.queues {
                g.insert(k, v);
            }
        }
        log::info!(
            "[Store] loaded: agents={}, groups={}, members={}, tasks={}, messages={}",
            self.agents.read().len(),
            self.groups.read().len(),
            self.members.read().len(),
            self.tasks.read().len(),
            self.messages.read().len()
        );
    }

    /// 列出所有群组（用于 engine 注册时遍历）
    pub fn list_all_groups(&self) -> Vec<Group> {
        self.groups.read().values().cloned().collect()
    }

    // ── Agents ──────────────────────────────────────────────

    pub fn list_agents(&self) -> Vec<AgentDefinition> {
        self.agents.read().values().cloned().collect()
    }

    pub fn get_agent(&self, id: &str) -> Option<AgentDefinition> {
        self.agents.read().get(id).cloned()
    }

    pub fn upsert_agent(&self, agent: AgentDefinition) -> AgentDefinition {
        {
            let mut g = self.agents.write();
            g.insert(agent.id.clone(), agent.clone());
        }
        self.persist_agents();
        agent
    }

    pub fn delete_agent(&self, id: &str) -> bool {
        let removed = self.agents.write().remove(id).is_some();
        if removed {
            self.persist_agents();
        }
        removed
    }

    fn persist_agents(&self) {
        let v: Vec<AgentDefinition> = self.list_agents();
        persistence::schedule_save_entity("agents", serde_json::to_value(&v).unwrap());
    }

    // ── Groups ──────────────────────────────────────────────

    pub fn list_groups(&self) -> Vec<Group> {
        self.groups.read().values().cloned().collect()
    }

    pub fn get_group(&self, id: &str) -> Option<Group> {
        self.groups.read().get(id).cloned()
    }

    pub fn upsert_group(&self, group: Group) -> Group {
        {
            let mut g = self.groups.write();
            g.insert(group.id.clone(), group.clone());
        }
        self.persist_groups();
        group
    }

    pub fn delete_group(&self, id: &str) -> bool {
        let removed = self.groups.write().remove(id).is_some();
        if removed {
            // 级联删除成员与队列
            {
                let mut m = self.members.write();
                m.retain(|_, v| v.group_id != id);
            }
            self.queues.write().remove(id);
            self.persist_groups();
            self.persist_members();
        }
        removed
    }

    fn persist_groups(&self) {
        let v: Vec<Group> = self.list_groups();
        persistence::schedule_save_entity("groups", serde_json::to_value(&v).unwrap());
    }

    // ── Members ────────────────────────────────────────────

    pub fn list_group_members(&self, group_id: &str) -> Vec<GroupMember> {
        self.members
            .read()
            .values()
            .filter(|m| m.group_id == group_id)
            .cloned()
            .collect()
    }

    pub fn list_group_members_with_agent(&self, group_id: &str) -> Vec<GroupMemberWithAgent> {
        let members = self.list_group_members(group_id);
        let agents = self.agents.read();
        members
            .into_iter()
            .filter_map(|m| {
                let agent = agents.get(&m.agent_id)?;
                Some(GroupMemberWithAgent {
                    agent_name: agent.name.clone(),
                    agent_role: agent.role.clone(),
                    member: m,
                })
            })
            .collect()
    }

    pub fn add_member(&self, member: GroupMember) -> GroupMember {
        {
            let mut g = self.members.write();
            g.insert(member.id.clone(), member.clone());
        }
        self.persist_members();
        member
    }

    pub fn remove_member(&self, id: &str) -> bool {
        let removed = self.members.write().remove(id).is_some();
        if removed {
            self.persist_members();
        }
        removed
    }

    fn persist_members(&self) {
        let v: Vec<GroupMember> = self.members.read().values().cloned().collect();
        persistence::schedule_save_entity("members", serde_json::to_value(&v).unwrap());
    }

    // ── Tasks ──────────────────────────────────────────────

    pub fn list_tasks(&self) -> Vec<Task> {
        self.tasks.read().values().cloned().collect()
    }

    pub fn list_tasks_by_group(&self, group_id: &str) -> Vec<Task> {
        self.tasks
            .read()
            .values()
            .filter(|t| t.group_id == group_id)
            .cloned()
            .collect()
    }

    pub fn get_task(&self, id: &str) -> Option<Task> {
        self.tasks.read().get(id).cloned()
    }

    pub fn upsert_task(&self, task: Task) -> Task {
        {
            let mut g = self.tasks.write();
            g.insert(task.id.clone(), task.clone());
        }
        self.persist_tasks();
        task
    }

    pub fn delete_task(&self, id: &str) -> bool {
        let removed = self.tasks.write().remove(id).is_some();
        if removed {
            self.persist_tasks();
        }
        removed
    }

    fn persist_tasks(&self) {
        let v: Vec<Task> = self.list_tasks();
        persistence::schedule_save_entity("tasks", serde_json::to_value(&v).unwrap());
    }

    // ── Messages ───────────────────────────────────────────

    pub fn list_messages_by_group(&self, group_id: &str) -> Vec<Message> {
        self.messages
            .read()
            .iter()
            .filter(|m| m.group_id == group_id)
            .cloned()
            .collect()
    }

    /// 列出所有消息（供按 task_id 过滤）
    pub fn list_all_messages(&self) -> Vec<Message> {
        self.messages.read().clone()
    }

    pub fn add_message(&self, msg: Message) -> Message {
        {
            let mut g = self.messages.write();
            g.push(msg.clone());
        }
        self.persist_messages();
        msg
    }

    pub fn clear_messages_by_group(&self, group_id: &str) {
        {
            let mut g = self.messages.write();
            g.retain(|m| m.group_id != group_id);
        }
        self.persist_messages();
    }

    fn persist_messages(&self) {
        let v: Vec<Message> = self.messages.read().clone();
        persistence::schedule_save_entity("messages", serde_json::to_value(&v).unwrap());
    }

    // ── Queues (A2A SharedStateCenter) ─────────────────────

    pub fn get_queue(&self, group_id: &str) -> GroupQueueSnapshot {
        self.queues
            .read()
            .get(group_id)
            .cloned()
            .unwrap_or_else(|| GroupQueueSnapshot {
                group_id: group_id.to_string(),
                tasks: vec![],
                notifies: vec![],
            })
    }

    pub fn save_queue(&self, snap: GroupQueueSnapshot) {
        let gid = snap.group_id.clone();
        {
            let mut g = self.queues.write();
            g.insert(gid.clone(), snap.clone());
        }
        persistence::schedule_save_queue(&gid, serde_json::to_value(&snap).unwrap());
    }

    pub fn list_files_for_group(&self, group_id: &str) -> Vec<GroupFile> {
        persistence::list_files(group_id)
    }
}

/// 生成带前缀的 uuid
pub fn new_id(prefix: &str) -> String {
    format!("{prefix}_{}", uuid::Uuid::new_v4().simple())
}

/// 当前 ISO8601 时间字符串
pub fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}
