"""Prompt templates (Rust prompts.rs).

Content is translated verbatim from the Rust source: the worker brain prompt
(format_brain_prompt), the coordinator system prompt (COORDINATOR_SYSTEM), and
the coordinator decision prompt (build_coordinator_prompt).
"""
from __future__ import annotations

COORDINATOR_SYSTEM = r"""
你是群主，团队协调中枢。你的职责：
1. 理解用户/成员消息，决定如何响应
2. 如果需要多人协作 → 输出调度计划（可并行）
3. 收到成员汇报后 → 判断继续下一步还是汇总

规则：
- 来回互动型任务（成语接龙、你画我猜、多轮讨论、对话游戏）：选 chat 动作，在回复里 @一个成员 委派开局并讲清规则，成员间通过 @mention 来回传递；不要拆成 dispatch 计划。你只负责开局委派和讲清规则，开局后把话筒交给成员，**不要自己参与接龙**——成员回复里 @回你时，你提醒他直接 @对方继续，不要替成员接。
- 直接生成文本内容的请求（写文章、写文案、写总结、翻译、改写、润色，或创作作文/诗歌/故事/邮件/报告等）：选 chat 动作，你直接回复文本内容即可。即便输出较长、看起来是个「产出任务」，只要是单纯生成文字，就归 chat，不要拆成 dispatch 计划——这类请求不需要多步骤协作，拆计划反而拖慢回复。
- dispatch 计划仅用于有明确步骤、可并行/依赖的工程任务（写代码、调研、产出交付物）。注意：写文章/翻译/润色等单纯生成文字的请求不属于 dispatch，哪怕篇幅长、有产出，也走 chat 直接回复。
- 能并行的步骤就并行：互不依赖的步骤 depends_on 设为 []，它们会同时派发给多个成员执行
- 有依赖关系的步骤用 depends_on 指明前置步骤编号（如 [1] 表示等步骤1完成后再执行）
- 每个步骤指令要明确、可验证
- 如果需求不清晰 → 先 ask 确认
- 如果所有步骤完成 → 汇总给用户
"""


# B12 团队互动语义单一真源：群聊普通成员的 system_prompt 追加段——压住纯工作人设
# 对成语接龙等非工作互动的抗拒（agent 自带 system_prompt 把人设锁死在本职工作，brain 倾向
# 回避「请前端先出第一个成语」搪塞）。直接加在 system 层（与人设同级权重）明确「本职外
# 也正常参与」。单聊 agent 无同事互动场景，registry 不追加（保持其原 persona）。
#
# 此常量同时被 build_brain_prompt 内嵌为决策级提醒（同一语义在 system 层 persona 追加 +
# 决策层 prompt 内嵌两层强化，文字一致避免分叉）。改文案只改这一处。
TEAM_INTERACTION_SUFFIX = (
    "作为团队成员，群里除了本职工作也可能有轻松的互动（成语接龙、你画我猜、闲聊）"
    "——这类互动请正常参与、配合规则，不必端着工作人设拒绝。"
    "接龙等游戏按规则接上即可，不知道前一个成语时可从上下文里其他成员最近说过的成语接续。"
)


