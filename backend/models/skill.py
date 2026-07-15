"""Skill + SkillCreatePayload Pydantic models.

A Skill is a reusable capability document: a natural-language description of
an ability an agent can use (PRD 3.2). Skills are mounted onto agents by id;
at execution time the engine injects the mounted skills' content into the
worker's system prompt (PL-06).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Skill(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str = ""
    # source: builtin | market | custom (PRD SK-09)
    source: str = "custom"
    # installation status: installed | not_installed (PRD SK-09)
    installed: bool = True
    # the natural-language skill body — what gets injected into the agent prompt
    content: str = ""
    # optional tags for search/filter (PRD SK-09)
    tags: list[str] = []
    # ── frontmatter (Claude Skills 化 · 阶段一地基2) ───────────────
    # 抄 Claude Skills 的 name/description 元数据思想 + 扩出可执行性字段。
    # 三字段皆可选、默认空 list，向后兼容旧数据（缺字段序列化兜底成空 list）。
    #   requires_tools: 该技能需要绑定的受控工具名（如 ["file_read","bash_run"]），
    #     非空 → 阶段四由 make_agent_node 解析后 bind_tools；空 = 纯文档技能只走 prompt 注入。
    #   triggers: 触发场景关键词（人读辅助 + 未来可作自动激活线索），如 ["建表","迁移"]。
    #   outputs: 该技能会产出的产物类别（人读辅助 + 产物卡渲染线索），如 ["sql","migration"]。
    requires_tools: list[str] = []
    triggers: list[str] = []
    outputs: list[str] = []
    # agent ids this skill is mounted on (computed at read time, not stored)
    mounted_to: list[str] = []
    # on-disk assets (scripts/ + templates/) under DATA_DIR/skills/{id}/ —
    # computed at read time from the filesystem, not stored in DB (stage 3 · task33).
    # Empty for old content-only skills with no assets dir.
    assets: list[str] = []
    created_at: str = ""
    updated_at: str = ""


class SkillCreatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    content: str | None = None
    source: str = "custom"
    tags: list[str] = []
    # frontmatter（阶段一地基2，皆可选，默认空 list 向后兼容）
    requires_tools: list[str] = []
    triggers: list[str] = []
    outputs: list[str] = []


class SkillUploadPayload(BaseModel):
    """SK-05: metadata for uploading an existing SKILL.md file as a skill.

    Used by ``POST /api/skills/upload`` as a multipart ``Form()`` body — the
    file itself is a separate ``UploadFile`` parameter, and this model carries
    only the optional metadata. ``name`` is optional because the endpoint
    falls back to the uploaded file's stem (filename without ``.md``) when the
    user does not supply an explicit name. ``source`` defaults to ``custom``
    (uploaded skills are user-provided, not ``builtin``/``market``) to stay
    within the existing ``Skill.source`` taxonomy consumed by SK-09 search.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    description: str | None = None
    source: str = "custom"
    tags: list[str] = []
