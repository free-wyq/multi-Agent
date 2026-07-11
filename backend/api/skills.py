"""Skill CRUD routes (M7: PRD 3.2 技能系统).

Routes map to the frontend ``skillApi``:
  GET    /api/skills                         → list_skills          (SK-09 浏览/搜索)
  GET    /api/skills/market                   → search_market_skills (SK-10 市场搜索)
  POST   /api/skills/market/install           → install_market_skill (SK-12 市场技能安装)
  GET    /api/skills/{id}                     → get_skill
  POST   /api/skills                          → create_skill         (SK-13 手动创建)
  POST   /api/skills/generate                 → generate_skill       (SK-01 自然语言生成)
  POST   /api/skills/upload                   → upload_skill         (SK-05 上传 SKILL.md)
  PUT    /api/skills/{id}                     → update_skill
  DELETE /api/skills/{id}                     → delete_skill        (SK-13 删除)
  POST   /api/skills/{id}/mount               → mount_skill         (SK-04/AG-08 挂载)
  POST   /api/skills/{id}/unmount             → unmount_skill       (AG-09 卸载)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from llm.client import chat_completion, get_llm_config
from llm.extract_json import extract_json
from models import AgentDefinition, Skill, SkillCreatePayload
from skill_hub import (
    MarketEntry,
    fetch_remote_entry_content,
    get_market_entry,
    search_market,
)
from store import crud

logger = logging.getLogger("multi-agent.skills")

router = APIRouter(prefix="/api/skills", tags=["skills"])

# SKILL.md 技能文档是 Markdown 文本，1MB 上限足够且防恶意/误传大文件撑爆内存。
_MAX_UPLOAD_BYTES = 1 * 1024 * 1024

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


@router.get("/market")
async def search_market_skills(
    q: str = Query("", description="搜索关键词（匹配 name/description/tags，大小写不敏感）"),
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
) -> list[MarketEntry]:
    """SK-10: 搜索技能市场（内置市场 + 可选远程 Hub）。

    调 ``skill_hub.search_market(query, limit)``：内置 catalog 恒可用，远程 Hub
    （``SKILL_HUB_URL`` 配置时）best-effort 叠加，失败静默回退 catalog-only。空 ``q``
    返回全部（受 ``limit`` 封顶）。返回 ``MarketEntry`` 列表（含 ``entry_id`` 供 SK-12
    安装解析；catalog 条目带 ``content`` 全文，远程条目带 ``source_url`` 待安装时拉取）。

    路由声明在 ``/{skill_id}`` 之前——``market`` 是字面段，须先于 path 参数匹配，
    否则会被当作 ``skill_id="market"`` 走 ``get_skill``（与 ``/upload`` 同处理）。
    """
    return await search_market(q, limit)


class InstallMarketBody(BaseModel):
    """SK-12: install a market skill by ``entry_id``.

    ``entry_id`` is the market entry's stable id returned by ``GET /market``
    (e.g. ``catalog:db-migration``). The endpoint resolves it via
    ``get_market_entry`` to fetch the full content, then creates a local Skill
    (``source="market"``) so it appears in "我的技能" and can be mounted to agents.
    """

    entry_id: str


@router.post("/market/install")
async def install_market_skill(body: InstallMarketBody) -> Skill:
    """SK-12: install a market skill to the local skill store.

    Resolves ``entry_id`` via ``skill_hub.get_market_entry``:
      - catalog entries carry full ``content`` → install directly;
      - remote entries may only carry ``source_url`` → best-effort fetch the
        body via ``fetch_remote_entry_content`` (failure aborts with 409, since
        a skill with no body is useless);
      - unknown ``entry_id`` → 404.

    Creates a local Skill with ``source="market"`` and the entry's
    name/description/tags/content. Idempotent-ish: repeated installs create
    duplicate local Skills (by design — the user can delete extras); this keeps
    the install path a thin delegation over the proven ``create_skill`` CRUD.

    Route declared before ``/{skill_id}`` — ``market/install`` is a two-segment
    literal path, must precede the ``{skill_id}`` path param so FastAPI does
    not treat ``market`` as a skill id (same ordering rule as ``/market`` and
    ``/upload``).
    """
    entry_id = (body.entry_id or "").strip()
    if not entry_id:
        raise HTTPException(status_code=400, detail="entry_id 不能为空")

    entry = await get_market_entry(entry_id)
    if entry is None:
        # 未知 entry_id：可能是已下架的市场条目或客户端伪造的 id
        raise HTTPException(
            status_code=404,
            detail=f"市场技能 {entry_id} 不存在（可能已下架或未配置远程 Hub）",
        )

    # 解析技能正文：catalog 自带 content；remote 仅 source_url 时 best-effort 拉取
    content = entry.content
    if not content and entry.source_url:
        content = await fetch_remote_entry_content(entry)
    if not content:
        # 无正文无法构成可用技能（worker 注入空 system_prompt 段无意义）
        raise HTTPException(
            status_code=409,
            detail=f"市场技能 {entry_id} 暂无可安装的技能文档正文",
        )

    payload = SkillCreatePayload(
        name=entry.name,
        description=entry.description or "",
        content=content,
        source="market",
        tags=list(entry.tags or []),
    )
    skill = await crud.create_skill(payload)
    logger.info(
        "[skills] installed market skill %s → local %s (%s)",
        entry_id,
        skill.id,
        skill.name,
    )
    return skill


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


@router.post("/upload")
async def upload_skill(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    description: str | None = Form(None),
    source: str = Form("custom"),
    tags: str | None = Form(None),
) -> Skill:
    """SK-05: upload an existing SKILL.md file as a skill.

    Accepts ``multipart/form-data``: one ``file`` (the SKILL.md body) plus
    optional metadata form fields. The file content becomes ``Skill.content``;
    ``name`` falls back to the uploaded file's stem (filename without ``.md``)
    when not supplied. ``tags`` is a JSON-encoded list string (multipart form
    fields are flat strings, so a list must be JSON-encoded client-side).

    This route is declared before ``/{skill_id}`` so FastAPI matches the
    literal ``upload`` segment instead of treating it as a path parameter.
    """
    # 读取并校验文件内容（Markdown 文本，UTF-8 解码）
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{len(raw)} 字节，上限 {_MAX_UPLOAD_BYTES} 字节）",
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"文件非 UTF-8 文本，无法作为技能文档：{exc}",
        ) from exc
    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="技能文档内容为空")

    # name 缺省回退文件 stem（去掉 .md/.markdown 扩展名）
    filename = file.filename or ""
    stem = Path(filename).stem or "uploaded_skill"
    skill_name = (name or "").strip() or stem

    # tags：multipart form 字段是扁平字符串，list 需客户端 JSON 编码后传
    parsed_tags: list[str] = []
    if tags:
        try:
            decoded = json.loads(tags)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"tags 不是合法 JSON 数组字符串：{exc}",
            ) from exc
        if not isinstance(decoded, list) or not all(
            isinstance(t, str) for t in decoded
        ):
            raise HTTPException(
                status_code=400,
                detail="tags 必须是 JSON 编码的字符串数组",
            )
        parsed_tags = decoded

    payload = SkillCreatePayload(
        name=skill_name,
        description=description,
        content=content,
        source=source or "custom",
        tags=parsed_tags,
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
