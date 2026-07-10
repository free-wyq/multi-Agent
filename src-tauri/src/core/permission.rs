//! 权限系统（AgentScope 启发）—— greenfield 重写
//!
//! 把 AgentDefinition 的 allowed_tools / denied_tools / model / max_turns 真正落到 CLI：
//! 旧代码持久化了这些字段却从不传递，导致配置形同虚设。

use crate::core::types::AgentDefinition;
use tokio::process::Command;

/// 将 agent 的权限/模型配置应用到 claude CLI 命令
pub fn apply_to_command(agent: &AgentDefinition, cmd: &mut Command) {
    if !agent.model.is_empty() {
        cmd.arg("--model").arg(&agent.model);
    }
    if !agent.allowed_tools.is_empty() {
        cmd.arg("--allowedTools")
            .arg(agent.allowed_tools.join(","));
    }
    if !agent.denied_tools.is_empty() {
        cmd.arg("--disallowedTools")
            .arg(agent.denied_tools.join(","));
    }
    if agent.max_turns > 0 {
        cmd.arg("--max-turns").arg(agent.max_turns.to_string());
    }
}
