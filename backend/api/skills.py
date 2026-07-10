"""Skill CRUD routes (M7: PRD 3.2 技能系统).

Routes map to the frontend ``skillApi``:
  GET    /api/skills                         → list_skills          (SK-09 浏览/搜索)
  GET    /api/skills/{id}                     → get_skill
  POST   /api/skills                          → create_skill         (SK-13 手动创建)
  POST   /api/skills/generate                 → generate_skill       (SK-01 自然语言生成)
  PUT    /api/skills/{id}                     → update_skill
  DELETE /api/skills/{id}                     → delete_skill        (SK-13 删除)
  POST   /api/skills/{id}/mount               → mount_skill         (SK-04/AG-08 挂载)
  POST   /api/skills/{id}/unmount             → unmount_skill       (AG-09 卸载)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from llm.client import chat_completion, get_llm_config
from llm.extract_json import extract_json
from models import AgentDefinition, Skill, SkillCreatePayload
from store import crud

logger = logging.getLogger("multi-agent.skills")

router = APIRouter(prefix="/api/skills", tags=["skills"])

_GENERATE_PROMPT = """你是一个技能文档生成器。用户会用自然语言描述一个技能，你需要生成一份标准技能文档。

用户描述：{desc}

请严格按照以下 JSON 格式回复（只输出纯 JSON）：
{{
  "name": "技能名称（简洁，中文）",
  "description": "一句话描述技能用途",
  "content": "技能的详细说明文档（Markdown 格式，包含：用途、适用场景、使用步骤、注意事项）",
  "tags": ["标签1", "标签2"]
}}"""


class GenerateSkillBody(BaseModel):
    description: str


class MountBody(BaseModel):
    agentId: str


async def _generate_skill_via_llm(description: str) -> dict:
    """Call the LLM to turn a natural-language description into a skill doc (SK-01).

    Returns a dict with name/description/content/tags. Falls back to a bare
    name+description if the LLM call or JSON parse fails.
    """
    config = get_llm_config()
    try:
        raw = await chat_completion(
            config,
            [{"role": "user", "content": _GENERATE_PROMPT.format(desc=description)}],
        )
        parsed = extract_json(raw)
    except Exception as exc:
        logger.warning("[skills] generate LLM failed: %s", exc)
        parsed = None
    if not parsed:
        return {
            "name": description[:32],
            "description": description[:200],
            "content": description,
            "tags": [],
        }
    return {
        "name": str(parsed.get("name") or description[:32])[:64],
        "description": str(parsed.get("description") or "")[:500],
        "content": str(parsed.get("content") or ""),
        "tags": list(parsed.get("tags") or []),
    }


@router.get("")
async def list_skills() -> list[Skill]:
    return await crud.list_skills()


@router.get("/{skill_id}")
async def get_skill(skill_id: str) -> Skill | None:
    return await crud.get_skill(skill_id)


@router.post("")
async def create_skill(payload: SkillCreatePayload) -> Skill:
    return await crud.create_skill(payload)


@router.post("/generate")
async def generate_skill(body: GenerateSkillBody) -> Skill:
    """SK-01: generate a skill document from a natural-language description."""
    fields = await _generate_skill_via_llm(body.description)
    payload = SkillCreatePayload(
        name=fields["name"],
        description=fields["description"],
        content=fields["content"],
        source="custom",
        tags=fields["tags"],
    )
    return await crud.create_skill(payload)


@router.put("/{skill_id}")
async def update_skill(skill_id: str, payload: SkillCreatePayload) -> Skill | None:
    return await crud.update_skill(skill_id, payload)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str) -> bool:
    return await crud.delete_skill(skill_id)


@router.post("/{skill_id}/mount")
async def mount_skill(skill_id: str, body: MountBody) -> AgentDefinition | None:
    return await crud.mount_skill(body.agentId, skill_id)


@router.post("/{skill_id}/unmount")
async def unmount_skill(skill_id: str, body: MountBody) -> AgentDefinition | None:
    return await crud.unmount_skill(body.agentId, skill_id)
