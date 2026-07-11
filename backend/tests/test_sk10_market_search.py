"""SK-10 自测：搜索市场技能返回列表（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL/SK 自测模式（httpx HTTP 真源交叉验证）。

SK-10 架构关键点（先读代码确认）：
  - 后端 GET /api/skills/market?q=&limit=（skills.py search_market_skills →
    skill_hub.search_market）做**服务端搜索**：
      ① curated catalog 内置市场恒可用（10 条真实技能文档，无网络依赖）；
      ② remote Hub best-effort overlay（SKILL_HUB_URL 未设时 no-op，仅 catalog）；
      ③ 合并去重（按 entry_id）→ 过滤（_matches：name/description/tags 大小写不敏感
         子串匹配，空 q 返回全部）→ limit 封顶（默认 50，上限 200）。
  - 与 SK-09 本地搜索不同：SK-10 搜索在后端完成（含 remote overlay），前端只传 q。
  - 故 SK-10「搜索市场技能返回列表」验证分四块：
    ① 浏览：GET /api/skills/market（空 q）返回非空列表，每个 MarketEntry 字段
       （entry_id/name/description/tags/content/hub/author/version/source_url）完整
       且类型正确，可被前端 SkillMarketEntry 消费。
    ② 搜索筛选：后端 _matches 过滤语义正确——在 Python 精确复刻 skill_hub._matches
       算法，对多组查询断言「服务端返回的 entry_id 子集 == 本地复刻算法计算的子集」，
       交叉验证服务端过滤与文档约定一致（大小写不敏感/中文命中/多结果/无命中）。
    ③ limit 封顶 + 越界 422：limit=N 返回 ≤N 条；limit=0 / limit=201 触发 FastAPI
       Query(ge=1, le=200) 的 422 校验。
    ④ catalog provenance：catalog 条目 hub="catalog" + content 非空（供 SK-12 安装取
       全文）+ entry_id 形如 "catalog:<slug>"，前端可据此区分来源并支持预览/安装。

为何复刻算法而非驱浏览器：与 SK-09 同理——前端无 Jest/vitest，_matches 是纯函数
（无副作用，仅依赖 name/description/tags 做大小写不敏感子串匹配），其算法可在 Python
等价复刻并对真实后端数据断言。服务端返回的 entry_id 子集若与本地复刻不一致，即说明
服务端过滤有 bug（如误做大小写敏感、漏查 tags、中文匹配失败），交叉验证可确定性捕获。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# MarketEntry 前端必读字段（SkillMarketEntry interface + SkillPage 市场卡片渲染依赖）
# snake_case 对齐后端 MarketEntry 模型与前端 api.ts SkillMarketEntry 约定。
REQUIRED_FIELDS = [
    "entry_id",
    "name",
    "description",
    "tags",
    "content",
    "hub",
    "author",
    "version",
    "source_url",
]

# 搜索查询矩阵（q → 期望命中条目的判据由本地复刻 _matches 计算，这里只给查询串，
# 不硬编码期望子集——查询串覆盖空/英文小写/英文大写/中文/多结果/无命中各分支）。
QUERIES = [
    "",                  # 空查询 → 返回全部
    "review",            # tag 命中多结果（code-review + security-review）
    "REVIEW",            # 大写，验证大小写不敏感（应 == "review" 结果）
    "审查",               # 中文名命中多结果（代码审查 + 安全审查）
    "数据库",             # 中文名+tag 命中单结果（db-migration）
    "git",               # name+tag 命中单结果（git-workflow）
    "测试",               # 中文名多结果（性能测试 + 测试生成）
    "quality",           # tag 多结果（code-review + refactor）
    "api",               # name+tag 单结果（api-doc-gen）
    "zzz不存在的关键词",   # 无命中 → 空集
]


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def search_market(q: str = "", limit: int = 50) -> tuple[int, list[dict]]:
    """GET /api/skills/market?q=&limit=，返回 (status, entries)。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{BASE}/api/skills/market",
            params={"q": q, "limit": limit},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, []


