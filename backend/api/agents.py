"""Agent CRUD routes (M2: SQLite-backed via store.crud).

Routes map 1:1 to the frontend `agentApi` in src/services/api.ts:
  GET    /api/agents                          → list_agents
  GET    /api/agents/templates                 → list_agent_templates  (AG-11)
  POST   /api/agents/templates/{id}/hire       → hire_template         (AG-12)
  GET    /api/agents/{id}                      → get_agent
  POST   /api/agents                           → create_agent   (body = AgentCreatePayload)
  POST   /api/agents/generate                  → generate_agent (AG-01 自然语言生成)
  PUT    /api/agents/{id}                      → update_agent   (body = partial payload)
  DELETE /api/agents/{id}                      → delete_agent
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent_templates import AgentTemplate, get_template, list_templates
from llm import build_agent_generate_prompt, chat_completion, extract_json, get_llm_config
from models import AgentCreatePayload, AgentDefinition
from store import crud

logger = logging.getLogger("multi-agent.agents")

router = APIRouter(prefix="/api/agents", tags=["agents"])


class GenerateAgentBody(BaseModel):
    description: str


async def _generate_agent_via_llm(description: str) -> dict:
    """Call the LLM to turn a natural-language description into an agent config (AG-01).

    Returns a dict with name/role/system_prompt/skills/extra_skills/description.
    Falls back to a bare name+role+description if the LLM call or JSON parse
    fails (mirrors ``_generate_skill_via_llm`` in skills.py — never raises so the
    endpoint always produces a usable agent).

    ``role`` is sanitized to snake_case: non ``[a-z0-9_]`` chars collapse to ``_``
    and leading/trailing underscores are stripped. An empty result (LLM returned
    a non-conforming role) falls back to ``"custom_agent"`` so the DB row always
    has a stable, downstream-consumable identifier.
    """
    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            [{"role": "user", "content": build_agent_generate_prompt(description)}],
        )
        parsed = extract_json(raw)
    except Exception as exc:
        logger.warning("[agents] generate LLM failed: %s", exc)
        parsed = None

    if not parsed:
        return {
            "name": description[:32] or "新智能体",
            "role": "custom_agent",
            "system_prompt": "",
            "skills": [],
            "extra_skills": [],
            "description": description[:200],
        }

    role_raw = str(parsed.get("role") or "custom_agent")
    role = _sanitize_role(role_raw)
    return {
        "name": str(parsed.get("name") or description[:32] or "新智能体")[:64],
        "role": role,
        "system_prompt": str(parsed.get("system_prompt") or ""),
        "skills": [str(s) for s in (parsed.get("skills") or []) if s],
        "extra_skills": [str(s) for s in (parsed.get("extra_skills") or []) if s],
        "description": str(parsed.get("description") or "")[:500],
    }


def _sanitize_role(role: str) -> str:
    """Coerce a role string to snake_case (matches seed convention).

    Lowercases, replaces any run of non ``[a-z0-9]`` with ``_``, and strips
    leading/trailing underscores. Falls back to ``custom_agent`` if nothing is
    left (e.g. LLM returned an all-CJK role). Keeps ``role`` a stable, safe
    identifier for downstream interpolation in the brain prompt.
    """
    s = role.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "custom_agent"


@router.get("")
async def list_agents() -> list[AgentDefinition]:
    return await crud.list_agents()


@router.get("/templates")
async def list_agent_templates(
    category: str = Query("", description="按分类筛选（精确匹配，留空返回全部）"),
) -> list[AgentTemplate]:
    """AG-11: 列出预设角色模板（供前端「角色模板广场」浏览）。

    委托 ``agent_templates.list_templates(category)``：catalog 是模块级静态常量，
    恒可用（无网络/无 DB 依赖），air-gapped 或未配 LLM 的环境也能渲染模板广场。
    ``category`` 留空返回全部；给定则精确匹配分类（分类是固定中文标签如「开发」「测试」）。
    返回顺序遵循 catalog 声明顺序（UI 网格稳定）。

    路由声明在 ``/{agent_id}`` 之前——``templates`` 是字面段，须先于 path 参数匹配，
    否则会被当作 ``agent_id="templates"`` 走 ``get_agent``（与 ``/generate`` 同处理，
    但 ``/generate`` 是 POST 不与 GET ``/{agent_id}`` 冲突，``/templates`` 是 GET 必须
    显式前置）。
    """
    return list_templates(category)


class HireTemplateBody(BaseModel):
    """AG-12: 雇佣预设角色模板的可选覆盖参数。

    全部字段可选——不传任何字段时直接用模板原值落库（最常见路径：用户在广场
    点「雇佣」即原样创建员工）。允许覆盖 ``name`` 让用户在雇佣时改名（如把
    「后端开发工程师」改成「小后端」个性化命名），其余字段取模板原值——模板
    的 role/system_prompt/skills 是角色定义的核心，不应在雇佣时随意改（要改用
    AG-06 编辑）。
    """

    name: str | None = None


@router.post("/templates/{template_id}/hire")
async def hire_template(template_id: str, body: HireTemplateBody) -> AgentDefinition:
    """AG-12: 雇佣一个预设角色模板，创建为本地员工。

    解析 ``template_id``（形如 ``tpl:backend-engineer``）经 ``get_template`` 取
    catalog 全配置 → 构造 ``AgentCreatePayload``（role/system_prompt/skills/
    extra_skills/description 取模板原值；name 用 body 覆盖值或模板名）→
    ``crud.create_agent`` 落库 → 返回 ``AgentDefinition``（与 AG-01 / create 同类型，
    直接进员工列表）。雇佣的 agent 无 mounted_skills/mcp/tools（挂载是独立用户动作
    AG-08/AG-09，与 AG-01 生成、AG-11 模板同立场）。

    未知 ``template_id`` → 404（catalog 无此条目，可能是已下架或客户端伪造 id）。
    ``name`` 覆盖为空串时回退模板名（不让用户雇出无名员工）。

    路由声明在 ``GET /{agent_id}`` 之前——``templates/{template_id}/hire`` 是三段
    字面路径，须先于 ``/{agent_id}`` 匹配，否则 ``templates`` 会被当作 agent_id
    走 ``get_agent``（与 ``/templates`` GET 同理，但本路由是 POST，FastAPI 按方法
    +路径双匹配，POST ``templates/.../hire`` 不与 GET ``/{agent_id}`` 冲突，仍前置
    保持路由表清晰一致）。
    """
    template = get_template(template_id)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"角色模板 {template_id} 不存在（catalog 无此条目）",
        )

    name = (body.name or "").strip() or template.name
    payload = AgentCreatePayload(
        name=name,
        role=template.role,
        system_prompt=template.system_prompt,
        skills=list(template.skills),
        extra_skills=list(template.extra_skills),
        description=template.description,
    )
    agent = await crud.create_agent(payload)
    logger.info(
        "[agents] hired template %s → agent %s (%s / %s)",
        template_id,
        agent.id,
        agent.name,
        agent.role,
    )
    return agent


@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> AgentDefinition | None:
    return await crud.get_agent(agent_id)


@router.post("")
async def create_agent(payload: AgentCreatePayload) -> AgentDefinition:
    return await crud.create_agent(payload)


@router.post("/generate")
async def generate_agent(body: GenerateAgentBody) -> AgentDefinition:
    """AG-01: generate a complete agent config from a natural-language description.

    Calls the LLM with ``build_agent_generate_prompt`` → extracts strict JSON →
    builds an ``AgentCreatePayload`` (with a sanitized snake_case ``role``) →
    persists via ``crud.create_agent``. The generated agent has no mounted
    skills/mcp/tools (those are wired later via AG-08/AG-09 mounting actions).

    Route declared before ``/{agent_id}`` — ``generate`` is a literal segment
    that must precede the path param so FastAPI does not treat it as an agent id
    (same ordering rule as ``/upload`` in skills.py).
    """
    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description 不能为空")

    fields = await _generate_agent_via_llm(description)
    payload = AgentCreatePayload(
        name=fields["name"],
        role=fields["role"],
        system_prompt=fields["system_prompt"],
        skills=fields["skills"],
        extra_skills=fields["extra_skills"],
        description=fields["description"],
    )
    agent = await crud.create_agent(payload)
    logger.info(
        "[agents] generated agent %s (%s / %s) from description",
        agent.id,
        agent.name,
        agent.role,
    )
    return agent


@router.put("/{agent_id}")
async def update_agent(agent_id: str, payload: AgentCreatePayload) -> AgentDefinition | None:
    return await crud.update_agent(agent_id, payload)


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str) -> bool:
    return await crud.delete_agent(agent_id)
