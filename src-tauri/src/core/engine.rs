//! AgentEngine —— 常驻智能体引擎（greenfield 重写）
//!
//! 保留行为：每 agent 一个 tokio task 消费 mpsc 收件箱；coordinator 走调度大脑，
//! worker 走 brainDecide + LocalWorkspace 执行 Claude Code CLI。
//!
//! 相对旧 engine.rs 的修复（编号对应已知 bug 列表）：
//! #1 删除 EngineStatus::Thinking（死状态）→ status 字符串 idle|executing|offline
//! #2 Executing 期间到达的任务不再丢弃，入 pending backlog，完成后自动续跑
//! #4 删除 CoordinatorBrainDecision.next_step（解析了却从不用）—— 步骤推进纯 depends_on 驱动
//! #5 dispatch_next_step fail-fast：依赖步骤 failed 时，本步也判 failed，不再永久挂起
//! #6/#10 统一 reply 路径（persist + emit + mention routing），删除 EngineHandle 重复 reply
//! #7 mention 扫描统一到 middleware::find_mentions（删除 regex_find_mentions）
//! #8 worker 完成后只 push 单一 agent_reply 通知给 coordinator（complete_task 不再自动通知）
//! #11 shutdown_all 真正接线（lib.rs 退出时调用）

use crate::core::event::{self, DomainEvent};
use crate::core::inbox::{hub, InboxItem, PushNotifyParams, PushTaskParams, TaskCompleteInfo};
use crate::core::llm;
use crate::core::middleware;
use crate::core::prompts;
use crate::core::store;
use crate::core::types::*;
use crate::core::workspace::{LocalWorkspace, Workspace};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Weak};
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
    #[serde(default)]
    pub task_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CoordinatorBrainDecision {
    pub action: String,
    pub content: String,
    #[serde(default)]
    pub plan: Vec<DispatchStep>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum EngineStatus {
    Idle,
    Executing,
    Offline,
}

impl EngineStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            EngineStatus::Idle => "idle",
            EngineStatus::Executing => "executing",
            EngineStatus::Offline => "offline",
        }
    }
}

