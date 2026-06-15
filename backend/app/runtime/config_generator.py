"""
CLAUDE.md / settings.json 自动生成

根据 AgentDefinition 生成容器内配置：
- CLAUDE.md   → 角色定义、技能、约束
- settings.json → 工具权限
"""


# ── CLAUDE.md 模板 ─────────────────────────────────────────────────

CLAUDE_MD_TEMPLATE = """# {name} 的角色定义

## 角色

{name} — {role_description}

## 职责

{responsibilities}

## 技能

{skills_section}

## 约束

{constraints}

## 环境

- 你在一个 Docker 容器内运行，工作目录为 /workspace
- 所有中间件自行安装（apt-get / npm / pip 等）
- 产出物放到 /workspace/shared/ 目录供其他智能体共享
- 最终交付物放到 /workspace/output/
- 任务完成后主动通知群主

## 交接规范

完成子任务后：
1. 将产出文件复制到 /workspace/shared/
2. 生成简要 summary 文件到 /workspace/shared/.summary-{task_id}.json
3. 退出码 0 表示成功，非 0 表示失败
"""

ROLE_DESCRIPTIONS: dict[str, str] = {
    "frontend-engineer": "前端开发工程师 — 负责页面开发、组件实现、样式编写",
    "backend-engineer": "后端开发工程师 — 负责 API 开发、数据库操作、业务逻辑",
    "tester": "测试工程师 — 负责编写测试用例、执行测试、报告缺陷",
    "reviewer": "代码审查员 — 负责代码审查、质量把关、最佳实践建议",
    "devops": "DevOps 工程师 — 负责部署、CI/CD、环境配置",
}

ROLE_RESPONSIBILITIES: dict[str, str] = {
    "frontend-engineer": """- 根据需求实现前端页面与组件
- 确保 UI 符合设计规范
- 编写前端单元测试
- 与后端 API 对接""",
    "backend-engineer": """- 设计和实现 REST/GraphQL API
- 数据库模型设计与查询优化
- 业务逻辑实现
- 编写接口文档""",
    "tester": """- 编写测试计划与用例
- 执行功能测试和回归测试
- 缺陷跟踪与报告
- 自动化测试脚本编写""",
    "reviewer": """- 审查代码质量与规范
- 检查安全漏洞
- 建议最佳实践
- 确保架构一致性""",
    "devops": """- 配置部署环境
- 编写 Dockerfile/docker-compose
- CI/CD 流水线配置
- 监控与日志管理""",
}

DEFAULT_CONSTRAINTS = """- 只能在 /workspace 下操作文件
- 安装中间件前检查是否已存在
- 使用容器内 localhost 连接自启动的中间件
- 不要访问外部生产系统
- 不要泄露敏感凭据
- git commit 前确认 commit message 清晰"""


# ── settings.json 模板 ─────────────────────────────────────────────

SETTINGS_JSON_TEMPLATE = """{{
  "$schema": "https://code.visualstudio.org/schema/settings",
  "permissions": {{
    "allowed_tools": {allowed_tools_json},
    "denied_tools": {denied_tools_json}
  }},
  "agent": {{
    "name": "{name}",
    "role": "{role}"
  }}
}}"""

# 所有可用工具（Claude Code 默认）
ALL_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "WebSearch",
    "WebFetch",
]

# 角色默认工具权限映射
ROLE_DEFAULT_TOOLS: dict[str, list[str]] = {
    "frontend-engineer": ["Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"],
    "backend-engineer": ["Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"],
    "tester": ["Bash", "Read", "Write", "Edit", "WebSearch"],
    "reviewer": ["Read", "Edit", "WebSearch", "WebFetch"],
    "devops": ["Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"],
}


# ── 生成函数 ───────────────────────────────────────────────────────

import json


def generate_claude_md(
    name: str,
    role: str,
    extra_skills: list[str] | None = None,
    custom_system_prompt: str | None = None,
) -> str:
    """根据 AgentDefinition 生成 CLAUDE.md 文本"""

    # 如果用户填了自定义 system_prompt，直接使用
    if custom_system_prompt and custom_system_prompt.strip():
        return f"""# {name} 的角色定义

## 角色

{custom_system_prompt}

## 技能

{ _build_skills_section(role, extra_skills) }

## 约束

{DEFAULT_CONSTRAINTS}

## 环境与交接

- 工作目录 /workspace
- 产出物 -> /workspace/shared/
- 最终交付 -> /workspace/output/
- 完成后通知群主
"""

    role_desc = ROLE_DESCRIPTIONS.get(role, f"{role} — 自定义角色")
    responsibilities = ROLE_RESPONSIBILITIES.get(role, "- 根据需求完成分配的工作")
    skills_section = _build_skills_section(role, extra_skills)

    return CLAUDE_MD_TEMPLATE.format(
        name=name,
        role_description=role_desc,
        responsibilities=responsibilities,
        skills_section=skills_section,
        constraints=DEFAULT_CONSTRAINTS,
    )


def generate_settings_json(
    name: str,
    role: str,
    extra_allowed_tools: list[str] | None = None,
    extra_denied_tools: list[str] | None = None,
) -> str:
    """根据 AgentDefinition 生成 settings.json 文本"""

    default = ROLE_DEFAULT_TOOLS.get(role, ALL_TOOLS[:])
    allowed = list(set(default + (extra_allowed_tools or [])))
    denied = list(set(extra_denied_tools or []))

    # 如果 explicitly denied，从 allowed 移除
    allowed = [t for t in allowed if t not in denied]

    return SETTINGS_JSON_TEMPLATE.format(
        name=name,
        role=role,
        allowed_tools_json=json.dumps(allowed, indent=4, ensure_ascii=False),
        denied_tools_json=json.dumps(denied, indent=4, ensure_ascii=False),
    )


def _build_skills_section(role: str, extra_skills: list[str] | None) -> str:
    """构建技能描述文本"""
    lines: list[str] = []

    default_skills: dict[str, list[str]] = {
        "frontend-engineer": ["React/Vue 开发", "CSS/Tailwind 样式", "前端测试 (Jest/Vitest)"],
        "backend-engineer": ["Python/FastAPI 开发", "SQL 数据库操作", "API 设计与文档"],
        "tester": ["测试用例设计", "自动化测试 (pytest)", "缺陷跟踪"],
        "reviewer": ["代码审查", "安全检查", "架构评估"],
        "devops": ["Docker 容器化", "CI/CD 配置", "部署脚本"],
    }

    for skill in default_skills.get(role, []):
        lines.append(f"- {skill}（内置）")

    for skill in extra_skills or []:
        lines.append(f"- {skill}（技能市场挂载）")

    return "\n".join(lines) if lines else "- 通用开发技能"
