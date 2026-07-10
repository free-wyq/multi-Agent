//! core —— 全新后端架构（greenfield 重写，借鉴 AgentScope 2.0 抽象）
//!
//! 四个 AgentScope 启发的抽象：
//! - 事件系统（event.rs）：类型化 DomainEvent 内部流转，边界投影为 BusEventData
//! - 工作区/沙箱（workspace.rs）：Workspace trait + LocalWorkspace，留 Docker/E2B 接缝
//! - 权限系统（permission.rs）：allowed/denied tools → CLI flags
//! - 中间件系统（middleware.rs）：inbound/outbound 管道（logging/mention routing）
//!
//! 构建顺序：types → persistence → store → inbox → llm → prompts
//!           → event → workspace → permission → middleware → engine → commands

pub mod event;
pub mod inbox;
pub mod llm;
pub mod middleware;
pub mod permission;
pub mod persistence;
pub mod prompts;
pub mod store;
pub mod types;
pub mod workspace;

pub mod engine;
pub mod commands;