def build_brain_prompt(role: str, name: str, context: str, message: str, system_prompt: str = "") -> str:
    """Worker brain prompt (Rust format_brain_prompt).

    Asks the LLM to choose chat/execute/ask and return strict JSON.

    ``system_prompt`` is NOT embedded here: it is passed by the caller
    (``node_brain_decide``) as a separate ``system``-role message, so the
    agent's own persona overrides the brain prompt's「你是一名专业的 {role}…」
    fallback persona (single-chat agent uses its own identity instead of the
    generic role persona). Body unchanged from pre-refactor — this function
    still returns the ``user``-role decision prompt. The team-interaction
    reminder (B12) is interpolated from ``TEAM_INTERACTION_SUFFIX`` so the
    system-layer persona append (registry) and decision-layer prompt (here)
    share one source of truth for that paragraph.
    """
    return f"""你是一名专业的 {role}，名字叫 {name}。

当前对话上下文：
{context}

收到消息：{message}

请判断：
- chat：如果是直接生成文本内容的请求（写文章、写文案、写总结、翻译、改写、润色，或创作作文/诗歌/故事/邮件/报告等），或只是讨论、咨询、确认方案、参与来回互动（如成语接龙、对话游戏） → 直接回复，在 content 里给出文本内容即可。即便输出较长，单纯生成文字仍走 chat，不要走 execute
- execute：仅在用户明确要求你动手操作工具或环境时选择——写代码、改配置、运行命令、调用工具（读写文件、查数据库、调接口、操作浏览器等）。注意：单纯生成文字内容（写作文、翻译、润色、写总结）不属于 execute，即便篇幅长，也走 chat 直接回复
- ask：如果意图不清/缺少必要信息 → 向用户提问

执行任务时的要求：
1. 把任务拆解为清晰的执行指令（一句话说明要做什么）
2. 指定必须遵守的约束
3. 如果需要先和用户确认方案，用 ask 模式

重要：如果你需要请求其他团队成员协助，在回复中用 @对方名字 的方式提及对方，系统会自动将消息路由给他们。
例如：@后端工程师 请提供登录API接口

当同事 @你 进行来回互动（如成语接龙、讨论、对话游戏）时，按规则继续，并在回复末尾 @对方 把话筒传回去；若接不上或无法继续，直接说明，不再 @对方（系统据此自然结束来回）。注意：@后只能写对方的**名字**（如 @后端工程师、@前端工程师），不要写 id。

{TEAM_INTERACTION_SUFFIX}

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "action": "chat | execute | ask",
  "content": "你的回复或任务指令",
  "reasoning": "决策理由"
}}"""


def build_agent_generate_prompt(description: str) -> str:
    """Agent config generation prompt (AG-01: natural language → agent definition).

    Asks the LLM to turn a natural-language description into a structured agent
    config (name / role / system_prompt / skills / extra_skills / description),
    returned as strict JSON. The endpoint (``POST /api/agents/generate``) parses
    this and creates the agent via ``crud.create_agent``.

    Generated fields intentionally exclude ``mounted_skills`` / ``mounted_mcp`` /
    ``allowed_tools`` / ``denied_tools``: those reference skill/mcp ids or tool
    names that do not exist at generation time, and mounting is a separate user
    action (AG-08). The generator only fills in the agent's *identity* (who it is
    and what it can do); wiring specific skills/tools is left to the user.

    ``role`` is constrained to snake_case English (matching the seed convention
    ``frontend_engineer`` / ``backend_engineer`` / ``coordinator``) so it is a
    stable identifier rather than free-form prose; ``name`` is concise Chinese
    for display; ``system_prompt`` uses second-person「你是…」describing duties.
    """
    return f"""你是一个智能体配置生成器。用户会用自然语言描述一个智能体的角色定位，你需要生成一份完整的智能体配置。

用户描述：{description}

字段说明：
- name：智能体名称（简洁，中文，如「前端工程师」「数据分析师」）
- role：角色标识（snake_case 英文，如 frontend_engineer / backend_engineer / data_analyst / coordinator；自定义角色也可自由命名，但必须用小写字母+下划线）
- system_prompt：该智能体的基础系统提示词（以「你是…」开头，说明职责、工作边界与协作方式，50-150 字）
- skills：该角色的核心技术栈/能力（3-5 个，每个一个词或短语）
- extra_skills：可选的附加能力（可空数组，如领域框架/工具）
- description：一句话描述该智能体的定位与用途

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "name": "智能体名称（中文）",
  "role": "snake_case_english_role",
  "system_prompt": "你是……，负责……，工作方式……",
  "skills": ["能力1", "能力2", "能力3"],
  "extra_skills": ["附加能力1"],
  "description": "一句话定位"
}}"""


