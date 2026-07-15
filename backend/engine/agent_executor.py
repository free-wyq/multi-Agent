"""Agent task executor — bridges the engine to the agentic loop.

This replaces the old ``cli_executor.py`` (which was a mock). The executor
reads the agent definition (system_prompt, max_turns, model) and delegates to
``run_agent_loop``, which runs an LLM + bind_tools agentic loop using
framework-internal tools (no external Claude CLI).

Mounted skills (PRD PL-06) are resolved here: the agent's ``mounted_skills``
ids are looked up to their content and appended to the system prompt, so the
worker "knows" its skills and can follow them autonomously.

Return shape is ``{"success": bool, "exit_code": int, "output": str}`` —
identical to the old mock so ``AgentEngine._run_worker_task`` is unchanged
apart from the import rename.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from engine.agent_loop import DEFAULT_MAX_TURNS, run_agent_loop, set_extra_tools
from store import crud

logger = logging.getLogger("multi-agent.agent_executor")

_SKILL_HEADER = "\n\n## 已挂载技能\n你拥有以下技能，请根据任务需要自主使用：\n"

# 渐进式披露开关（阶段二）：True = manifest 常驻 + 全文按需 load；False = 旧全文直接拼。
# 默认 **关**：渐进式要真正生效需「worker brain 按需 load 全文」的机制——即阶段四的
# load_skill 受控工具（task36 工具池 + task40 安全审计）。在 load_skill 落地前开渐进式
# 会让 worker 只看到技能清单而拿不到全文 content，test_pl06「挂载技能 content 到达 worker
# 输出」契约会断（哨兵标记在 content 里，manifest 里没有）。故阶段二只铺函数地基 +
# 契约测试锁其行为，开关保持关，阶段四 load_skill 就绪后翻 True。旧全文路径
# （_compose_system_prompt）始终是兜底真源。
_SKILL_PROGRESSIVE = False

# manifest 常驻头：只放技能清单（name+description+triggers），成本低，告诉 worker 有哪些技能
_SKILL_MANIFEST_HEADER = (
    "\n\n## 可用技能清单\n"
    "你已挂载以下技能。若某项与当前任务相关，可用 load_skill 工具加载其完整内容后再依其指引执行：\n"
)


def _compose_system_prompt(base: str, skill_contents: list[str]) -> str:
    """Append mounted-skill **full content** to the base system prompt (PL-06 stage 1).

    Legacy/兜底 full-injection path: every mounted skill's whole ``content`` is
    appended. Kept as the fallback when progressive disclosure is off
    (``_SKILL_PROGRESSIVE = False``) and as the honest default for
    ``agent_executor.execute_agent_task`` — the resident execute path where a
    task is already scoped and pulling full skill text up front is acceptable.
    """
    base = (base or "").strip()
    if not skill_contents:
        return base
    blocks = []
    for i, content in enumerate(skill_contents, 1):
        blocks.append(f"### 技能 {i}\n{content}")
    return base + _SKILL_HEADER + "\n\n".join(blocks)


def _compose_skill_manifest(base: str, manifest: list[dict]) -> str:
    """Append a lightweight skill **manifest** (name+description+triggers) to the
    base system prompt (PL-06 stage 2 progressive disclosure).

    Only metadata is常驻 — no big ``content`` blobs — so token cost stays low
    even with many skills mounted. The worker brain reads this to decide which
    skill to load fully on demand (via ``load_skill`` tool / ``_load_skill_full``).
    """
    base = (base or "").strip()
    if not manifest:
        return base
    lines = []
    for i, m in enumerate(manifest, 1):
        name = m.get("name", "")
        desc = m.get("description", "")
        triggers = m.get("triggers") or []
        # 编号 + name + 一行 description + 触发词（人读辅助 + 自动激活线索）
        trig = f"（触发：{', '.join(triggers)}）" if triggers else ""
        lines.append(f"{i}. **{name}**{trig} — {desc}")
    return base + _SKILL_MANIFEST_HEADER + "\n".join(lines) + "\n"


def _load_skill_full(manifest_item: dict, content: str) -> str:
    """Format a single skill's full content for on-demand injection (stage 2).

    Given a manifest entry (for the skill's name/identity) and its loaded
    ``content`` string, produce the injected block. Called by the worker brain
    when it decides a specific skill is needed — the manifest (常驻) told it
    the skill exists; this fetches + formats the full body.
    """
    name = (manifest_item or {}).get("name", "技能")
    return f"### 技能：{name}\n{content}"


async def execute_agent_task(
    group_id: str,
    agent: dict[str, Any],
    task_content: str,
    task_id: str,
    on_log: Callable[[str, str, dict | None], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute a worker task via the agentic loop.

    Reads ``system_prompt``, ``max_turns``, and ``model`` from the agent
    definition and calls ``run_agent_loop``. Mounted skills are resolved and
    injected into the system prompt (PL-06). Mounted MCP connections are
    loaded as LangChain tools and injected via ``set_extra_tools`` (PL-07).
    Returns the standard ``{success, exit_code, output}`` dict.
    """
    agent_name = agent.get("name", "agent")
    agent_id = agent.get("id", "")
    system_prompt = agent.get("system_prompt", "") or ""
    agent_model = agent.get("model", "") or ""

    raw_turns = agent.get("max_turns", 0) or 0
    max_turns = raw_turns if raw_turns > 0 else DEFAULT_MAX_TURNS

    # PL-06: resolve mounted skills → inject into the system prompt.
    # 阶段二渐进式披露（_SKILL_PROGRESSIVE）：manifest(name+description+triggers)常驻
    # 拼进 prompt（成本低、技能多不爆），全文 content 按 worker brain 决策再 load。
    # 旧全文直接拼路径（_compose_system_prompt）作兜底：开关关或 manifest 拉取失败时回退。
    mounted_skills = agent.get("mounted_skills") or []
    skill_contents: list[str] = []
    if mounted_skills and _SKILL_PROGRESSIVE:
        manifest = await crud.resolve_skill_manifest(mounted_skills)
        if manifest:
            system_prompt = _compose_skill_manifest(system_prompt, manifest)
            if on_log:
                await on_log(
                    "log",
                    f"[技能] 已挂载 {len(manifest)} 个技能清单（渐进式·全文按需 load）",
                    None,
                )
    elif mounted_skills:
        # 兜底/旧路径：全文直接拼
        skill_contents = await crud.resolve_skill_contents(mounted_skills)
        if skill_contents:
            system_prompt = _compose_system_prompt(system_prompt, skill_contents)
            if on_log:
                await on_log(
                    "log",
                    f"[技能] 已加载 {len(skill_contents)} 个挂载技能到上下文（全文注入）",
                    None,
                )

    # ── 额外工具累积（skill 受控工具 + MCP 工具，合并后一次性 set_extra_tools）──
    # 阶段四·task36：技能 requires_tools → 受控工具池（file_read/file_write/bash_run，
    # 绑各技能自家沙箱 workspace）。无 requires_tools 的技能不 bind（纯文档走 prompt 注入）。
    # PL-07：MCP 连接 → 外部工具。两者合并进 _EXTRA_TOOLS 由 run_agent_loop 拼接 group
    # 内置工具。注意：set_extra_tools 是覆盖语义，故先累积 extra_tools 再一次性 set，
    # 否则后跑的块会冲掉先跑的块（早期实现 MCP 块覆盖 skill 工具的 bug）。
    extra_tools: list = []
    if mounted_skills:
        skill_manifest = await crud.resolve_skill_manifest(mounted_skills)
        if skill_manifest:
            from engine.tools import resolve_skill_tools

            skill_tools, tool_warnings = resolve_skill_tools(skill_manifest)
            if skill_tools:
                extra_tools.extend(skill_tools)
                if on_log:
                    await on_log(
                        "log",
                        f"[技能] 已绑定 {len(skill_tools)} 个受控工具: "
                        + ", ".join(t.name for t in skill_tools),
                        None,
                    )
            for w in tool_warnings:
                logger.warning("[agent_executor] skill tool: %s", w)
                if on_log:
                    await on_log("log", f"[警告] 技能工具: {w}", None)

    # PL-07: load MCP tools from mounted connections, inject into the loop
    mounted_mcp = agent.get("mounted_mcp") or []
    mcp_tools: list = []
    if mounted_mcp:
        from engine.mcp_manager import load_mcp_tools

        try:
            mcp_tools = await load_mcp_tools(mounted_mcp)
        except Exception as exc:
            logger.warning("[agent_executor] MCP tools load failed: %s", exc)
            if on_log:
                await on_log("log", f"[警告] MCP 工具加载失败: {exc}", None)
        if mcp_tools:
            if on_log:
                await on_log(
                    "log",
                    f"[MCP] 已挂载 {len(mcp_tools)} 个外部工具: "
                    + ", ".join(t.name for t in mcp_tools),
                    None,
                )
            extra_tools.extend(mcp_tools)

    # 一次性注入累积的额外工具（skill 受控 + MCP）。空也 set（清空上轮残留）。
    set_extra_tools(extra_tools)

    logger.info(
        "[agent_executor] group=%s agent=%s task_id=%s model=%s turns=%d skills=%d mcp=%d extra_tools=%d",
        group_id, agent_name, task_id, agent_model or "(default)", max_turns,
        len(skill_contents), len(mcp_tools), len(extra_tools),
    )

    try:
        return await run_agent_loop(
            group_id=group_id,
            agent_id=agent_id,
            agent_name=agent_name,
            task_content=task_content,
            task_id=task_id,
            on_log=on_log,
            max_turns=max_turns,
            system_prompt=system_prompt,
            agent_model=agent_model,
        )
    finally:
        # clear extra tools so concurrent runs don't bleed (set per-run)
        set_extra_tools([])
