//! Runtime —— spawn Claude Code CLI 子进程
//! 对应 TS `main/runtime/claude-code-runtime.ts` + `process-manager.ts`

use crate::store::persistence;
use crate::store::types::AgentDefinition;
use std::path::PathBuf;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::Command;

pub struct AgentResult {
    pub success: bool,
    pub exit_code: Option<i32>,
    pub output: String,
}

/// 检测 Claude Code CLI 路径：环境变量 > PATH > 平台默认
pub fn find_claude_code() -> anyhow::Result<String> {
    // 1. 环境变量
    if let Ok(p) = std::env::var("CLAUDE_CODE_PATH") {
        if std::path::Path::new(&p).exists() {
            return Ok(p);
        }
    }
    // 2. PATH 中查找
    let cmd = if cfg!(target_os = "windows") {
        "where claude"
    } else {
        "which claude"
    };
    if let Ok(out) = std::process::Command::new("sh").arg("-c").arg(cmd).output() {
        if out.status.success() {
            let found = String::from_utf8_lossy(&out.stdout)
                .lines()
                .next()
                .unwrap_or("")
                .trim()
                .to_string();
            if !found.is_empty() && std::path::Path::new(&found).exists() {
                return Ok(found);
            }
        }
    }
    // 3. 平台默认路径
    let home = dirs::home_dir().unwrap_or_default();
    let candidates: Vec<PathBuf> = if cfg!(target_os = "macos") {
        vec![
            PathBuf::from("/usr/local/bin/claude"),
            home.join(".claude").join("bin").join("claude"),
        ]
    } else if cfg!(target_os = "linux") {
        vec![
            PathBuf::from("/usr/local/bin/claude"),
            PathBuf::from("/usr/bin/claude"),
            home.join(".local").join("bin").join("claude"),
        ]
    } else {
        vec![]
    };
    for c in candidates {
        if c.exists() {
            return Ok(c.to_string_lossy().to_string());
        }
    }
    anyhow::bail!(
        "Claude Code CLI not found. Please install it or set CLAUDE_CODE_PATH environment variable."
    )
}

/// 生成 CLAUDE.md 内容
fn generate_claude_md(agent: &AgentDefinition) -> String {
    let sp: &str = agent.system_prompt.as_str();
    if !sp.trim().is_empty() {
        return format!(
            "# {name} 的角色定义\n\n## 角色\n\n{sp}\n\n## 技能\n\n{skills}\n\n## 约束\n\n- 只能在工作目录下操作文件\n- 不要访问外部生产系统\n- 不要泄露敏感凭据\n\n## 环境与交接\n\n- 工作目录为当前群组共享目录\n- 产出物 -> shared/\n- 最终交付 -> output/\n- 完成后通知群主\n",
            name = agent.name,
            sp = sp,
            skills = build_skills_section(agent),
        );
    }
    format!(
        "# {name} 的角色定义\n\n## 角色\n\n{role} — {name}\n\n## 技能\n\n{skills}\n\n## 约束\n\n- 只能在工作目录下操作文件\n- 不要访问外部生产系统\n",
        name = agent.name,
        role = agent.role,
        skills = build_skills_section(agent),
    )
}

fn build_skills_section(agent: &AgentDefinition) -> String {
    let mut lines = Vec::new();
    for s in &agent.extra_skills {
        lines.push(format!("- {s}（技能市场挂载）"));
    }
    for s in &agent.skills {
        lines.push(format!("- {s}（内置）"));
    }
    if lines.is_empty() {
        "- 通用开发技能".into()
    } else {
        lines.join("\n")
    }
}

pub struct ClaudeCodeRuntime {
    pub group_id: String,
    pub agent: AgentDefinition,
    pub work_dir: PathBuf,
}

impl ClaudeCodeRuntime {
    pub fn new(group_id: &str, agent: AgentDefinition) -> Self {
        let work_dir = persistence::group_work_dir(group_id);
        Self {
            group_id: group_id.to_string(),
            agent,
            work_dir,
        }
    }

    /// 启动准备：创建工作目录 + CLAUDE.md
    pub async fn start(&self) -> anyhow::Result<()> {
        std::fs::create_dir_all(&self.work_dir)?;
        std::fs::write(self.work_dir.join("CLAUDE.md"), generate_claude_md(&self.agent))?;
        for dir in ["shared", "output", ".agenticx/tasks", ".agenticx/results"] {
            std::fs::create_dir_all(self.work_dir.join(dir))?;
        }
        Ok(())
    }

    /// 执行任务：spawn Claude Code CLI，实时逐行回调日志
    pub async fn execute<F>(&self, task_content: &str, task_id: &str, mut on_log: F) -> AgentResult
    where
        F: FnMut(&str),
    {
        if let Err(e) = self.start().await {
            return AgentResult {
                success: false,
                exit_code: None,
                output: format!("start failed: {e}"),
            };
        }

        let claude_path = match find_claude_code() {
            Ok(p) => p,
            Err(e) => {
                return AgentResult {
                    success: false,
                    exit_code: None,
                    output: e.to_string(),
                };
            }
        };

        // 写入任务文件
        let task_file = self
            .work_dir
            .join(".agenticx")
            .join("tasks")
            .join(format!("{task_id}.json"));
        let _ = std::fs::write(
            &task_file,
            serde_json::to_string_pretty(&serde_json::json!({
                "task_id": task_id,
                "content": task_content,
                "agent": self.agent.name,
                "role": self.agent.role,
            }))
            .unwrap_or_default(),
        );

        let mut cmd = Command::new(&claude_path);
        cmd.arg("--print").arg(task_content).current_dir(&self.work_dir);
        cmd.env("CLAUDE_MD", "CLAUDE.md");
        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(target_os = "windows")]
        {
            // Windows 上通过 shell 执行
        }

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                return AgentResult {
                    success: false,
                    exit_code: None,
                    output: format!("Process error: {e}"),
                };
            }
        };

        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        let mut output = String::new();

        if let Some(stdout) = stdout {
            let mut reader = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                if !line.trim().is_empty() {
                    on_log(&line);
                }
                output.push_str(&line);
                output.push('\n');
            }
        }
        if let Some(stderr) = stderr {
            let mut reader = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = reader.next_line().await {
                if !line.trim().is_empty() {
                    on_log(&format!("[stderr] {line}"));
                }
                output.push_str(&line);
                output.push('\n');
            }
        }

        let status = child.wait().await.ok();
        let exit_code = status.and_then(|s| s.code());
        AgentResult {
            success: exit_code == Some(0),
            exit_code,
            output: output.trim().to_string(),
        }
    }
}