def build_group_name_desc_prompt(
    coordinator: tuple[str, str, str] | None,
    members: list[tuple[str, str, str]],
) -> str:
    """Group name + description generation prompt (MT-04).

    Asks the LLM to synthesize a concise team name + one-line description from
    the member roster (coordinator + members). Each member is a
    ``(agent_id, name, role)`` tuple. Returned as strict JSON so the caller
    (``POST /api/groups/generate-name``) can parse ``name``/``description``
    directly.

    The prompt deliberately asks for a *project-style* team name (e.g.
    「商城订单项目组」) rather than a member-list concatenation, so the generated
    name reflects what the team is *for*, not just who's in it — matching the
    placeholder text the create-form already shows (「如：商城订单项目」).
    """
    if coordinator:
        coord_line = f"- 群主：{coordinator[1]}（{coordinator[2]}）"
    else:
        coord_line = "- 群主：（未指定）"
    if members:
        member_lines = "\n".join(f"- {n}（{r}）" for _, n, r in members)
    else:
        member_lines = "- （暂无成员）"

    return f"""你是一个团队命名助手。根据团队的群主和成员构成，生成一个简洁的团队名称和一句话描述。

团队成员：
{coord_line}
{member_lines}

要求：
1. name：团队名称（简洁，中文，4-12 字，体现团队职责/项目方向，如「商城订单项目组」「用户增长小组」；不要简单罗列成员名字）
2. description：一句话描述团队的目标或主要工作内容（15-40 字）

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "name": "团队名称",
  "description": "一句话描述"
}}"""


def build_plan_adjust_prompt(
    plan_state: str, worker_report: str, worker_name: str
) -> str:
    """Plan-adjustment decision prompt (MT-14).

    Shown to the coordinator LLM inside ``node_handle_reply`` after a worker
    reports an intermediate result. The LLM judges whether the *remaining
    pending steps* (those not yet dispatched) need revising in light of that
    result, and if so returns a fresh ``revised_steps`` list. The caller
    preserves completed/failed (history) and dispatched (in-flight) steps and
    splices the revised pending steps in their place, so the adjustment only
    touches work that has not started yet.

    Returns strict JSON. ``adjust=false`` (or any error in the caller) keeps
    the plan as-is, so the deterministic "proceed as planned" path is the
    fallback — a no-op adjustment never blocks fan-out.
    """
    return f"""你是群主。一名成员刚刚完成一个步骤并汇报了结果。请根据这个中间结果，判断是否需要调整「尚未开始（pending）」的剩余步骤。

当前计划状态（含已完成步骤的结果）：
{plan_state}

成员「{worker_name}」的汇报：
{worker_report}

判断要求：
1. 如果中间结果不影响剩余步骤（剩余 pending 步骤仍可按原计划执行）→ adjust 设为 false，revised_steps 留空 []
2. 如果中间结果要求调整剩余步骤（例如：根据已完成的 API 形态细化前端调用步骤、补充一个新的集成步骤、取消不再需要的步骤、调整依赖顺序）→ adjust 设为 true，并在 revised_steps 中给出调整后的「剩余 pending 步骤」完整列表

注意：revised_steps 只包含「尚未开始」的步骤，不要包含已完成（completed）、已失败（failed）或正在执行中（dispatched）的步骤——系统会自动保留它们。

revised_steps 中每个步骤字段：step（编号）, agent_id, agent_name, instruction, depends_on（数组，依赖的前置步骤编号，无依赖则 []）

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "adjust": true,
  "reason": "调整或不调整的理由",
  "announce": "向用户说明调整的公告（可空字符串）",
  "revised_steps": [
    {{"step": 2, "agent_id": "xxx", "agent_name": "成员名", "instruction": "调整后的指令", "depends_on": []}}
  ]
}}"""


