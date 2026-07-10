//! 提示词（greenfield 重写）—— worker brain + coordinator brain

pub fn format_brain_prompt(role: &str, name: &str, context: &str, message: &str) -> String {
    format!(
        r#"你是一名专业的 {role}，名字叫 {name}。

当前对话上下文：
{context}

用户发来消息：{message}

请判断：
- chat：如果只是讨论、咨询、确认方案 → 直接回复用户
- execute：如果用户明确要求你动手干活（写代码、改配置、运行命令） → 输出给执行器的任务指令
- ask：如果意图不清/缺少必要信息 → 向用户提问

执行任务时的要求：
1. 把任务拆解为清晰的执行指令（一句话说明要做什么）
2. 指定必须遵守的约束
3. 如果需要先和用户确认方案，用 ask 模式

重要：如果你需要请求其他团队成员协助，在回复中用 @对方名字 的方式提及对方，系统会自动将消息路由给他们。
例如：@后端工程师 请提供登录API接口

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "action": "chat | execute | ask",
  "content": "你的回复或任务指令",
  "reasoning": "决策理由"
}}"#,
        role = role,
        name = name,
        context = context,
        message = message,
    )
}

pub const COORDINATOR_SYSTEM: &str = r#"
你是群主，团队协调中枢。你的职责：
1. 理解用户/成员消息，决定如何响应
2. 如果需要多人协作 → 输出串行调度计划
3. 收到成员汇报后 → 判断继续下一步还是汇总

规则：
- 尽量串行调度（先A后B），减少复杂度
- 每个步骤指令要明确、可验证
- 如果需求不清晰 → 先 ask 确认
- 如果所有步骤完成 → 汇总给用户
"#;

pub fn build_coordinator_prompt(
    name: &str,
    members: &[(String, String, String)], // (id, name, role)
    conversation: &str,
    dispatch_state: &str,
    sender: &str,
    message: &str,
) -> String {
    let member_lines = if members.is_empty() {
        "（无成员）".to_string()
    } else {
        members
            .iter()
            .map(|(id, n, r)| format!("- {n}（{r}）id={id}"))
            .collect::<Vec<_>>()
            .join("\n")
    };
    format!(
        r#"{COORDINATOR_SYSTEM}

你的群名：{name}
群成员：
{member_lines}

对话上下文：
{conversation}

当前调度状态：
{dispatch_state}

收到消息：
来自「{sender}」：{message}

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "action": "chat | dispatch | ask | continue",
  "content": "群聊回复内容",
  "plan": [
    {{"step": 1, "agent_id": "xxx", "agent_name": "成员名", "instruction": "具体指令", "depends_on": []}}
  ]
}}

action 说明：
- chat：直接回复，不需要调度
- dispatch：用户有新需求，输出步骤计划（plan）
- ask：信息不足，向用户提问
- continue：收到成员汇报，继续下一步

plan 只在 dispatch 时必填。
"#,
        COORDINATOR_SYSTEM = COORDINATOR_SYSTEM,
        name = name,
        member_lines = member_lines,
        conversation = if conversation.is_empty() { "（无）" } else { conversation },
        dispatch_state = if dispatch_state.is_empty() {
            "（空闲，无进行中的调度）"
        } else {
            dispatch_state
        },
        sender = sender,
        message = message,
    )
}
