//! AgentEngine —— 常驻智能体引擎（A2A + 真消息驱动）
//!
//! 对应 TS `main/agent-engine/engine.ts` + `brain.ts` + `coordinator-brain.ts` + `registry.ts`
//! 核心改造：用 tokio task + mpsc channel 替代 setInterval 100ms 轮询
//! - 每个 engine 一个 tokio task，select! 监听收件箱 channel
//! - 任务执行非阻塞（tokio::spawn）
//! - coordinator 走调度大脑，普通成员走 brainDecide

use crate::bus;
use crate::llm;
use crate::prompts;
use crate::runtime::ClaudeCodeRuntime;
use crate::store;
use crate::store::shared_state::{self, InboxItem, PushNotifyParams, PushTaskParams};
use crate::store::types::*;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use tokio::sync::mpsc;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainDecision {
    pub action: String,
    pub content: String,
    #[serde(default)]
    pub reasoning: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DispatchStep {
    pub step: i64,
    pub agent_id: String,
    pub agent_name: String,
    pub instruction: String,
    #[serde(default)]
    pub depends_on: Vec<i64>,
    pub status: String,
    #[serde(default)]
    pub result: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CoordinatorBrainDecision {
    pub action: String,
    pub content: String,
    #[serde(default)]
    pub plan: Vec<DispatchStep>,
    #[serde(default)]
    pub next_step: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum EngineStatus {
    Idle,
    Thinking,
    Executing,
    Offline,
}

struct EngineInner {
    status: EngineStatus,
    current_task_id: Option<String>,
    memory: Vec<MemoryEntry>,
    dispatch_plan: Vec<DispatchStep>,
    dispatch_step: i64,
    processing_task_ids: HashSet<String>,
    recent_routes: HashMap<String, f64>,
}

#[derive(Clone)]
struct MemoryEntry {
    role: String,
    content: String,
}

pub struct AgentEngine {
    pub id: String,
    pub name: String,
    pub role: String,
    pub system_prompt: String,
    pub group_id: String,
    inner: Arc<Mutex<EngineInner>>,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
}

impl AgentEngine {
    pub fn new(agent: &AgentDefinition, group_id: &str) -> Self {
        Self {
            id: agent.id.clone(),
            name: agent.name.clone(),
            role: agent.role.clone(),
            system_prompt: agent.system_prompt.clone(),
            group_id: group_id.to_string(),
            inner: Arc::new(Mutex::new(EngineInner {
                status: EngineStatus::Idle,
                current_task_id: None,
                memory: vec![],
                dispatch_plan: vec![],
                dispatch_step: 0,
                processing_task_ids: HashSet::new(),
                recent_routes: HashMap::new(),
            })),
            shutdown: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        }
    }

    /// 启动引擎：注册收件箱 channel，spawn 一个 tokio task 消费
    pub fn start(self: &Arc<Self>) {
        self.shutdown
            .store(false, std::sync::atomic::Ordering::SeqCst);
        {
            let mut g = self.inner.lock();
            g.status = EngineStatus::Idle;
        }
        let rx = shared_state::shared_state().register_inbox(&self.group_id, &self.id);
        let engine = self.clone();
        crate::rt::spawn(async move {
            engine.run_loop(rx).await;
        });
        log::info!("[AgentEngine] {} (role={}) started in group {}", self.name, self.role, self.group_id);
    }

    pub fn stop(&self) {
        self.shutdown
            .store(true, std::sync::atomic::Ordering::SeqCst);
        shared_state::shared_state().unregister_inbox(&self.group_id, &self.id);
        let mut g = self.inner.lock();
        g.status = EngineStatus::Offline;
        log::info!("[AgentEngine] {} stopped", self.name);
    }

    /// 主循环：select! 监听收件箱 channel，真消息驱动（非轮询）
    async fn run_loop(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<InboxItem>) {
        loop {
            if self.shutdown.load(std::sync::atomic::Ordering::SeqCst) {
                break;
            }
            // 等待下一条收件箱消息（阻塞直至到达，无空转）
            match rx.recv().await {
                Some(item) => {
                    if let Err(e) = self.handle_inbox(item).await {
                        log::error!("[AgentEngine {}] 处理消息失败: {e}", self.name);
                    }
                }
                None => {
                    // channel 关闭，退出
                    break;
                }
            }
        }
    }

    async fn handle_inbox(&self, item: InboxItem) -> anyhow::Result<()> {
        match item {
            InboxItem::Task(task) => {
                let status = self.inner.lock().status.clone();
                if status == EngineStatus::Executing {
                    // 执行中不消费新任务，重新入队（由 poll 兜底）—— 这里直接跳过，
                    // 该任务仍在 SharedState 中为 pending，下轮可再触发
                    return Ok(());
                }
                {
                    let mut g = self.inner.lock();
                    if g.processing_task_ids.contains(&task.id) {
                        return Ok(());
                    }
                    g.processing_task_ids.insert(task.id.clone());
                }
                let engine_ref = shared_state::shared_state();
                // 认领
                let claimed = engine_ref.claim_task(&self.group_id, &self.id, &self.id);
                match claimed {
                    Some(c) if c.id == task.id => {
                        self.claim_and_execute(task).await;
                    }
                    _ => {
                        self.inner.lock().processing_task_ids.remove(&task.id);
                    }
                }
            }
            InboxItem::Notify(notify) => {
                if notify.sender_id == self.id {
                    return Ok(());
                }
                self.handle_notify(notify).await;
            }
        }
        Ok(())
    }

    // ── 任务执行（非阻塞 spawn）──────────────────────────────

    async fn claim_and_execute(&self, task: TaskQueueItem) {
        {
            let mut g = self.inner.lock();
            g.status = EngineStatus::Executing;
            g.current_task_id = Some(task.id.clone());
        }
        let preview: String = task.content.chars().take(50).collect();
        self.publish_log(
            Some(&task.id),
            &format!("▶ [{}] 开始执行任务: {preview}...", self.name),
        )
        .await;

        let is_coordinator = self.role == "coordinator";
        if is_coordinator {
            // coordinator 不 spawn CLI，走调度大脑（dispatch 流程在 handle_notify 内）
            // 这里直接完成该 task（标记成功）
            shared_state::shared_state().complete_task(
                &task.id,
                true,
                Some("协调者已接收需求".into()),
                None,
            );
            self.reply("已收到需求，开始协调团队成员。").await;
            self.reset_idle(&task.id);
        } else {
            // 普通 agent：spawn CLI，非阻塞
            let agent_def = store::store().get_agent(&self.id);
            match agent_def {
                Some(def) => {
                    let engine_inner = self.inner.clone();
                    let engine_self = self.clone_id_info();
                    let task_id = task.id.clone();
                    let task_content = task.content.clone();
                    let group_id = self.group_id.clone();
                    let agent_id = self.id.clone();
                    let agent_name = self.name.clone();
                    crate::rt::spawn(async move {
                        let runtime = ClaudeCodeRuntime::new(&group_id, def);
                        let eg = engine_self.clone();
                        let result = runtime
                            .execute(&task_content, &task_id, |line| {
                                // 日志：通过 bus 推送（同步闭包，用 blocking_send 不可用，这里直接 fire-and-forget）
                                let _ = bus::try_publish_log(&eg.group_id, Some(&task_id), &eg.id, line);
                            })
                            .await;
                        // 完成
                        shared_state::shared_state().complete_task(
                            &task_id,
                            result.success,
                            Some(result.output.chars().take(500).collect()),
                            Some(serde_json::json!({
                                "exit_code": result.exit_code,
                            })),
                        );
                        let reply_content = if result.success {
                            let snippet: String = result.output.chars().take(200).collect();
                            format!("任务完成 🎉\n{snippet}")
                        } else {
                            format!("执行出错了: {}", result.output)
                        };
                        eg.reply(&reply_content).await;
                        // 向 coordinator 汇报
                        let group = store::store().get_group(&eg.group_id);
                        if let Some(g) = group {
                            if !g.coordinator_id.is_empty() && g.coordinator_id != eg.id {
                                let snippet: String = result.output.chars().take(200).collect();
                                shared_state::shared_state().push_notify(PushNotifyParams {
                                    group_id: eg.group_id.clone(),
                                    kind: "agent_reply".into(),
                                    sender_id: eg.id.clone(),
                                    receiver_id: g.coordinator_id.clone(),
                                    content: format!(
                                        "步骤完成：{}\n\n结果：{}",
                                        task_content,
                                        if snippet.is_empty() { "已完成".into() } else { snippet }
                                    ),
                                    data: Some(serde_json::json!({
                                        "task_id": task_id,
                                        "success": result.success,
                                    })),
                                });
                            }
                        }
                        let _ = (agent_id, agent_name); // 保留引用
                        engine_inner.lock().status = EngineStatus::Idle;
                        engine_inner.lock().current_task_id = None;
                        engine_inner.lock().processing_task_ids.remove(&task_id);
                    });
                }
                None => {
                    shared_state::shared_state().complete_task(
                        &task.id,
                        false,
                        Some("找不到智能体定义".into()),
                        None,
                    );
                    self.publish_log(Some(&task.id), "❌ 找不到智能体定义").await;
                    self.reset_idle(&task.id);
                }
            }
        }
    }

    fn clone_id_info(&self) -> EngineHandle {
        EngineHandle {
            id: self.id.clone(),
            name: self.name.clone(),
            role: self.role.clone(),
            group_id: self.group_id.clone(),
            inner: self.inner.clone(),
            shutdown: self.shutdown.clone(),
        }
    }

    fn reset_idle(&self, task_id: &str) {
        let mut g = self.inner.lock();
        g.status = EngineStatus::Idle;
        g.current_task_id = None;
        g.processing_task_ids.remove(task_id);
    }

    // ── 通知处理 ─────────────────────────────────────────────

    async fn handle_notify(&self, notify: NotifyQueueItem) {
        let content = notify.content.clone();
        let sender = notify.sender_id.clone();

        if self.role == "coordinator" {
            self.handle_notify_as_coordinator(content, sender).await;
            return;
        }

        // 普通成员：brainDecide
        let context = self.build_context();
        let display_msg = if sender != "user" && sender != "coordinator" {
            format!("[来自智能体 {sender}] {content}")
        } else {
            content.clone()
        };

        let config = llm::get_default_config();
        let prompt = prompts::format_brain_prompt(&self.role, &self.name, &context, &display_msg);
        let decision = match llm::chat_completion(&config, vec![llm::ChatMessage {
            role: "user".into(),
            content: prompt,
        }])
        .await
        {
            Ok(raw) => parse_brain_decision(&raw),
            Err(e) => {
                log::warn!("[AgentEngine {}] 大脑决策失败: {e}", self.name);
                BrainDecision {
                    action: "chat".into(),
                    content: "抱歉，我这边有点卡壳，能再说一遍吗？".into(),
                    reasoning: "llm_error".into(),
                }
            }
        };

        {
            let mut g = self.inner.lock();
            g.memory.push(MemoryEntry {
                role: "user".into(),
                content: content.clone(),
            });
        }

        match decision.action.as_str() {
            "chat" => {
                self.reply(&decision.content).await;
                let mut g = self.inner.lock();
                g.memory.push(MemoryEntry {
                    role: "assistant".into(),
                    content: decision.content,
                });
            }
            "execute" => {
                let preview: String = decision.content.chars().take(30).collect();
                self.reply(&format!("收到，我来 {preview}...")).await;
                shared_state::shared_state().push_task(PushTaskParams {
                    group_id: self.group_id.clone(),
                    sender_id: self.id.clone(),
                    receiver_id: self.id.clone(),
                    content: decision.content,
                    data: None,
                });
            }
            _ => {
                self.reply(&decision.content).await;
            }
        }
    }

    /// Coordinator 调度中枢
    async fn handle_notify_as_coordinator(&self, content: String, sender: String) {
        let config = llm::get_default_config();
        let members = store::store().list_group_members_with_agent(&self.group_id);
        let member_list: Vec<(String, String, String)> = members
            .iter()
            .map(|m| (m.member.agent_id.clone(), m.agent_name.clone(), m.agent_role.clone()))
            .collect();

        let conversation: String = {
            let g = self.inner.lock();
            g.memory
                .iter()
                .rev()
                .take(8)
                .rev()
                .map(|m| m.content.clone())
                .collect::<Vec<_>>()
                .join("\n")
        };

        let dispatch_state: String = {
            let g = self.inner.lock();
            if g.dispatch_plan.is_empty() {
                String::new()
            } else {
                g.dispatch_plan
                    .iter()
                    .map(|s| {
                        let icon = match s.status.as_str() {
                            "completed" => "✅",
                            "failed" => "❌",
                            "dispatched" => "🔄",
                            _ => "⏳",
                        };
                        format!("{icon} 步骤{}: {} {icon}", s.step, s.agent_name)
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
            }
        };

        let prompt = prompts::build_coordinator_prompt(
            &self.name,
            &member_list,
            &conversation,
            &dispatch_state,
            &sender,
            &content,
        );

        let decision = match llm::chat_completion(&config, vec![
            llm::ChatMessage {
                role: "system".into(),
                content: prompts::COORDINATOR_SYSTEM.into(),
            },
            llm::ChatMessage {
                role: "user".into(),
                content: prompt,
            },
        ])
        .await
        {
            Ok(raw) => parse_coordinator_decision(&raw),
            Err(e) => {
                log::error!("[CoordinatorBrain] 决策失败: {e}");
                CoordinatorBrainDecision {
                    action: "chat".into(),
                    content: "抱歉，我这边理解有点困难，能再说一次吗？".into(),
                    plan: vec![],
                    next_step: 0,
                }
            }
        };

        {
            let mut g = self.inner.lock();
            g.memory.push(MemoryEntry {
                role: "user".into(),
                content: format!("[{sender}] {content}"),
            });
        }

        match decision.action.as_str() {
            "chat" => {
                self.reply(&decision.content).await;
                let mut g = self.inner.lock();
                g.memory.push(MemoryEntry {
                    role: "assistant".into(),
                    content: decision.content,
                });
            }
            "ask" => {
                self.reply(&decision.content).await;
            }
            "dispatch" if !decision.plan.is_empty() => {
                {
                    let mut g = self.inner.lock();
                    g.dispatch_plan = decision.plan.clone();
                    g.dispatch_step = 0;
                }
                let plan_summary = decision
                    .plan
                    .iter()
                    .map(|s| {
                        let instr: String = s.instruction.chars().take(40).collect();
                        format!("{}. {} → {instr}...", s.step, s.agent_name)
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                self.reply(&format!("📋 已制定协作计划，开始调度：\n{plan_summary}"))
                    .await;
                self.dispatch_next_step().await;
            }
            "continue" => {
                {
                    let mut g = self.inner.lock();
                    if let Some(cur) = g.dispatch_plan.iter_mut().find(|s| s.status == "dispatched") {
                        cur.result = Some(content.clone());
                        cur.status = "completed".into();
                    }
                }
                self.reply(if decision.content.is_empty() {
                    "收到汇报，继续下一步。"
                } else {
                    &decision.content
                })
                .await;
                self.dispatch_next_step().await;
            }
            _ => {
                self.reply(&decision.content).await;
            }
        }
    }

    /// 派发下一个待执行步骤
    async fn dispatch_next_step(&self) {
        let next_idx = {
            let g = self.inner.lock();
            g.dispatch_plan
                .iter()
                .position(|s| {
                    if s.status != "pending" {
                        return false;
                    }
                    s.depends_on.iter().all(|dep_step| {
                        g.dispatch_plan
                            .iter()
                            .any(|d| d.step == *dep_step && d.status == "completed")
                    })
                })
        };

        match next_idx {
            Some(idx) => {
                let mention_msg = {
                    let mut g = self.inner.lock();
                    let step = &mut g.dispatch_plan[idx];
                    step.status = "dispatched".into();
                    let agent_name = step.agent_name.clone();
                    let instruction = step.instruction.clone();
                    let step_num = step.step;
                    g.dispatch_step = step_num;
                    format!(
                        "@{} \n\n{}\n\n完成后请 @我 汇报。",
                        agent_name, instruction
                    )
                };
                let _ = self.reply(&mention_msg).await;
            }
            None => {
                // 检查是否全部完成
                let all_done = {
                    let g = self.inner.lock();
                    g.dispatch_plan
                        .iter()
                        .all(|s| s.status == "completed" || s.status == "failed")
                };
                if all_done {
                    let summary = {
                        let g = self.inner.lock();
                        g.dispatch_plan
                            .iter()
                            .map(|s| {
                                let icon = if s.status == "completed" { "✅" } else { "❌" };
                                let res = s.result.clone().unwrap_or_else(|| s.instruction.clone());
                                format!("{icon} {}: {res}", s.agent_name)
                            })
                            .collect::<Vec<_>>()
                            .join("\n")
                    };
                    {
                        let mut g = self.inner.lock();
                        g.dispatch_plan.clear();
                        g.dispatch_step = 0;
                    }
                    let group_id = self.group_id.clone();
                    let summary_msg = format!("🎉 全部完成！协作结果汇总：\n{summary}");
                    crate::rt::spawn(async move {
                        tokio::time::sleep(std::time::Duration::from_millis(1000)).await;
                        let _ = bus::publish_agent_reply(&group_id, &summary_msg).await;
                        // 同时写入 store
                        store::store().add_message(Message {
                            id: store::new_id("msg"),
                            group_id: group_id.clone(),
                            task_id: None,
                            sender_id: "coordinator".into(),
                            receiver_id: "broadcast".into(),
                            kind: "agent_reply".into(),
                            content: Some(summary_msg),
                            data: None,
                            created_at: store::now_iso(),
                        });
                    });
                }
            }
        }
    }

    // ── 辅助 ─────────────────────────────────────────────────

    async fn reply(&self, content: &str) {
        let msg = store::store().add_message(Message {
            id: store::new_id("msg"),
            group_id: self.group_id.clone(),
            task_id: None,
            sender_id: self.id.clone(),
            receiver_id: "broadcast".into(),
            kind: "agent_reply".into(),
            content: Some(content.to_string()),
            data: None,
            created_at: store::now_iso(),
        });
        bus::publish_message(&self.group_id, &msg).await;
        self.route_mentions(content);
    }

    async fn publish_log(&self, task_id: Option<&str>, line: &str) {
        let msg = Message {
            id: store::new_id("msg"),
            group_id: self.group_id.clone(),
            task_id: task_id.map(|s| s.to_string()),
            sender_id: self.id.clone(),
            receiver_id: "broadcast".into(),
            kind: "task_log".into(),
            content: Some(line.to_string()),
            data: None,
            created_at: store::now_iso(),
        };
        store::store().add_message(msg.clone());
        bus::publish_message(&self.group_id, &msg).await;
    }

    fn build_context(&self) -> String {
        let g = self.inner.lock();
        let recent: Vec<&MemoryEntry> = g.memory.iter().rev().take(5).rev().collect();
        if recent.is_empty() {
            return "（无历史对话）".into();
        }
        recent
            .iter()
            .map(|m| {
                if m.role == "user" {
                    format!("用户: {}", m.content)
                } else {
                    format!("{}: {}", self.name, m.content)
                }
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    fn route_mentions(&self, content: &str) {
        let mentions: Vec<String> = regex_find_mentions(content);
        if mentions.is_empty() {
            return;
        }
        // 防循环：30s 内不重复路由同一目标
        let now = now_secs();
        {
            let mut g = self.inner.lock();
            g.recent_routes.retain(|_, ts| now - *ts < 30.0);
        }
        let members = store::store().list_group_members_with_agent(&self.group_id);
        let agents = store::store().list_agents();

        for mention in mentions {
            if mention == self.id || mention == self.name {
                continue;
            }
            let mut target_id: Option<String> = None;

            // 按 member.agent_id
            if let Some(m) = members.iter().find(|m| m.member.agent_id == mention) {
                target_id = Some(m.member.agent_id.clone());
            }
            // 按 agent.name
            if target_id.is_none() {
                if let Some(a) = agents.iter().find(|a| a.name == mention) {
                    if members.iter().any(|m| m.member.agent_id == a.id) {
                        target_id = Some(a.id.clone());
                    }
                }
            }
            // 按 alias
            if target_id.is_none() {
                for m in &members {
                    if let Some(alias) = &m.member.alias {
                        if !alias.is_empty() && mention.contains(alias) {
                            target_id = Some(m.member.agent_id.clone());
                            break;
                        }
                    }
                }
            }

            let target_id = match target_id {
                Some(t) if t != self.id => t,
                _ => continue,
            };

            // 防循环
            let dup = {
                let mut g = self.inner.lock();
                if g.recent_routes.contains_key(&target_id) {
                    true
                } else {
                    g.recent_routes.insert(target_id.clone(), now);
                    false
                }
            };
            if dup {
                log::info!("防循环: 跳过重复路由 {target_id}");
                continue;
            }

            shared_state::shared_state().push_task(PushTaskParams {
                group_id: self.group_id.clone(),
                sender_id: self.id.clone(),
                receiver_id: target_id,
                content: content.to_string(),
                data: None,
            });
        }
    }
}

/// 用于跨 spawn 传递的最小句柄（无 &self 生命周期）
#[derive(Clone)]
struct EngineHandle {
    id: String,
    name: String,
    role: String,
    group_id: String,
    inner: Arc<Mutex<EngineInner>>,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
}

impl EngineHandle {
    async fn reply(&self, content: &str) {
        let msg = store::store().add_message(Message {
            id: store::new_id("msg"),
            group_id: self.group_id.clone(),
            task_id: None,
            sender_id: self.id.clone(),
            receiver_id: "broadcast".into(),
            kind: "agent_reply".into(),
            content: Some(content.to_string()),
            data: None,
            created_at: store::now_iso(),
        });
        bus::publish_message(&self.group_id, &msg).await;
    }
}

// ── 引擎注册表 ──────────────────────────────────────────────

pub struct AgentRegistry {
    engines: Mutex<HashMap<String, HashMap<String, Arc<AgentEngine>>>>,
}

static REGISTRY: once_cell::sync::Lazy<Arc<AgentRegistry>> =
    once_cell::sync::Lazy::new(|| Arc::new(AgentRegistry::new()));

pub fn registry() -> Arc<AgentRegistry> {
    REGISTRY.clone()
}

impl AgentRegistry {
    fn new() -> Self {
        Self {
            engines: Mutex::new(HashMap::new()),
        }
    }

    pub fn add_engine(&self, group_id: &str, agent: &AgentDefinition) -> Arc<AgentEngine> {
        let mut map = self.engines.lock();
        let group = map.entry(group_id.to_string()).or_insert_with(HashMap::new);
        if let Some(existing) = group.get(&agent.id) {
            return existing.clone();
        }
        let engine = Arc::new(AgentEngine::new(agent, group_id));
        engine.start();
        group.insert(agent.id.clone(), engine.clone());
        engine
    }

    pub fn remove_engine(&self, group_id: &str, agent_id: &str) {
        let mut map = self.engines.lock();
        if let Some(group) = map.get_mut(group_id) {
            if let Some(engine) = group.remove(agent_id) {
                engine.stop();
            }
            if group.is_empty() {
                map.remove(group_id);
            }
        }
    }

    pub fn load_from_store(&self) {
        let groups = store::store().list_all_groups();
        for g in groups {
            if !g.coordinator_id.is_empty() {
                if let Some(coord) = store::store().get_agent(&g.coordinator_id) {
                    self.add_engine(&g.id, &coord);
                }
            }
            let members = store::store().list_group_members_with_agent(&g.id);
            for m in members {
                if let Some(agent) = store::store().get_agent(&m.member.agent_id) {
                    self.add_engine(&g.id, &agent);
                }
            }
        }
    }

    pub fn shutdown_all(&self) {
        let mut map = self.engines.lock();
        for group in map.values_mut() {
            for engine in group.values() {
                engine.stop();
            }
        }
        map.clear();
    }
}

// ── 解析辅助 ──────────────────────────────────────────────

fn parse_brain_decision(raw: &str) -> BrainDecision {
    if let Some(v) = llm::extract_json(raw) {
        let action = v
            .get("action")
            .and_then(|a| a.as_str())
            .unwrap_or("chat")
            .to_string();
        let action = if ["chat", "execute", "ask"].contains(&action.as_str()) {
            action
        } else {
            "chat".into()
        };
        let content = v
            .get("content")
            .and_then(|c| c.as_str())
            .unwrap_or("")
            .to_string();
        let reasoning = v
            .get("reasoning")
            .and_then(|r| r.as_str())
            .unwrap_or("")
            .to_string();
        BrainDecision {
            action,
            content,
            reasoning,
        }
    } else {
        BrainDecision {
            action: "chat".into(),
            content: "抱歉，我这边有点卡壳，能再说一遍吗？".into(),
            reasoning: "parse_failed".into(),
        }
    }
}

fn parse_coordinator_decision(raw: &str) -> CoordinatorBrainDecision {
    if let Some(v) = llm::extract_json(raw) {
        let action = v
            .get("action")
            .and_then(|a| a.as_str())
            .unwrap_or("chat")
            .to_string();
        let action = if ["chat", "dispatch", "ask", "continue"].contains(&action.as_str()) {
            action
        } else {
            "chat".into()
        };
        let content = v
            .get("content")
            .and_then(|c| c.as_str())
            .unwrap_or("")
            .to_string();
        let plan: Vec<DispatchStep> = v
            .get("plan")
            .and_then(|p| p.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|p| serde_json::from_value(p.clone()).ok())
                    .collect()
            })
            .unwrap_or_default();
        let next_step = v.get("next_step").and_then(|n| n.as_i64()).unwrap_or(0);
        CoordinatorBrainDecision {
            action,
            content,
            plan,
            next_step,
        }
    } else {
        CoordinatorBrainDecision {
            action: "chat".into(),
            content: "抱歉，我这边理解有点困难，能再说一次吗？".into(),
            plan: vec![],
            next_step: 0,
        }
    }
}

/// 极简 @mention 提取（无正则依赖，手写扫描）
fn regex_find_mentions(content: &str) -> Vec<String> {
    let mut out = Vec::new();
    let bytes = content.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'@' {
            let start = i + 1;
            let mut j = start;
            while j < bytes.len() {
                let c = bytes[j] as char;
                if c.is_whitespace() || c == '\n' || c == '\r' {
                    break;
                }
                j += 1;
            }
            if j > start {
                let name = &content[start..j];
                // 去掉末尾常见标点
                let name = name.trim_end_matches(|c: char| {
                    matches!(c, ',' | '.' | '，' | '。' | ':' | '：' | '!' | '！' | '？' | '?')
                });
                if !name.is_empty() {
                    out.push(name.to_string());
                }
            }
            i = j;
        } else {
            i += 1;
        }
    }
    out
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}