def matches(entry: dict, q: str) -> bool:
    """精确复刻 skill_hub._matches 的过滤算法。

    原始 Python（skill_hub.py）：
        qq = q.strip().lower()
        if not qq: return True
        if qq in entry.name.lower(): return True
        if qq in (entry.description or "").lower(): return True
        return any(qq in str(t).lower() for t in entry.tags)
    """
    qq = q.strip().lower()
    if not qq:
        return True
    name = (entry.get("name") or "").lower()
    desc = (entry.get("description") or "").lower()
    tags = entry.get("tags") or []
    return (qq in name) or (qq in desc) or any(qq in str(t).lower() for t in tags)


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== SK-10 自测：搜索市场技能返回列表 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # ── 1. 浏览：空 q 返回非空 catalog 列表 + 字段完整 ──
    print("\n[check 1] 浏览：GET /api/skills/market（空 q）返回 catalog 列表")
    status, all_entries = await search_market("", 200)
    if not _check("HTTP 200", status == 200, f"status={status}"):
        errs.append("[browse] 非 200")
    else:
        if not _check("返回非空列表（catalog 恒可用）", len(all_entries) >= 10,
                      f"仅 {len(all_entries)} 条"):
            errs.append(f"[browse] 列表过少 {len(all_entries)}")
        # catalog 条目数封顶 200（_DEFAULT_LIMIT=50，但 limit=200 应允许全量 catalog）
        if not _check("返回类型是 list", isinstance(all_entries, list)):
            errs.append("[browse] 返回非 list")

        # 字段完整性 + 类型校验（每个 entry 都须 9 字段齐全）
        field_ok = True
        type_ok = True
        for i, e in enumerate(all_entries):
            missing = [f for f in REQUIRED_FIELDS if f not in e]
            if missing:
                field_ok = False
                errs.append(f"[browse] entry#{i} 缺字段 {missing}")
                break
            # 类型断言：name/entry_id/hub 必 str；tags 必 list；content 可 str|None；
            # source_url 可 str|None；description/author/version 必 str（catalog 非空）
            if not isinstance(e.get("entry_id"), str) or not e.get("entry_id"):
                type_ok = False
                errs.append(f"[browse] entry#{i} entry_id 非非空 str")
                break
            if not isinstance(e.get("name"), str) or not e.get("name"):
                type_ok = False
                errs.append(f"[browse] entry#{i} name 非非空 str")
                break
            if not isinstance(e.get("tags"), list):
                type_ok = False
                errs.append(f"[browse] entry#{i} tags 非 list")
                break
        _check("每条 entry 9 字段齐全", field_ok)
        _check("核心字段类型正确（entry_id/name str 非空、tags list）", type_ok)

    # ── 2. 搜索筛选矩阵：服务端过滤 == 本地复刻 _matches ──
    print("\n[check 2] 搜索筛选：服务端返回子集 == 本地复刻 _matches 计算")
    if all_entries:
        for q in QUERIES:
            status, got = await search_market(q, 200)
            if status != 200:
                errs.append(f"[search] q={q!r} status={status}")
                print(f"  ✗ q={q!r} status={status}")
                continue
            got_ids = {e.get("entry_id") for e in got}
            expected_ids = {
                e.get("entry_id") for e in all_entries if matches(e, q)
            }
            ok = got_ids == expected_ids
            mark = "✓" if ok else "✗"
            qlabel = q if q else "(空)"
            print(
                f"  {mark} q={qlabel!r:24s} → 服务端 {len(got_ids)} 条 / "
                f"期望 {len(expected_ids)} 条"
                + ("" if ok else f"  差集={got_ids ^ expected_ids}")
            )
            if not ok:
                errs.append(
                    f"[search] q={q!r} 子集不符：got={sorted(got_ids)} "
                    f"expected={sorted(expected_ids)}"
                )

    # ── 3. 大小写不敏感专项：REVIEW == review ──
    print("\n[check 3] 大小写不敏感：q=REVIEW 与 q=review 返回相同子集")
    _, lo = await search_market("review", 200)
    _, up = await search_market("REVIEW", 200)
    lo_ids = {e.get("entry_id") for e in lo}
    up_ids = {e.get("entry_id") for e in up}
    if not _check("REVIEW 与 review 子集相同", lo_ids == up_ids,
                  f"review={sorted(lo_ids)} REVIEW={sorted(up_ids)}"):
        errs.append("[case] REVIEW != review")

    # ── 4. limit 封顶 + 越界 422 ──
    print("\n[check 4] limit 封顶 + 越界 422")
    _, capped = await search_market("", 3)
    if _check("limit=3 返回 ≤3 条", len(capped) <= 3, f"返回 {len(capped)} 条"):
        if not _check("limit=3 返回 3 条（catalog ≥3）", len(capped) == 3):
            errs.append(f"[limit] limit=3 返回 {len(capped)} 非 3")
    else:
        errs.append(f"[limit] limit=3 返回 {len(capped)} 超 3")

    # limit=0 → 422（ge=1）
    async with httpx.AsyncClient() as c:
        r0 = await c.get(f"{BASE}/api/skills/market", params={"limit": 0})
    if _check("limit=0 → 422（ge=1 校验）", r0.status_code == 422,
              f"status={r0.status_code}"):
        pass
    else:
        errs.append(f"[limit] limit=0 status={r0.status_code} 非 422")

    # limit=201 → 422（le=200）
    async with httpx.AsyncClient() as c:
        r201 = await c.get(f"{BASE}/api/skills/market", params={"limit": 201})
    if _check("limit=201 → 422（le=200 校验）", r201.status_code == 422,
              f"status={r201.status_code}"):
        pass
    else:
        errs.append(f"[limit] limit=201 status={r201.status_code} 非 422")

    # ── 5. catalog provenance + content 非空（供 SK-12 安装取全文 + 前端预览）──
    print("\n[check 5] catalog provenance：hub/entry_id/content 字段")
    if all_entries:
        catalog_entries = [e for e in all_entries if e.get("hub") == "catalog"]
        all_catalog = len(catalog_entries) == len(all_entries)
        if _check("全部条目 hub=catalog（remote 未配置时仅 catalog）", all_catalog,
                  f"{len(catalog_entries)}/{len(all_entries)}"):
            pass
        else:
            errs.append("[provenance] 非 catalog 条目存在（remote 可能意外激活）")

        # entry_id 形如 "catalog:<slug>"
        id_ok = all(
            isinstance(e.get("entry_id"), str)
            and e.get("entry_id", "").startswith("catalog:")
            for e in catalog_entries
        )
        if not _check('entry_id 形如 "catalog:<slug>"', id_ok):
            errs.append("[provenance] entry_id 前缀异常")

        # content 非空（catalog 条目自带全文，供 SK-12 安装 + 前端预览 Modal）
        content_ok = all(
            isinstance(e.get("content"), str) and e.get("content")
            for e in catalog_entries
        )
        if not _check("catalog content 非空（供 SK-12 安装取全文）", content_ok):
            errs.append("[provenance] catalog content 存在空值")

        # version + author provenance（catalog 标 "WorkMate 内置市场" / v1.0）
        prov_ok = all(
            e.get("author") == "WorkMate 内置市场" and e.get("version") == "1.0"
            for e in catalog_entries
        )
        if not _check("catalog author/version provenance 一致", prov_ok):
            errs.append("[provenance] author/version 不一致")

    # ── 6. 单条查询验证字段可消费（前端 SkillMarketEntry 契约）──
    print("\n[check 6] 单条查询：MarketEntry 字段可被前端消费")
    _, single_q = await search_market("数据库", 200)
    if single_q:
        e = single_q[0]
        # 前端市场卡片需要：name（标题）、description（正文）、tags（标签）、hub（来源 Tag）、
        # content（预览 Modal）、entry_id（安装 key）、version（v 标签）、author/source_url（来源行）
        consumable = (
            isinstance(e.get("name"), str)
            and isinstance(e.get("description"), str)
            and isinstance(e.get("tags"), list)
            and isinstance(e.get("hub"), str)
            and isinstance(e.get("content"), str)
            and isinstance(e.get("entry_id"), str)
            and isinstance(e.get("version"), str)
        )
        if _check("市场卡片字段契约完整", consumable):
            print(
                f"      样本：{e.get('name')!r} hub={e.get('hub')!r} "
                f"v{e.get('version')} content={len(e.get('content') or '')}字"
            )
        else:
            errs.append("[consume] 字段契约不完整")
    else:
        errs.append("[consume] 数据库 查询无结果")
        print("  ✗ 数据库 查询无结果")

    # ── 汇总 ──
    print("\n" + "=" * 52)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 市场搜索返回列表、筛选语义、limit 校验、provenance 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
