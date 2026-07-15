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
  POST   /api/skills/{id}/run                 → run_skill            (阶段四·task38 运行)
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from starlette.responses import StreamingResponse

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
from store import skill_assets

logger = logging.getLogger("multi-agent.skills")

router = APIRouter(prefix="/api/skills", tags=["skills"])

# SKILL.md 技能文档是 Markdown 文本，1MB 上限足够且防恶意/误传大文件撑爆内存。
_MAX_UPLOAD_BYTES = 1 * 1024 * 1024

# 阶段三·task34：上传目录/zip 时的总资产上限（scripts/+templates/ 合计），与
# skill_assets._MAX_TOTAL_ASSETS 对齐——这里在解包前再兜一道，防 zip 内超大资产
# 在落盘前先撑爆内存。
_MAX_ZIP_TOTAL_BYTES = 10 * 1024 * 1024

# zip 解包时单文件上限（防 zip bomb：一个压缩条目解出来超大）
_MAX_ZIP_ENTRY_BYTES = 1 * 1024 * 1024

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


class RunSkillBody(BaseModel):
    """阶段四·task38: 运行一个可执行技能的请求体.

    ``prompt`` 是驱动该次运行的自然语言指令（默认让技能按自身 content 自主执行）。
    ``max_turns`` 可选覆盖（不传用 ``run_skill_loop`` 默认 15）。
    """

    prompt: str | None = None
    max_turns: int | None = None


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
    """SK-05: upload an existing SKILL.md file (or a skill directory zip) as a skill.

    Accepts ``multipart/form-data``: one ``file``. Two modes:

    - **Single SKILL.md** (default, backward-compatible): the file content
      becomes ``Skill.content``; ``name`` falls back to the file's stem (filename
      without ``.md``).
    - **Directory/zip** (stage 3 · task34): when the upload is a ``.zip`` (or
      the filename endswith ``.zip``), it is unpacked — the ``SKILL.md`` at the
      archive root (or one level under it) becomes ``content``, and
      ``scripts/`` + ``templates/`` subdirs become on-disk assets under
      ``DATA_DIR/skills/{id}/``. Supports the Claude Skills "one skill = one
      directory" self-contained layout.

    Optional metadata form fields: ``name`` / ``description`` / ``source`` /
    ``tags`` (tags is a JSON-encoded list string — multipart form fields are
    flat strings, so a list must be JSON-encoded client-side).

    This route is declared before ``/{skill_id}`` so FastAPI matches the
    literal ``upload`` segment instead of treating it as a path parameter.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    filename = file.filename or ""

    # 阶段三·task34：zip → 目录技能（SKILL.md→content + scripts/templates→assets）
    if filename.lower().endswith(".zip"):
        return await _upload_skill_zip(raw, filename, name, description, source, tags)

    # 单文件 SKILL.md 路径（向后兼容原行为）
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
    stem = Path(filename).stem or "uploaded_skill"
    skill_name = (name or "").strip() or stem

    parsed_tags = _parse_tags_form(tags)

    payload = SkillCreatePayload(
        name=skill_name,
        description=description,
        content=content,
        source=source or "custom",
        tags=parsed_tags,
    )
    return await crud.create_skill(payload)


async def _upload_skill_zip(
    raw: bytes,
    filename: str,
    name: str | None,
    description: str | None,
    source: str,
    tags: str | None,
) -> Skill:
    """Unpack an uploaded skill zip into a directory skill (stage 3 · task34).

    Layout (Claude Skills self-contained): archive root may be flat or have one
    top-level directory (common when zipping a folder). Recognized files:

    - ``SKILL.md`` (root or one level under) → ``Skill.content``
    - ``scripts/**`` and ``templates/**`` → on-disk assets

    Safety: each entry is size-bounded + total-bounded (zip-bomb defense), and
    asset writes go through ``skill_assets.write_skill_asset`` (which enforces
    the scripts/templates whitelist + path-traversal protection).
    """
    if len(raw) > _MAX_ZIP_TOTAL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zip 过大（{len(raw)} 字节，上限 {_MAX_ZIP_TOTAL_BYTES} 字节）",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=400, detail=f"不是合法 zip 文件：{exc}"
        ) from exc

    # 找公共前缀（容许 zip 内多一层目录）：一个 skill 目录 zip 通常打成
    # skillname/SKILL.md + skillname/scripts/... 形式，前缀 = skillname/。
    names = [n for n in zf.namelist() if n and not n.endswith("/")]
    if not names:
        raise HTTPException(status_code=400, detail="zip 内无可识别文件")
    prefix = _common_zip_prefix(names)

    # 定位 SKILL.md（前缀下根级）
    skill_md_name = f"{prefix}SKILL.md"
    skill_md_alt = f"{prefix}skill.md"
    md_member = None
    if skill_md_name in zf.namelist():
        md_member = skill_md_name
    elif skill_md_alt in zf.namelist():
        md_member = skill_md_alt
    if md_member is None:
        raise HTTPException(
            status_code=400,
            detail="zip 内未找到 SKILL.md（应在归档根或一层目录下）",
        )

    # 解 SKILL.md → content（带总量上限累加，防 zip bomb）
    total = 0
    md_raw = zf.read(md_member)
    total += len(md_raw)
    if len(md_raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"SKILL.md 过大（{len(md_raw)} 字节，上限 {_MAX_UPLOAD_BYTES} 字节）",
        )
    try:
        content = md_raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"SKILL.md 非 UTF-8 文本：{exc}"
        ) from exc
    if not content:
        raise HTTPException(status_code=400, detail="SKILL.md 内容为空")

    # name 缺省：form name > zip 文件 stem > 前缀目录名
    stem = Path(filename).stem or "uploaded_skill"
    prefix_dir = prefix.rstrip("/") if prefix else ""
    skill_name = (name or "").strip() or stem or prefix_dir or "uploaded_skill"
    parsed_tags = _parse_tags_form(tags)

    payload = SkillCreatePayload(
        name=skill_name,
        description=description,
        content=content,
        source=source or "custom",
        tags=parsed_tags,
    )
    skill = await crud.create_skill(payload)

    # 解 scripts/ + templates/ → 落盘资产（走 write_skill_asset 的白名单+越界校验）
    asset_members = [
        n for n in zf.namelist()
        if n and not n.endswith("/") and n != md_member
    ]
    written: list[str] = []
    asset_errors: list[str] = []
    for member in asset_members:
        # 去掉前缀得到 skill_assets 期望的相对路径（scripts/... / templates/...）
        rel = member[len(prefix):] if prefix and member.startswith(prefix) else member
        # 只接受 scripts/ 或 templates/ 下的（write_skill_asset 会再校验，这里先快筛）
        top = rel.split("/")[0] if rel else ""
        if top not in ("scripts", "templates"):
            continue  # 非 assets 资产（如 README/LICENSE）跳过，不报错
        data = zf.read(member)
        total += len(data)
        if total > _MAX_ZIP_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"解包总量超限（上限 {_MAX_ZIP_TOTAL_BYTES} 字节）",
            )
        if len(data) > _MAX_ZIP_ENTRY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"zip 条目过大（{member}：{len(data)} 字节，上限 {_MAX_ZIP_ENTRY_BYTES} 字节）",
            )
        try:
            skill_assets.write_skill_asset(skill.id, rel, data)
            written.append(rel)
        except ValueError as exc:
            # 越界/非白名单条目记下继续（不全失败，让合法资产落盘）
            asset_errors.append(f"{member}: {exc}")
    logger.info(
        "[skills] uploaded zip skill %s (%s) — %d assets written%s",
        skill.id, skill.name, len(written),
        f", {len(asset_errors)} rejected" if asset_errors else "",
    )
    if asset_errors:
        logger.debug("[skills] zip asset rejections: %s", asset_errors)

    # 重新读一遍带 assets 字段的 skill 返回（create 时 assets 还没落盘）
    return await crud.get_skill(skill.id)  # type: ignore[return-value]


def _parse_tags_form(tags: str | None) -> list[str]:
    """Parse the JSON-encoded tags form field (multipart flat string → list)."""
    parsed_tags: list[str] = []
    if not tags:
        return parsed_tags
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
    return decoded


def _common_zip_prefix(names: list[str]) -> str:
    """Return the common top-level dir prefix if all entries share one (else '').

    e.g. ``['myskill/SKILL.md', 'myskill/scripts/x.sh']`` → ``'myskill/'``;
    ``['SKILL.md', 'scripts/x.sh']`` → ``''`` (flat layout, no prefix).
    """
    tops = set()
    for n in names:
        if "/" in n:
            tops.add(n.split("/")[0])
        else:
            return ""  # 有根级文件 → 无前缀
    if len(tops) == 1:
        top = tops.pop()
        # 前缀只接受当它是「一层目录」时；单文件名当 top 的不会进这里
        return f"{top}/"
    return ""


@router.put("/{skill_id}")
async def update_skill(skill_id: str, payload: SkillCreatePayload) -> Skill | None:
    return await crud.update_skill(skill_id, payload)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str) -> bool:
    return await crud.delete_skill(skill_id)


@router.post("/{skill_id}/mount")
async def mount_skill(skill_id: str, body: MountBody) -> AgentDefinition | None:
    """SK-04/AG-08: mount a skill onto an agent.

    阶段四·task36：若该技能声明了 ``requires_tools``，挂载时校验引用的工具名
    都在受控工具池（``file_read``/``file_write``/``bash_run``）内。引用了未知
    工具名 → 不阻断挂载（技能仍可作纯文档注入），但记 warning 日志让运维可见
    （前端可后续在挂载结果里展示）。挂载本身是幂等的 DB append，工具校验是
    best-effort 告警，不改变 mount 的返回契约。
    """
    # best-effort requires_tools 校验（不阻断挂载，只告警）
    skill = await crud.get_skill(skill_id)
    if skill and skill.requires_tools:
        from engine.tools import SKILL_TOOL_NAMES

        unknown = [t for t in skill.requires_tools if t not in SKILL_TOOL_NAMES]
        if unknown:
            logger.warning(
                "[skills] mount skill %s (%s): requires_tools 引用未知工具 %s "
                "（可用 %s）—— 技能仍作纯文档注入，受控工具绑定跳过这些项",
                skill_id, skill.name, unknown, list(SKILL_TOOL_NAMES),
            )
    return await crud.mount_skill(body.agentId, skill_id)


@router.post("/{skill_id}/unmount")
async def unmount_skill(skill_id: str, body: MountBody) -> AgentDefinition | None:
    return await crud.unmount_skill(body.agentId, skill_id)


@router.post("/{skill_id}/run")
async def run_skill(skill_id: str, body: RunSkillBody):
    """阶段四·task38: 运行一个可执行技能（带受控工具 + 沙箱）。

    起一个临时 agent：该技能的 ``content`` 作 system prompt，``requires_tools``
    解析出的受控工具（``file_read``/``file_write``/``bash_run``，绑该技能自家
    沙箱 workspace）经 ``bind_tools`` 注入，跑 ``run_skill_loop``（``create_react_agent``
    + ``astream_events``）。流式回传 token/tool/think/answer 事件（SSE），最终
    返回 ``{ok, run_id, output_path}``。

    安全契约（task40 全审锁死）：
    - 仅 ``requires_tools`` 非空的技能可运行（纯文档技能无工具不可执行 → 400）；
    - 不污染群聊 GroupState（独立执行，非群图回合）；
    - 工具 cwd 限 ``DATA_DIR/skills/{id}/workspace/``，产物落 ``output/``，
      路径穿越/危险命令由工具层拒绝（task35 denylist + safe_skill_path）。

    流式协议：SSE（``text/event-stream``），每事件一行 ``data: <json>\\n\\n``。
    事件 ``kind`` ∈ token/tool_start/tool_end/think/answer/log；最后一条
    ``{"kind":"done","ok":...,"run_id":...,"output_path":...}`` 收尾。
    """
    skill = await crud.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"技能 {skill_id} 不存在")

    # 安全契约①：仅 requires_tools 非空的可运行（纯文档技能无工具不可执行）
    if not skill.requires_tools:
        raise HTTPException(
            status_code=400,
            detail="该技能未声明 requires_tools（纯文档技能不可运行，请挂载到智能体后在群聊中使用）",
        )

    # 校验 requires_tools 全部在受控工具池（防御性·mount 时已告警，run 时硬拒）
    from engine.tools import SKILL_TOOL_NAMES, resolve_skill_tools

    unknown = [t for t in skill.requires_tools if t not in SKILL_TOOL_NAMES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"技能引用了未知工具 {unknown}（可用：{list(SKILL_TOOL_NAMES)}）",
        )

    run_id = f"run_{uuid4().hex}"
    # 准备沙箱 workspace（含 output/ 子目录）+ 解析受控工具
    skill_assets.skill_workspace_path(skill_id)
    manifest = [{
        "id": skill_id, "name": skill.name, "description": skill.description or "",
        "requires_tools": list(skill.requires_tools),
        "triggers": list(skill.triggers or []),
        "outputs": list(skill.outputs or []),
    }]
    tools, tool_warnings = resolve_skill_tools(manifest)
    if tool_warnings:
        # 多技能同名碰撞不会发生（单技能 run），但未知工具 run 时已硬拒上方；
        # 这里仅记录到日志，不阻断（tools 非空即可继续）。
        for w in tool_warnings:
            logger.warning("[skills] run_skill %s: %s", skill_id, w)
    if not tools:
        raise HTTPException(
            status_code=400,
            detail="技能 requires_tools 解析后无可用工具（可能是工具名未知或重复）",
        )

    prompt = (body.prompt or "").strip() or "请按本技能文档的指引自主执行，产物输出到 output/ 目录。"
    max_turns = body.max_turns or 15

    async def event_stream():
        """SSE generator: project run_skill_loop events onto text/event-stream."""
        from engine.agent_loop import run_skill_loop

        queue: asyncio.Queue = asyncio.Queue()

        async def on_event(kind: str, content: str, data: dict | None = None):
            await queue.put({"kind": kind, "content": content, "data": data})

        async def runner():
            try:
                result = await run_skill_loop(
                    skill_id=skill_id,
                    skill_name=skill.name,
                    skill_content=skill.content or "",
                    prompt=prompt,
                    tools=tools,
                    on_event=on_event,
                    max_turns=max_turns,
                )
                # 扫描产物目录，给出 output_path（首个产物文件相对路径）
                out_dir = skill_assets.skill_output_path(skill_id)
                # 产物在 output/ 子目录下，单独扫一遍
                products: list[str] = []
                if out_dir.exists():
                    for f in sorted(out_dir.rglob("*")):
                        if f.is_file():
                            products.append(str(f.relative_to(out_dir)))
                output_path = (out_dir / products[0]).as_posix() if products else None
                await queue.put({
                    "kind": "done",
                    "ok": bool(result.get("success")),
                    "run_id": run_id,
                    "output_path": output_path,
                    "products": products,
                    "exit_code": result.get("exit_code"),
                    "output": (result.get("output") or "")[:2000],
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception("[skills] run_skill %s failed", skill_id)
                await queue.put({
                    "kind": "done", "ok": False, "run_id": run_id,
                    "output_path": None, "error": str(exc),
                })
            finally:
                await queue.put(None)  # sentinel: stream end

        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲，保证 SSE 实时
        },
    )