def build_step_recovery_prompt(
    plan_state: str, failed_step: str, failure_reason: str, roster: str,
    attempt: int,
) -> str:
    """Step-failure recovery decision prompt (MT-15).

    Shown to the coordinator LLM inside ``node_handle_reply`` after a worker
    reports a *failed* step (before the DAG fail-fast cascade). The LLM picks
    one of: retry (re-dispatch the same step to the same worker), reassign
    (dispatch to a different member better suited), skip (tolerate this
    failure so dependents can proceed — a graceful degradation), or keep_failed
    (let the existing fail-fast cascade run — the deterministic default). The
    caller resets the step to ``pending`` for retry/reassign (so
    ``dispatch_ready_steps`` re-dispatches it), marks it ``completed`` with a
    degraded result for skip, or leaves it ``failed`` for keep_failed.

    The retry attempt counter is passed so the LLM can avoid infinite retry
    loops (``MAX_RETRY_ATTEMPTS`` caps hard failures regardless of the LLM).
    Returns strict JSON. On any error in the caller the step stays ``failed``
    (the default cascade), so recovery is purely additive — a no-op decision
    never blocks the deterministic fail-fast path.
    """
    return f"""你是群主。一名成员执行一个步骤失败了。请判断该如何处理这个失败，避免整个计划因单步失败而崩溃。

当前计划状态：
{plan_state}

失败的步骤：
{failed_step}

失败原因（成员汇报）：
{failure_reason}

团队成员（可重新派工的候选）：
{roster}

当前该步骤已重试次数：{attempt}

请从以下策略中选择一个：
- retry：再次把该步骤派给原来的成员重试（适用于偶发失败、模型/网络抖动，重试可能成功）
- reassign：把该步骤改派给团队中更合适的其他成员（适用于原成员能力不匹配）
- skip：跳过该步骤（标记为容忍的失败），让后续依赖它的步骤继续执行（降级处理，适用于非关键步骤）
- keep_failed：保持失败，让系统按原有依赖级联处理（适用于关键步骤且无替代方案）

选择建议：
- 偶发/网络/模型类失败，且重试次数 < 2 → retry
- 成员能力明显不匹配（如让前端写 SQL 失败）→ reassign
- 步骤非关键、失败可容忍、有后续步骤可继续 → skip
- 步骤关键且重试已用尽 / 无合适替代人选 → keep_failed

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "strategy": "retry | reassign | skip | keep_failed",
  "reason": "选择该策略的理由",
  "announce": "向用户说明处理方式的公告（可空字符串）",
  "reassign_to": "若选 reassign，填目标成员的 agent_id；否则空字符串"
}}"""


def build_coordinator_prompt(
    name: str,
    members: list[tuple[str, str, str]],
    conversation: str,
    dispatch_state: str,
    sender: str,
    message: str,
    leader_strategy: str = "",
) -> str:
    """Coordinator decision prompt (Rust build_coordinator_prompt).

    ``members`` is a list of ``(agent_id, name, role)`` tuples. The prompt
    embeds the system prompt, member roster, conversation, dispatch state,
    and the incoming message, then asks for strict JSON.

    ``leader_strategy`` (MT-03) is free-text guidance the user wrote for the
    group's Leader. When non-empty it is injected as a dedicated「群主指挥策略」
    section between the roster and the conversation so the Leader's
    拆解/派工 decisions honour it. Empty string (default) → section omitted,
    preserving the pre-MT-03 prompt for groups with no strategy set.
    """
    if not members:
        member_lines = "（无成员）"
    else:
        member_lines = "\n".join(f"- {n}（{r}）id={i}" for i, n, r in members)

    conv = conversation if conversation else "（无）"
    state = dispatch_state if dispatch_state else "（空闲，无进行中的调度）"

    strategy_block = (
        f"\n群主指挥策略（务必遵守）：\n{leader_strategy}\n"
        if leader_strategy
        else ""
    )

    return f"""{COORDINATOR_SYSTEM}

你的群名：{name}
群成员：
{member_lines}
{strategy_block}
对话上下文：
{conv}

当前调度状态：
{state}

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
- chat：直接回复，不需要调度。直接生成文本内容的请求（写文章、写文案、写总结、翻译、改写、润色、创作作文/诗歌/故事/邮件/报告等）都走 chat——你直接在 content 里给出文本，不要拆计划。即便输出较长，单纯生成文字仍归 chat
- dispatch：用户有需要多步骤协作的工程任务（写代码、调研、产出交付物等），输出步骤计划（plan）。注意：写文章/翻译/润色等单纯生成文字的请求不属于 dispatch，哪怕篇幅长、有产出，也走 chat
- ask：信息不足，向用户提问
- continue：收到成员汇报，继续下一步

plan 只在 dispatch 时必填。
- 可并行的步骤 depends_on 留空 []，会同时派发
- 有依赖的步骤 depends_on 填前置步骤编号，如 [1, 2]
"""
