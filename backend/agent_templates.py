"""Agent Templates — preset role catalog (PRD AG-11).

A curated, static set of ready-to-hire role templates backing
``GET /api/agents/templates`` (next task) and ``POST /api/agents/templates/{id}/hire``
(AG-12). Mirrors ``skill_hub.py``'s design (lowest-risk, always-deterministic):

  - The catalog is a **module-level tuple constant** — cheap to load, easy to
    extend, no network dependency, so the AG-11 self-test is deterministic and
    the "角色模板广场" is usable in air-gapped / unconfigured envs.
  - ``AgentTemplate`` is the DTO returned to the frontend; ``get_template`` backs
    AG-12 hire (resolve template_id → build ``AgentCreatePayload`` → ``crud.create_agent``).

Template fields intentionally **exclude** ``mounted_skills`` / ``mounted_mcp`` /
``allowed_tools`` / ``denied_tools``: those reference skill/mcp ids that wiring is
a separate user action (AG-08/AG-09). A hired template is a bare agent carrying
only its *identity* (who it is, what it can do) — same stance as AG-01 generation.
This keeps templates about "role definition", not "wiring configuration".

``role`` is snake_case English matching the seed convention
(``frontend_engineer`` / ``backend_engineer`` / ``coordinator``) so the hired
agent's ``role`` is a stable downstream identifier (worker brain prompt interpolates
``{role}``), consistent with ``_sanitize_role`` output in agents.py.

Field names are snake_case to match the frontend ``api.ts`` convention (see
``AgentDefinition`` / ``SkillMarketEntry``).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AgentTemplate(BaseModel):
    """A preset role template ready to hire (AG-11 browse / AG-12 hire).

    Mirrors the subset of ``AgentCreatePayload`` fields the hire flow needs
    (name/role/system_prompt/skills/extra_skills/description) plus UI metadata
    (``category`` / ``icon_emoji``) so the "角色模板广场" can group and badge
    templates, and ``template_id`` so AG-12 hire can resolve the full config.
    """

    model_config = ConfigDict(extra="allow")

    # Stable id within the catalog (e.g. "tpl:backend-engineer"). Prefixed so ids
    # are globally unique and unambiguous (mirrors skill_hub "catalog:<slug>").
    template_id: str
    name: str
    role: str
    description: str = ""
    system_prompt: str = ""
    skills: list[str] = []
    extra_skills: list[str] = []
    # UI metadata
    category: str = "其他"
    icon_emoji: str = "🤖"


# ── Curated catalog ────────────────────────────────────────────────────
# Each tuple: (slug, name, role, description, system_prompt, skills, extra_skills,
#              category, icon_emoji). The slug becomes the template_id ("tpl:<slug>").
# Configs are real, usable role definitions so that hiring one (AG-12) yields a
# genuinely useful agent, not a stub (same principle as skill_hub catalog bodies).
_CATALOG: tuple[
    tuple[str, str, str, str, str, tuple[str, ...], tuple[str, ...], str, str],
    ...,
] = (
    (
        "backend-engineer",
        "后端开发工程师",
        "backend_engineer",
        "负责 API、数据层与服务端业务逻辑开发，熟悉 Python 生态与数据库设计。",
        "你是一名经验丰富的后端开发工程师，擅长 Python、数据库设计、API 开发和系统架构。"
        "请严格按照需求完成开发任务，代码规范、注释清晰，关注接口契约与错误处理。",
        ("Python", "FastAPI", "SQL", "数据库设计"),
        ("LangGraph", "RESTful API"),
        "开发",
        "🔧",
    ),
    (
        "frontend-engineer",
        "前端开发工程师",
        "frontend_engineer",
        "负责页面与组件开发，熟悉 React 生态与现代前端工程化。",
        "你是一名专业的前端开发工程师，擅长 React、TypeScript、CSS 和交互设计。"
        "请按照设计稿和需求完成前端开发，注重用户体验、组件复用与代码质量。",
        ("React", "TypeScript", "CSS"),
        ("Ant Design", "ReactFlow"),
        "开发",
        "💻",
    ),
    (
        "fullstack-engineer",
        "全栈工程师",
        "fullstack_engineer",
        "贯通前后端，能独立交付端到端功能，熟悉全链路调试与部署。",
        "你是一名全栈工程师，能同时驾驭前端 React 与后端 Python/FastAPI，"
        "熟悉数据库与部署流程。请端到端交付功能，保证前后端契约一致与可部署性。",
        ("React", "TypeScript", "Python", "FastAPI"),
        ("Docker", "CI/CD"),
        "开发",
        "🧩",
    ),
    (
        "qa-engineer",
        "测试工程师",
        "qa_engineer",
        "负责测试用例设计、边界覆盖与回归守护，保障产品质量。",
        "你是一名细致的测试工程师，擅长编写测试用例、发现边界问题和回归测试。"
        "请全面覆盖功能测试和异常场景，确保产品质量，输出可运行的测试用例。",
        ("测试用例", "边界分析", "回归测试"),
        ("pytest", "Jest"),
        "测试",
        "🐛",
    ),
    (
        "devops-engineer",
        "DevOps 工程师",
        "devops_engineer",
        "负责容器化、CI/CD 与基础设施自动化，保障部署稳定可重复。",
        "你是一名 DevOps 工程师，擅长 Docker、CI/CD、云部署和基础设施自动化。"
        "请确保部署流程稳定、可重复、安全，提供可回滚的发布方案。",
        ("Docker", "CI/CD", "Linux"),
        ("Kubernetes", "Nginx"),
        "运维",
        "🚀",
    ),
    (
        "product-manager",
        "产品经理",
        "product_manager",
        "负责需求分析、用户故事编写与优先级排序，拉齐团队理解。",
        "你是一名产品经理，擅长需求分析、用户故事编写和优先级排序。"
        "请清晰定义需求，确保团队理解一致，输出结构化的需求文档与验收标准。",
        ("需求分析", "用户故事", "优先级"),
        ("PRD", "竞品分析"),
        "产品",
        "📋",
    ),
    (
        "data-analyst",
        "数据分析师",
        "data_analyst",
        "负责数据清洗、SQL 查询与报表生成，从原始数据提炼业务洞察。",
        "你是一名数据分析师，负责从原始数据中提取业务洞察。你擅长使用 Python pandas "
        "进行数据清洗与转换，能编写高效的 SQL 查询并生成报表，结论需可复现。",
        ("数据清洗", "SQL查询", "报表生成"),
        ("Python pandas", "PostgreSQL"),
        "数据",
        "📊",
    ),
    (
        "ui-designer",
        "UI/UX 设计师",
        "ui_designer",
        "负责界面视觉与交互设计，输出设计稿与组件规范。",
        "你是一名 UI/UX 设计师，擅长界面视觉设计、交互流程与设计系统。"
        "请根据需求输出设计稿与组件规范，注重一致性、可用性与可访问性。",
        ("视觉设计", "交互设计", "设计系统"),
        ("Figma", "Ant Design"),
        "设计",
        "🎨",
    ),
    (
        "tech-writer",
        "技术文档工程师",
        "tech_writer",
        "负责技术文档撰写与维护，保持文档与代码同源、可读。",
        "你是一名技术文档工程师，擅长将复杂技术方案转化为清晰文档。"
        "请保持文档与代码同源、结构清晰、示例可运行，覆盖 API、架构与使用指南。",
        ("技术写作", "Markdown", "API 文档"),
        ("OpenAPI", "Mermaid"),
        "文档",
        "📝",
    ),
    (
        "security-engineer",
        "安全工程师",
        "security_engineer",
        "负责安全审查与风险处置，覆盖注入、越权、密钥泄露与依赖漏洞。",
        "你是一名安全工程师，擅长代码安全审查、依赖漏洞评估与风险处置。"
        "请识别注入、越权、密钥泄露等风险并输出可处置清单，修复建议需可执行。",
        ("安全审查", "漏洞评估", "风险评估"),
        ("OWASP", "依赖扫描"),
        "安全",
        "🛡️",
    ),
)


def _catalog_entries() -> list[AgentTemplate]:
    """Materialize the static catalog into AgentTemplate objects."""
    out: list[AgentTemplate] = []
    for slug, name, role, desc, sp, skills, extra, category, emoji in _CATALOG:
        out.append(
            AgentTemplate(
                template_id=f"tpl:{slug}",
                name=name,
                role=role,
                description=desc,
                system_prompt=sp,
                skills=list(skills),
                extra_skills=list(extra),
                category=category,
                icon_emoji=emoji,
            )
        )
    return out


# In-memory index for O(1) lookup by template_id (AG-12 hire resolves config).
_CATALOG_INDEX: dict[str, AgentTemplate] = {t.template_id: t for t in _catalog_entries()}


def list_templates(category: str = "") -> list[AgentTemplate]:
    """List preset role templates (AG-11).

    Returns all templates by default; when ``category`` is given, filters to that
    category (exact match, case-sensitive — categories are fixed Chinese labels).
    Order follows catalog declaration order (stable for the UI grid).
    """
    if not category:
        return list(_CATALOG_INDEX.values())
    return [t for t in _CATALOG_INDEX.values() if t.category == category]


def get_template(template_id: str) -> AgentTemplate | None:
    """Resolve a template by id (AG-12 hire source).

    Returns the full ``AgentTemplate`` (with system_prompt/skills/etc.) so the
    hire endpoint can build an ``AgentCreatePayload`` directly. A missing id
    returns ``None`` (caller maps to 404).
    """
    return _CATALOG_INDEX.get(template_id)


def list_categories() -> list[dict[str, Any]]:
    """Introspection helper: which categories exist and how many templates each.

    Useful for the UI to render category filter tabs in the 角色模板广场.
    Returns ``[{id, name, count}]`` in catalog declaration order (deduped).
    """
    seen: dict[str, int] = {}
    for t in _CATALOG_INDEX.values():
        seen[t.category] = seen.get(t.category, 0) + 1
    return [{"id": name, "name": name, "count": count} for name, count in seen.items()]