struct EngineInner {
    status: EngineStatus,
    current_task_id: Option<String>,
    memory: Vec<MemoryEntry>,
    dispatch_plan: Vec<DispatchStep>,
    processing_task_ids: HashSet<String>,
    recent_routes: HashMap<String, f64>,
    /// #2 修复：Executing 期间到达的任务积压于此，完成后续跑
    pending_tasks: Vec<TaskQueueItem>,
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
    #[allow(dead_code)]
    pub system_prompt: String,
    pub group_id: String,
    inner: Arc<Mutex<EngineInner>>,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
    /// 自引用弱指针：spawn 出去的 worker task 据此回到 Arc<Self>
    weak: once_cell::sync::OnceCell<Weak<AgentEngine>>,
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
                processing_task_ids: HashSet::new(),
                recent_routes: HashMap::new(),
                pending_tasks: Vec::new(),
            })),
            shutdown: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            weak: once_cell::sync::OnceCell::new(),
        }
    }

    /// 启动引擎：注册收件箱 channel，spawn tokio task 消费
    pub fn start(self: &Arc<Self>) {
        let _ = self.weak.set(Arc::downgrade(self));
        self.shutdown
            .store(false, std::sync::atomic::Ordering::SeqCst);
        self.inner.lock().status = EngineStatus::Idle;
        let rx = hub().register_inbox(&self.group_id, &self.id);
        let engine = self.clone();
        let _ = tauri::async_runtime::spawn(async move {
            engine.run_loop(rx).await;
        });
        log::info!(
            "[engine] {} (role={}) started in group {}",
            self.name,
            self.role,
            self.group_id
        );
    }

    pub fn stop(&self) {
        self.shutdown
            .store(true, std::sync::atomic::Ordering::SeqCst);
        hub().unregister_inbox(&self.group_id, &self.id);
        self.inner.lock().status = EngineStatus::Offline;
        log::info!("[engine] {} stopped", self.name);
    }

    async fn run_loop(self: Arc<Self>, mut rx: mpsc::UnboundedReceiver<InboxItem>) {
        loop {
            if self.shutdown.load(std::sync::atomic::Ordering::SeqCst) {
                break;
            }
            match rx.recv().await {
                Some(item) => {
                    if let Err(e) = self.handle_inbox(item).await {
                        log::error!("[engine {}] 处理消息失败: {e}", self.name);
                    }
                }
                None => break,
            }
        }
    }

    async fn handle_inbox(&self, item: InboxItem) -> anyhow::Result<()> {
        match item {
            InboxItem::Task(task) => {
                let busy = {
                    let g = self.inner.lock();
                    g.status == EngineStatus::Executing || g.processing_task_ids.contains(&task.id)
                };
                if busy {
                    // #2 修复：不丢弃，入 backlog
                    self.inner.lock().pending_tasks.push(task);
                    return Ok(());
                }
                self.inner.lock().processing_task_ids.insert(task.id.clone());
                let claimed = hub().claim_task(&self.group_id, &self.id, &self.id);
                match claimed {
                    Some(c) if c.id == task.id => {
                        // 需要 Arc 来 spawn worker；通过 weak 取回
                        if let Some(arc) = self.upgrade_self() {
                            arc.claim_and_execute(task).await;
                        }
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

    fn upgrade_self(&self) -> Option<Arc<AgentEngine>> {
        self.weak.get().and_then(|w| w.upgrade())
    }

    // ── 任务执行 ──────────────────────────────────────────────

    async fn claim_and_execute(self: Arc<Self>, task: TaskQueueItem) {
        {
            let mut g = self.inner.lock();
            g.status = EngineStatus::Executing;
            g.current_task_id = Some(task.id.clone());
        }
        let preview: String = task.content.chars().take(50).collect();
        self.publish_log(Some(&task.id), &format!("▶ [{}] 开始执行任务: {preview}...", self.name))
            .await;

        let is_coordinator = self.role == "coordinator";
        if is_coordinator {
            // coordinator 不直接执行 CLI；把 task 当作来自 user 的需求触发调度大脑
            self.publish_log(
                Some(&task.id),
                &format!("▶ [{}] Coordinator 收到需求: {preview}...", self.name),
            )
            .await;
            // #8：complete_task 不再自动通知
            hub().complete_task(&task.id, true, Some("协调者已接收需求，开始调度。".into()), None);
            let notify = NotifyQueueItem {
                id: task.id.clone(),
                group_id: self.group_id.clone(),
                kind: "coordinator_task".into(),
                sender_id: "user".into(),
                receiver_id: self.id.clone(),
                content: task.content.clone(),
                data: task.data.clone(),
                created_at: store::now_iso(),
            };
            self.handle_notify_as_coordinator(notify).await;
            self.reset_idle(&task.id);
            // coordinator 内联完成，若有积压续跑
            if let Some(arc) = self.upgrade_self() {
                arc.drain_pending();
            }
        } else {
            match store::store().get_agent(&self.id) {
                Some(def) => {
                    let engine = self.clone();
                    let task = task.clone();
                    let _ = tauri::async_runtime::spawn(async move {
                        engine.run_worker_task(def, task).await;
                    });
                }
                None => {
                    hub().complete_task(&task.id, false, Some("找不到智能体定义".into()), None);
                    self.publish_log(Some(&task.id), "❌ 找不到智能体定义").await;
                    self.reset_idle(&task.id);
                    if let Some(arc) = self.upgrade_self() {
                        arc.drain_pending();
                    }
                }
            }
        }
    }

    /// worker 执行：跑 LocalWorkspace，发 TaskCompleted 事件，回复 + 汇报 + 续跑
    async fn run_worker_task(self: Arc<Self>, def: AgentDefinition, task: TaskQueueItem) {
        let group_id = self.group_id.clone();
        let agent_id = self.id.clone();
        let task_id = task.id.clone();
        let task_content = task.content.clone();

        let ws = LocalWorkspace::new(&group_id, def);
        let gid = group_id.clone();
        let tid = task_id.clone();
        let aid = agent_id.clone();
        let on_log: Arc<dyn Fn(String) + Send + Sync> = Arc::new(move |line: String| {
            event::emit_log(&gid, Some(&tid), &aid, &line);
        });
        let result = ws.execute(&task_content, &task_id, on_log).await;

        let snippet: String = result.output.chars().take(200).collect();
        // #修复：task_complete/task_failed 现在发到 bus（旧代码只发通知不发 bus）
        event::emit(DomainEvent::TaskCompleted {
            group_id: group_id.clone(),
            task_id: task_id.clone(),
            agent_id: agent_id.clone(),
            success: result.success,
            result: Some(result.output.chars().take(500).collect()),
            exit_code: result.exit_code.map(|c| c as i64),
        });
        // #8：complete_task 不再自动 push task_complete 通知
        hub().complete_task(
            &task_id,
            result.success,
            Some(result.output.chars().take(500).collect()),
            Some(serde_json::json!({ "exit_code": result.exit_code })),
        );

        let reply_content = if result.success {
            if snippet.is_empty() {
                "任务完成 🎉".to_string()
            } else {
                format!("任务完成 🎉\n{snippet}")
            }
        } else {
            format!("执行出错了: {}", result.output)
        };
        self.reply(&reply_content).await;

        // #8：向 coordinator 发单一 agent_reply 通知（携带 task_id/success）
        if let Some(g) = store::store().get_group(&group_id) {
            if !g.coordinator_id.is_empty() && g.coordinator_id != agent_id {
                hub().push_notify(PushNotifyParams {
                    group_id: group_id.clone(),
                    kind: "agent_reply".into(),
                    sender_id: agent_id.clone(),
                    receiver_id: g.coordinator_id,
                    content: format!(
                        "步骤完成：{task_content}\n\n结果：{}",
                        if snippet.is_empty() { "已完成".into() } else { snippet.clone() }
                    ),
                    data: Some(serde_json::json!({ "task_id": task_id, "success": result.success })),
                });
            }
        }

        self.reset_idle(&task_id);
        if let Some(arc) = self.upgrade_self() {
            arc.drain_pending();
        }
    }

    fn reset_idle(&self, task_id: &str) {
        let mut g = self.inner.lock();
        g.status = EngineStatus::Idle;
        g.current_task_id = None;
        g.processing_task_ids.remove(task_id);
    }

    /// #2 修复：Executing 期间积压的任务，当前任务完成后续跑
    fn drain_pending(self: Arc<Self>) {
        let next = {
            let mut g = self.inner.lock();
            if g.status != EngineStatus::Idle {
                return;
            }
            if g.pending_tasks.is_empty() {
                return;
            }
            let t = g.pending_tasks.remove(0);
            g.processing_task_ids.insert(t.id.clone());
            t
        };
        let claimed = hub().claim_task(&self.group_id, &self.id, &self.id);
        match claimed {
            Some(c) if c.id == next.id => {
                let engine = self.clone();
                let _ = tauri::async_runtime::spawn(async move {
                    engine.claim_and_execute(next).await;
                });
            }
            _ => {
                self.inner.lock().processing_task_ids.remove(&next.id);
            }
        }
    }

    // ── 通知处理 ─────────────────────────────────────────────

    async fn handle_notify(&self, notify: NotifyQueueItem) {
        if self.role == "coordinator" {
            self.handle_notify_as_coordinator(notify).await;
            return;
        }

        let content = notify.content.clone();
        let sender = notify.sender_id.clone();

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
                log::warn!("[engine {}] 大脑决策失败: {e}", self.name);
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
                hub().push_task(PushTaskParams {
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
    async fn handle_notify_as_coordinator(&self, notify: NotifyQueueItem) {
        let content = notify.content.clone();
        let sender = notify.sender_id.clone();

        // agent_reply = Worker 汇报 → 走继续/完成任务流程
        if notify.kind == "agent_reply" && sender != "user" {
            let maybe_task_id = notify
                .data
                .as_ref()
                .and_then(|d| d.get("task_id"))
                .and_then(|v| v.as_str());
            let maybe_success = notify
                .data
                .as_ref()
                .and_then(|d| d.get("success"))
                .and_then(|v| v.as_bool())
                .unwrap_or(true);

            let matched_idx = if let Some(task_id) = maybe_task_id {
                let g = self.inner.lock();
                g.dispatch_plan
                    .iter()
                    .position(|s| s.task_id.as_deref() == Some(task_id))
            } else {
                None
            };

            if let Some(idx) = matched_idx {
                let all_done = {
                    let mut g = self.inner.lock();
                    let step = &mut g.dispatch_plan[idx];
                    step.status = if maybe_success {
                        "completed".into()
                    } else {
                        "failed".into()
                    };
                    step.result = Some(content.clone());
                    g.dispatch_plan
                        .iter()
                        .all(|s| s.status == "completed" || s.status == "failed")
                };
                if all_done {
                    self.dispatch_all_done().await;
                } else {
                    self.dispatch_next_step().await;
                }
                return;
            }
        }

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
                        format!("{icon} 步骤{}: {}", s.step, s.agent_name)
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

        let decision = match llm::chat_completion(
            &config,
            vec![
                llm::ChatMessage {
                    role: "system".into(),
                    content: prompts::COORDINATOR_SYSTEM.into(),
                },
                llm::ChatMessage {
                    role: "user".into(),
                    content: prompt,
                },
            ],
        )
        .await
        {
            Ok(raw) => parse_coordinator_decision(&raw),
            Err(e) => {
                log::error!("[coordinator] 决策失败: {e}");
                CoordinatorBrainDecision {
                    action: "chat".into(),
                    content: "抱歉，我这边理解有点困难，能再说一次吗？".into(),
                    plan: vec![],
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
                let mut already_handled = false;
                {
                    let mut g = self.inner.lock();
                    if let Some(cur) = g
                        .dispatch_plan
                        .iter_mut()
                        .find(|s| s.status == "dispatched")
                    {
                        cur.result = Some(content.clone());
                        cur.status = "completed".into();
                        already_handled = true;
                    }
                }
                if already_handled {
                    self.reply(if decision.content.is_empty() {
                        "收到汇报，继续下一步。"
                    } else {
                        &decision.content
                    })
                    .await;
                    self.dispatch_next_step().await;
                } else {
                    self.reply(&decision.content).await;
                }
            }
            _ => {
                self.reply(&decision.content).await;
            }
        }
    }

    /// #5 修复：派发下一个待执行步骤。fail-fast —— 依赖步骤 failed 时本步也判 failed。
    async fn dispatch_next_step(&self) {
        // 先做 fail-fast：任何 pending 步骤若其依赖中有 failed，则本步 failed
        {
            let mut g = self.inner.lock();
            loop {
                // 计算本轮需标记 failed 的步骤号（避免在 iter_mut 中再借用 plan）
                let failed_steps: Vec<i64> = {
                    let plan = &g.dispatch_plan;
                    plan.iter()
                        .filter(|s| s.status == "pending")
                        .filter(|s| {
                            s.depends_on.iter().any(|dep| {
                                plan.iter().any(|d| d.step == *dep && d.status == "failed")
                            })
                        })
                        .map(|s| s.step)
                        .collect()
                };
                if failed_steps.is_empty() {
                    break;
                }
                for step in g.dispatch_plan.iter_mut() {
                    if failed_steps.contains(&step.step) {
                        step.status = "failed".into();
                        step.result = Some("上游步骤失败，跳过".into());
                    }
                }
            }
        }

        let next_idx = {
            let g = self.inner.lock();
            g.dispatch_plan.iter().position(|s| {
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
                let (step_num, agent_id, agent_name, instruction) = {
                    let mut g = self.inner.lock();
                    let step = &mut g.dispatch_plan[idx];
                    step.status = "dispatched".into();
                    let sn = step.step;
                    let a_id = step.agent_id.clone();
                    let a_name = step.agent_name.clone();
                    let inst = step.instruction.clone();
                    (sn, a_id, a_name, inst)
                };

                let dispatch_msg = format!("🚀 步骤 {step_num} 派发：\n@{agent_name} \n\n{instruction}");
                self.reply(&dispatch_msg).await;

                let pushed = hub().push_task(PushTaskParams {
                    group_id: self.group_id.clone(),
                    sender_id: self.id.clone(),
                    receiver_id: agent_id.clone(),
                    content: instruction.clone(),
                    data: Some(serde_json::json!({
                        "step": step_num,
                        "agent_name": agent_name,
                    })),
                });

                {
                    let mut g = self.inner.lock();
                    if let Some(step) = g.dispatch_plan.iter_mut().find(|s| s.step == step_num) {
                        step.task_id = Some(pushed.id.clone());
                    }
                }

                log::info!(
                    "[coordinator] dispatched step {} to {} (task_id={})",
                    step_num,
                    agent_name,
                    pushed.id
                );

                event::emit(DomainEvent::TaskDispatched {
                    group_id: self.group_id.clone(),
                    task_id: pushed.id.clone(),
                    step: step_num,
                    agent_id,
                    agent_name,
                    instruction,
                });
            }
            None => {
                let all_done = {
                    let g = self.inner.lock();
                    g.dispatch_plan
                        .iter()
                        .all(|s| s.status == "completed" || s.status == "failed")
                };
                if all_done {
                    self.dispatch_all_done().await;
                }
            }
        }
    }

    /// 全部步骤完成，汇总输出（走统一 reply，因此也带 mention 路由）
    async fn dispatch_all_done(&self) {
        let summary = {
            let g = self.inner.lock();
            g.dispatch_plan
                .iter()
                .map(|s| {
                    let icon = if s.status == "completed" { "✅" } else { "❌" };
                    let res = s
                        .result
                        .clone()
                        .unwrap_or_else(|| s.instruction.clone())
                        .chars()
                        .take(200)
                        .collect::<String>();
                    format!("{icon} {}: {res}", s.agent_name)
                })
                .collect::<Vec<_>>()
                .join("\n")
        };
        {
            let mut g = self.inner.lock();
            g.dispatch_plan.clear();
        }
        let summary_msg = format!("🎉 全部完成！协作结果汇总：\n{summary}");
        self.reply(&summary_msg).await;
    }

    // ── 统一回复路径（#6/#10 修复）──────────────────────────────

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
        event::emit(DomainEvent::MessageAdded(msg));
        // mention 路由（统一走 middleware，不再散落）
        let mut g = self.inner.lock();
        middleware::route_mentions(&self.group_id, &self.id, &self.name, content, &mut g.recent_routes);
    }

    async fn publish_log(&self, task_id: Option<&str>, line: &str) {
        let msg = store::store().add_message(Message {
            id: store::new_id("msg"),
            group_id: self.group_id.clone(),
            task_id: task_id.map(|s| s.to_string()),
            sender_id: self.id.clone(),
            receiver_id: "broadcast".into(),
            kind: "task_log".into(),
            content: Some(line.to_string()),
            data: None,
            created_at: store::now_iso(),
        });
        event::emit(DomainEvent::MessageAdded(msg));
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

    pub fn status(&self) -> (EngineStatus, Option<String>) {
        let g = self.inner.lock();
        (g.status.clone(), g.current_task_id.clone())
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

    pub fn get_engine(&self, group_id: &str, agent_id: &str) -> Option<Arc<AgentEngine>> {
        let map = self.engines.lock();
        map.get(group_id).and_then(|g| g.get(agent_id).cloned())
    }

    pub fn list_group_engines(&self, group_id: &str) -> Vec<(String, Arc<AgentEngine>)> {
        let map = self.engines.lock();
        map.get(group_id)
            .map(|g| g.iter().map(|(id, e)| (id.clone(), e.clone())).collect())
            .unwrap_or_default()
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

    /// #11 修复：真正接线，供应用退出时调用
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
        CoordinatorBrainDecision { action, content, plan }
    } else {
        CoordinatorBrainDecision {
            action: "chat".into(),
            content: "抱歉，我这边理解有点困难，能再说一次吗？".into(),
            plan: vec![],
        }
    }
}

/// 避免未使用导入告警（TaskCompleteInfo 在签名中保留以备扩展）
#[allow(dead_code)]
fn _use_task_complete_info(_: TaskCompleteInfo) {}
