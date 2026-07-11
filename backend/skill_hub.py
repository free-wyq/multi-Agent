"""Skill Hub — market skill discovery (PRD SK-10).

Search installable skills from a curated catalog plus, optionally, a remote public
Hub (ClawHub / SkillsMP) configured via env. This module is the single search
entry point backing ``GET /api/skills/market?q=`` (next task).

Design (lowest-risk, always-deterministic):
  - The curated catalog is the **default and always-available** provider. It ships
    a static set of real skill documents (Markdown bodies) so search returns
    results without any network dependency — this makes the SK-10 self-test
    deterministic and keeps the feature usable in air-gapped / unconfigured envs.
  - The **remote Hub adapter** is a best-effort overlay: when ``SKILL_HUB_URL``
    is set, it is queried first and its results merged on top; on ANY failure
    (network, auth, parse, timeout) it logs a warning and is silently skipped,
    falling back to the catalog alone. This is the honest "对接公开 Hub" seam —
    real external Hub integration plugs in here, but nothing breaks when it is
    unconfigured or unreachable.
  - ``MarketEntry`` is the DTO returned to the frontend; ``get_market_entry`` +
    ``entry.content`` back SK-12 (install market skill → store as local Skill).

Field names are snake_case to match the frontend ``api.ts`` convention (see
``Skill`` / ``GroupMember``). The catalog is intentionally a module-level tuple so
it is cheap to load and easy to extend.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("multi-agent.skill_hub")

# Remote Hub is given a short, bounded timeout — it is a best-effort overlay, not
# a critical path. We never want a slow / hung external Hub to block the market
# search endpoint (which must stay snappy for the UI).
_REMOTE_TIMEOUT = 8.0
# Hard cap on merged results so a misbehaving remote Hub cannot flood the UI.
_DEFAULT_LIMIT = 50
# Upper bound on a single remote skill body fetched at install time — keeps a
# misbehaving/remote Hub from pushing a huge payload through the install path.
_MAX_INSTALL_BYTES = 1 * 1024 * 1024


class MarketEntry(BaseModel):
    """A skill discovered in the market (not yet installed locally).

    Mirrors the subset of ``Skill`` fields the market UI needs plus provenance
    (``hub`` / ``author`` / ``version`` / ``entry_id``) so the user can tell
    sources apart and SK-12 install can resolve the full body via ``content``.
    """

    model_config = ConfigDict(extra="allow")

    # Stable id within the hub (e.g. "catalog:db-migration" or remote "clawhub:42").
    # Prefixed with the hub name so ids are globally unique across providers.
    entry_id: str
    name: str
    description: str = ""
    tags: list[str] = []
    # Full SKILL.md body — present for catalog entries; for remote entries it is
    # fetched lazily by get_market_entry (so list responses stay small).
    content: str | None = None
    # Provenance
    hub: str = "catalog"
    author: str = ""
    version: str = ""
    # Remote-only: a URL/ref to fetch the full body at install time.
    source_url: str | None = None


# ── Curated catalog ────────────────────────────────────────────────────
# Each tuple: (slug, name, description, tags, markdown body). The slug becomes the
# catalog entry_id ("catalog:<slug>"). Bodies are real, usable skill docs so that
# installing one (SK-12) yields a genuinely useful local Skill, not a stub.
_CATALOG: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (
    (
        "db-migration",
        "数据库迁移技能",
        "生成并校验数据库 schema 迁移脚本（up/down），检测破坏性变更并给出回滚建议。",
        ("database", "migration", "sql", "数据库"),
        "# 数据库迁移技能\n\n## 用途\n生成可逆的数据库 schema 迁移脚本并校验安全性。\n\n"
        "## 适用场景\n- 新增/修改表结构\n- 新增索引或字段\n- 重命名列\n\n"
        "## 使用步骤\n1. 读取当前 schema 与目标 schema 的差异\n2. 生成 up/down 迁移脚本\n"
        "3. 检测破坏性变更（删列/改类型）并提示风险\n4. 给出回滚方案\n\n"
        "## 注意事项\n- 始终提供 down 脚本\n- 破坏性变更需人工确认\n",
    ),
    (
        "api-doc-gen",
        "API 文档生成技能",
        "从后端路由/处理函数自动生成 OpenAPI 文档与示例，保持文档与代码同步。",
        ("api", "docs", "openapi", "文档"),
        "# API 文档生成技能\n\n## 用途\n从代码自动生成并维护 API 文档。\n\n"
        "## 适用场景\n- 新增接口后补文档\n- 对齐 OpenAPI 规范\n- 生成请求/响应示例\n\n"
        "## 使用步骤\n1. 扫描路由定义与类型注解\n2. 抽取参数/响应结构\n"
        "3. 生成 OpenAPI 片段并合并到主文档\n4. 补充示例与错误码\n\n"
        "## 注意事项\n- 文档与代码同源，避免手写漂移\n",
    ),
    (
        "performance-test",
        "性能测试技能",
        "编写并执行性能基准测试，识别瓶颈，给出优化建议与回归基线。",
        ("performance", "test", "benchmark", "性能"),
        "# 性能测试技能\n\n## 用途\n建立性能基线并识别瓶颈。\n\n"
        "## 适用场景\n- 关键路径性能回归守护\n- 上线前压测\n- 瓶颈定位\n\n"
        "## 使用步骤\n1. 识别需基准化的热点函数/接口\n2. 编写基准用例\n"
        "3. 多轮运行取中位数\n4. 对比基线输出回归报告\n\n"
        "## 注意事项\n- 基线随版本演进，需定期更新\n",
    ),
    (
        "code-review",
        "代码审查技能",
        "对变更 diff 做结构化代码审查，聚焦正确性、可读性与回归风险，给出可执行建议。",
        ("review", "quality", "code", "审查"),
        "# 代码审查技能\n\n## 用途\n对变更做结构化审查并给出可执行建议。\n\n"
        "## 适用场景\n- 提交前自审\n- PR 评审\n- 关键模块变更把关\n\n"
        "## 使用步骤\n1. 读取变更 diff\n2. 按正确性/可读性/回归风险分维评审\n"
        "3. 标注严重级别并给出修改示例\n\n## 注意事项\n- 评审聚焦 diff，不重写整个文件\n",
    ),
    (
        "security-review",
        "安全审查技能",
        "审查代码与依赖的安全风险（注入、越权、密钥泄露、已知漏洞依赖），输出风险清单。",
        ("security", "review", "audit", "安全"),
        "# 安全审查技能\n\n## 用途\n识别安全风险并输出可处置清单。\n\n"
        "## 适用场景\n- 上线前安全检查\n- 引入新依赖后\n- 鉴权/输入处理变更\n\n"
        "## 使用步骤\n1. 扫描输入处理与鉴权边界\n2. 检查依赖已知漏洞\n"
        "3. 标注风险等级与修复建议\n\n## 注意事项\n- 密钥/凭证不得出现在日志\n",
    ),
    (
        "git-workflow",
        "Git 工作流技能",
        "规范分支、提交信息（Conventional Commits）与 PR 流程，自动生成提交信息与变更摘要。",
        ("git", "workflow", "commit", "规范"),
        "# Git 工作流技能\n\n## 用途\n规范 Git 提交与分支流程。\n\n"
        "## 适用场景\n- 提交信息规范化\n- PR 描述生成\n- 分支策略落地\n\n"
        "## 使用步骤\n1. 分析 staged diff\n2. 生成 Conventional Commits 提交信息\n"
        "3. 生成 PR 描述与变更摘要\n\n## 注意事项\n- 提交信息前缀需匹配变更类型\n",
    ),
    (
        "test-gen",
        "测试生成技能",
        "根据实现代码生成单元/集成测试，覆盖边界与异常路径，输出可运行的测试用例。",
        ("test", "coverage", "unit", "测试"),
        "# 测试生成技能\n\n## 用途\n根据实现生成可运行测试用例。\n\n"
        "## 适用场景\n- 新功能补测\n- 提升覆盖率\n- 回归守护\n\n"
        "## 使用步骤\n1. 读取目标实现与公开接口\n2. 生成正常/边界/异常用例\n"
        "3. 运行测试并修正常失败\n\n## 注意事项\n- 测试不得依赖外部不可控状态\n",
    ),
    (
        "refactor",
        "重构简化技能",
        "在不改变外部行为的前提下重构代码：提取重复、消除冗余、降低圈复杂度，保留测试绿。",
        ("refactor", "quality", "simplify", "重构"),
        "# 重构简化技能\n\n## 用途\n在不改变行为前提下简化代码结构。\n\n"
        "## 适用场景\n- 降低圈复杂度\n- 消除重复\n- 提升可读性\n\n"
        "## 使用步骤\n1. 识别重复/过长函数/复杂条件\n2. 小步重构并保持测试绿\n"
        "3. 输出变更摘要\n\n## 注意事项\n- 行为不变是硬约束，先有测试再重构\n",
    ),
    (
        "i18n",
        "国际化技能",
        "抽取硬编码文案到资源文件，生成多语言模板与 key 规范，支持增量补齐翻译。",
        ("i18n", "l10n", "frontend", "国际化"),
        "# 国际化技能\n\n## 用途\n抽取文案并建立多语言资源体系。\n\n"
        "## 适用场景\n- 项目接入 i18n\n- 新增文案补齐翻译\n- key 规范治理\n\n"
        "## 使用步骤\n1. 扫描硬编码文案\n2. 生成资源文件与 key\n3. 补齐多语言条目\n\n"
        "## 注意事项\n- key 命名需稳定且有语义\n",
    ),
    (
        "observability",
        "可观测性技能",
        "为服务补充结构化日志、指标与链路追踪埋点，输出关键 SLI/SLO 定义。",
        ("observability", "logging", "metrics", "可观测"),
        "# 可观测性技能\n\n## 用途\n建立日志/指标/追踪的可观测基线。\n\n"
        "## 适用场景\n- 排障链路补全\n- 关键 SLI 定义\n- 告警阈值设计\n\n"
        "## 使用步骤\n1. 识别关键路径与失败模式\n2. 补充结构化日志与指标\n"
        "3. 定义 SLI/SLO 与告警\n\n## 注意事项\n- 高基数 label 会导致指标爆炸\n",
    ),
)


def _catalog_entries() -> list[MarketEntry]:
    """Materialize the static catalog into MarketEntry objects."""
    out: list[MarketEntry] = []
    for slug, name, desc, tags, body in _CATALOG:
        out.append(
            MarketEntry(
                entry_id=f"catalog:{slug}",
                name=name,
                description=desc,
                tags=list(tags),
                content=body,
                hub="catalog",
                author="WorkMate 内置市场",
                version="1.0",
            )
        )
    return out


# In-memory index for O(1) lookup by entry_id (SK-12 install resolves content).
_CATALOG_INDEX: dict[str, MarketEntry] = {e.entry_id: e for e in _catalog_entries()}


def _matches(entry: MarketEntry, q: str) -> bool:
    """Case-insensitive substring match across name/description/tags.

    Mirrors the frontend filter semantics (SkillPage filteredSkills) so market
    search behaves consistently with local SK-09 search.
    """
    qq = q.strip().lower()
    if not qq:
        return True
    if qq in entry.name.lower():
        return True
    if qq in (entry.description or "").lower():
        return True
    return any(qq in str(t).lower() for t in entry.tags)


def _search_catalog(query: str) -> list[MarketEntry]:
    return [e for e in _CATALOG_INDEX.values() if _matches(e, query)]


async def _search_remote_hub(query: str) -> list[MarketEntry]:
    """Best-effort remote Hub search (ClawHub / SkillsMP).

    Reads ``SKILL_HUB_URL``. When unset (the default in unconfigured envs) this
    is a no-op returning ``[]`` — the catalog alone serves the market. When set,
    it issues ``GET {url}?q={query}`` and expects a JSON list of objects with at
    least ``name``; remaining fields are optional and mapped defensively.

    Any failure (network, non-200, parse error, timeout) is logged at warning
    level and returns ``[]`` — remote is an overlay, never a hard dependency.
    """
    url = os.environ.get("SKILL_HUB_URL", "").strip()
    if not url:
        return []
    request_url = url.rstrip("/") + ("?q=" + httpx.QueryParams({"q": query}).get("q", "") if query else "")
    try:
        async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
            resp = await client.get(request_url)
            if resp.status_code != 200:
                logger.warning("[skill_hub] remote %s returned %s", url, resp.status_code)
                return []
            data = resp.json()
        if not isinstance(data, list):
            logger.warning("[skill_hub] remote %s returned non-list payload", url)
            return []
        out: list[MarketEntry] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            tags = item.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            out.append(
                MarketEntry(
                    entry_id=str(item.get("id") or f"remote:{i}"),
                    name=str(name),
                    description=str(item.get("description") or ""),
                    tags=[str(t) for t in tags],
                    content=item.get("content"),
                    hub=str(item.get("hub") or "remote"),
                    author=str(item.get("author") or ""),
                    version=str(item.get("version") or ""),
                    source_url=item.get("source_url") or item.get("url"),
                )
            )
        logger.info("[skill_hub] remote %s returned %d entries", url, len(out))
        return out
    except Exception as exc:  # noqa: BLE001 — overlay must never raise
        logger.warning("[skill_hub] remote hub %s failed: %s", url, exc)
        return []


def _dedupe(entries: list[MarketEntry]) -> list[MarketEntry]:
    """Dedupe by entry_id, preserving first occurrence (catalog wins ties when
    merged after remote — but here remote is prepended so remote wins on conflict,
    which is the intended "overlay on top" semantics)."""
    seen: set[str] = set()
    out: list[MarketEntry] = []
    for e in entries:
        if e.entry_id in seen:
            continue
        seen.add(e.entry_id)
        out.append(e)
    return out


async def search_market(query: str = "", limit: int = _DEFAULT_LIMIT) -> list[MarketEntry]:
    """Search the skill market (SK-10).

    Merges remote Hub results (best-effort, tried first) with the curated catalog,
    dedupes by ``entry_id``, filters by ``query`` (case-insensitive substring on
    name/description/tags; empty query returns all), and caps at ``limit``.

    Always returns a list — never raises: remote failures degrade to catalog only.
    """
    remote = await _search_remote_hub(query)
    catalog = _search_catalog(query)
    # Remote first so it overlays on top of the catalog on id conflicts.
    merged = _dedupe(remote + catalog)
    # Re-filter the merged set in case remote returned unfiltered payloads.
    if query:
        merged = [e for e in merged if _matches(e, query)]
    if limit and limit > 0:
        merged = merged[:limit]
    return merged


async def get_market_entry(entry_id: str) -> MarketEntry | None:
    """Resolve a market entry by id (SK-12 install source).

    Catalog entries return with full ``content``. Remote entries without bundled
    content are returned as-is (their ``source_url`` can be fetched at install
    time by the SK-12 endpoint); a missing id returns ``None``.
    """
    if entry_id in _CATALOG_INDEX:
        return _CATALOG_INDEX[entry_id]
    # Remote entries are not indexed (they come and go per search); allow a final
    # best-effort remote re-search to surface the id, but we cannot reliably map
    # an arbitrary remote id back to a payload without the source_url. SK-12 will
    # handle remote installs via source_url; here we only guarantee catalog ids.
    return None


async def fetch_remote_entry_content(entry: MarketEntry) -> str | None:
    """Best-effort fetch of a remote entry's full content for SK-12 install.

    Catalog entries already carry ``content``; remote entries discovered via
    search may only have ``source_url``. This fetches the body from that URL so
    the installed local Skill carries a real document instead of an empty body.

    Any failure (network, non-200, non-text, timeout) returns ``None`` — the
    caller (SK-12 endpoint) decides whether to abort the install or proceed with
    an empty body. Never raises: remote is best-effort, never a hard dependency.
    """
    url = (entry.source_url or "").strip()
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_REMOTE_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("[skill_hub] install fetch %s returned %s", url, resp.status_code)
                return None
            # 技能文档是文本，二进制响应无意义；is_text_content 粗判后 decode
            ctype = resp.headers.get("content-type", "").lower()
            body = resp.content
            if len(body) > _MAX_INSTALL_BYTES:
                logger.warning("[skill_hub] install fetch %s too large: %d bytes", url, len(body))
                return None
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.warning("[skill_hub] install fetch %s non-utf8: %s", url, exc)
                return None
            if "text/" not in ctype and "json" not in ctype and "markdown" not in ctype and "charset" not in ctype:
                # 非文本 content-type（如 image/octet-stream）不当作技能文档
                logger.warning("[skill_hub] install fetch %s non-text content-type %s", url, ctype)
                return None
            return text.strip() or None
    except Exception as exc:  # noqa: BLE001 — overlay must never raise
        logger.warning("[skill_hub] install fetch %s failed: %s", url, exc)
        return None


def list_hubs() -> list[dict[str, Any]]:
    """Introspection helper: which providers are active right now.

    Useful for the UI to badge "market: catalog (+ remote when configured)".
    """
    hubs: list[dict[str, Any]] = [{"id": "catalog", "name": "内置市场", "active": True}]
    remote_url = os.environ.get("SKILL_HUB_URL", "").strip()
    hubs.append(
        {
            "id": "remote",
            "name": "远程 Hub",
            "active": bool(remote_url),
            "url": remote_url or None,
        }
    )
    return hubs
