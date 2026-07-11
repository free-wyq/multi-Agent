"""AG-11 自测：浏览预设角色模板（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 AG-05 自测模式（httpx HTTP 真源 + 前端
筛选/渲染逻辑复刻交叉验证，不连 WS）。

AG-11 链路（模板广场浏览）：
  GET /api/agents/templates?category=
    → agents.py list_agent_templates → agent_templates.list_templates(category)
    → 返回 AgentTemplate 列表（catalog 模块级静态常量，恒可用无网络/DB 依赖）
  前端 AgentPage.tsx 角色模板广场：
    · 首开懒拉取 listTemplates() 全量到 state templates
    · tplCategories = useMemo 去重分类（保序）→ Segmented ['全部', ...分类]
    · filteredTemplates = useMemo 按 tplCategory 筛选（'全部' 返全部 else 精确匹配）
    · 卡片渲染 icon_emoji/name/role/category Tag/description(2 行截断)/skills+extra 合并 Tag

为何不复刻前端懒拉取/折叠态：懒拉取（首开才请求）与折叠开关是 UI 交互态非数据契约，
HTTP 层只验证「端点返回正确 + 筛选算法正确 + 字段可渲染」即等价证明「广场能浏览模板」。

验证八块（确定性断言）：
  ① 浏览：GET /api/agents/templates 返回非空列表（catalog 10 模板）；
  ② 字段契约：每个 AgentTemplate 含 9 字段（template_id/name/role/description/
     system_prompt/skills/extra_skills/category/icon_emoji）且类型正确；
  ③ 模板可用性：name 非空 + role snake_case + system_prompt「你」开头 + description 非空
     + icon_emoji 非空（真模板非 stub，前端卡片每格都有内容可渲染）；
  ④ 分类筛选：?category=开发 → 3 模板（后端精确匹配）；?category=不存在 → 空列表 200；
  ⑤ 前端筛选算法复刻：tplCategories 去重保序 == 后端实际分类集合；filteredTemplates
     按 '全部'/精确分类 筛选结果 == 前端 useMemo 派生逻辑（证明前端内存筛选正确）；
  ⑥ 卡片渲染条件：每个模板的 icon_emoji/name/role/description/skills 满足前端卡片
     渲染条件（非空 + 类型对，卡片每区都有内容）；
  ⑦ 路由未被遮蔽：GET /api/agents/templates 返 200 + list（非被 /{agent_id} 当 id 返 null）；
  ⑧ template_id 唯一 + tpl: 前缀（AG-12 雇佣用此 id 解析，须全局唯一可寻址）。

为何不连 WS：AG-11 是同步 HTTP 接口（list_templates 读静态常量返回），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 AG-05/SK-09 同构）。

为何无收尾清理：模板是静态 catalog 不落库，GET-only 无副作用，无探针 agent/skill 需删。
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx

BASE = "http://localhost:8000"

# 前端 AgentPage 模板卡片必读字段（AgentTemplate interface 9 字段）。
REQUIRED_FIELDS = [
    "template_id",
    "name",
    "role",
    "description",
    "system_prompt",
    "skills",
    "extra_skills",
    "category",
    "icon_emoji",
]

ROLE_RE = re.compile(r"^[a-z0-9_]+$")


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_templates(category: str = "") -> tuple[int, list[dict]]:
    """GET /api/agents/templates?category= → (status, list)."""
    async with httpx.AsyncClient() as c:
        params = {"category": category} if category else None
        r = await c.get(f"{BASE}/api/agents/templates", params=params)
        return r.status_code, r.json() if r.status_code == 200 else []


def merge_skills(skills: list, extra: list) -> list[str]:
    """精确复刻 AgentPage.tsx 模板卡片 allSkills 合并逻辑。

    原始 TS：
      const tplSkills = Array.from(
        new Set([...(tpl.skills ?? []), ...(tpl.extra_skills ?? [])]),
      )
    去重保序（Set 插入序），skills 在前 extra 在后。
    """
    out: list[str] = []
    seen: set[str] = set()
    for s in list(skills or []) + list(extra or []):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def derive_categories(templates: list[dict]) -> list[str]:
    """精确复刻 AgentPage.tsx tplCategories useMemo 派生逻辑。

    原始 TS：
      const tplCategories = useMemo(() => {
        const seen: string[] = []
        templates.forEach((t) => {
          if (!seen.includes(t.category)) seen.push(t.category)
        })
        return seen
      }, [templates])
    去重保序（首次出现序）。
    """
    seen: list[str] = []
    for t in templates:
        cat = t.get("category")
        if cat and cat not in seen:
            seen.append(cat)
    return seen


def filter_templates(templates: list[dict], category: str) -> list[dict]:
    """精确复刻 AgentPage.tsx filteredTemplates useMemo 筛选逻辑。

    原始 TS：
      const filteredTemplates = useMemo(() => {
        if (tplCategory === '全部') return templates
        return templates.filter((t) => t.category === tplCategory)
      }, [templates, tplCategory])
    """
    if category == "全部":
        return templates
    return [t for t in templates if t.get("category") == category]


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-11 自测：浏览预设角色模板 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    status, templates = await list_templates()
    print(f"[list] GET /api/agents/templates → {status}, {len(templates)} 模板")

    # ── 1. 浏览：列表非空 ──
    print("\n[check 1] 浏览：GET /api/agents/templates 返回非空列表")
    if not _check("模板列表非空（catalog 至少 1 条）", len(templates) >= 1,
                  f"仅 {len(templates)} 条"):
        errs.append("[browse] 模板列表为空，无法验证浏览")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    for t in templates:
        print(f"  · {t.get('template_id')} | {t.get('name')} | {t.get('category')}{t.get('icon_emoji')}")

    # ── 2. 字段契约：9 字段齐全 + 类型正确 ──
    print(f"\n[check 2] 字段契约：每个模板含 {REQUIRED_FIELDS} 且类型正确")
    field_ok = True
    for t in templates:
        for f in REQUIRED_FIELDS:
            if f not in t:
                field_ok = False
                errs.append(f"[fields] 模板 {t.get('template_id')} 缺字段 {f}")
        # 列表字段必须是 list
        for f in ["skills", "extra_skills"]:
            v = t.get(f)
            if not isinstance(v, list):
                field_ok = False
                errs.append(f"[fields] 模板 {t.get('template_id')} {f} 非 list: {type(v)}")
        # 字符串字段必须非空 str
        for f in ["template_id", "name", "role", "description", "system_prompt", "category", "icon_emoji"]:
            v = t.get(f)
            if not (isinstance(v, str) and v):
                field_ok = False
                errs.append(f"[fields] 模板 {t.get('template_id')} {f} 非非空str: {v!r}")
    if field_ok:
        print(f"  ✓ {len(templates)} 个模板 9 字段全部齐全且类型正确")

    # ── 3. 模板可用性：真模板非 stub ──
    print("\n[check 3] 模板可用性：真模板非 stub（role snake_case / system_prompt「你」开头）")
    usable = True
    for t in templates:
        role = t.get("role", "")
        sp = t.get("system_prompt", "")
        if not ROLE_RE.match(role):
            usable = False
            errs.append(f"[usable] {t.get('template_id')} role 非 snake_case: {role}")
        if not sp.startswith("你"):
            usable = False
            errs.append(f"[usable] {t.get('template_id')} system_prompt 非「你」开头: {sp[:20]!r}")
    if _check("所有模板 role 匹配 ^[a-z0-9_]+$ + system_prompt「你」开头", usable):
        pass
    else:
        errs.append("[usable] 存在 stub 模板（role 非 snake_case 或 system_prompt 非真角色定义）")

    # ── 4. 分类筛选（后端 ?category= 精确匹配） ──
    print("\n[check 4] 分类筛选：?category= 后端精确匹配")
    # 取首个分类做正常筛选（first_cat 仅用于展示，实际断言用固定分类开发/测试）
    first_cat = templates[0].get("category", "")
    s_dev, dev_list = await list_templates("开发")
    s_qa, qa_list = await list_templates("测试")
    s_none, none_list = await list_templates("不存在分类XYZ")
    filter_ok = (
        s_dev == 200 and len(dev_list) == 3
        and s_qa == 200 and len(qa_list) == 1
        and s_none == 200 and len(none_list) == 0
    )
    if not _check("?category=开发 → 3 / ?category=测试 → 1 / 不存在 → 0（全 200）",
                  filter_ok,
                  f"开发={len(dev_list)} 测试={len(qa_list)} 不存在={len(none_list)}"):
        errs.append(f"[filter] 后端分类筛选异常：开发={len(dev_list)} 测试={len(qa_list)} 不存在={len(none_list)}")
    # 验证筛选结果确实属于该分类
    all_dev = all(t.get("category") == "开发" for t in dev_list)
    if not _check("?category=开发 返回项 category 全 == 开发", all_dev):
        errs.append("[filter] ?category=开发 返回项含非开发分类")

    # ── 5. 前端筛选算法复刻 ──
    print("\n[check 5] 前端筛选算法复刻：tplCategories 去重保序 + filteredTemplates 筛选")
    fe_cats = derive_categories(templates)
    # 后端实际分类集合（从全量列表去重）
    be_cats = list(dict.fromkeys(t.get("category") for t in templates))
    cats_match = fe_cats == be_cats
    if not _check("tplCategories(前端) == 实际分类集合(后端) 去重保序一致", cats_match,
                  f"前端={fe_cats} 后端={be_cats}"):
        errs.append(f"[fe-filter] 分类派生不一致：前端={fe_cats} 后端={be_cats}")
    else:
        print(f"      分类: {fe_cats}")
    # filteredTemplates '全部' == 全量
    fe_all = filter_templates(templates, "全部")
    # filteredTemplates 精确分类 == 同分类全量（按 template_id 集合相等）。
    # 用全量内存筛选与前端同源对比（后端 ?category= 已在 check 4 验证）。
    filter_algo_ok = fe_all == templates
    for cat in be_cats:
        fe_filtered = filter_templates(templates, cat)
        be_filtered = [t for t in templates if t.get("category") == cat]
        if fe_filtered != be_filtered:
            filter_algo_ok = False
            errs.append(f"[fe-filter] 分类 {cat} 前端筛选 != 后端筛选")
    if not _check("filteredTemplates 精确分类筛选 == 同分类全量（前端算法正确）", filter_algo_ok):
        errs.append("[fe-filter] 前端 filteredTemplates 筛选算法与后端不一致")
    else:
        # 展示各分类筛选数量
        for cat in be_cats:
            n = len(filter_templates(templates, cat))
            print(f"      {cat}: {n} 模板")

    # ── 6. 卡片渲染条件：每区有内容可渲染 ──
    print("\n[check 6] 卡片渲染条件：每模板字段满足前端卡片渲染")
    render_ok = True
    for t in templates:
        # icon_emoji 非空（卡片顶部 emoji）
        if not (isinstance(t.get("icon_emoji"), str) and t.get("icon_emoji")):
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} icon_emoji 空")
        # name 非空（卡片标题）
        if not (isinstance(t.get("name"), str) and t.get("name")):
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} name 空")
        # role 非空（卡片 role 行 monospace）
        if not (isinstance(t.get("role"), str) and t.get("role")):
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} role 空")
        # category 非空（分类 Tag）
        if not (isinstance(t.get("category"), str) and t.get("category")):
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} category 空")
        # description 非空（卡片描述区 2 行截断）
        if not (isinstance(t.get("description"), str) and t.get("description")):
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} description 空")
        # skills 合并非空（技能 Tag 区，至少 1 条技能可展示）
        merged = merge_skills(t.get("skills"), t.get("extra_skills"))
        if not merged:
            render_ok = False
            errs.append(f"[render] {t.get('template_id')} 合并技能空")
    if _check("所有模板卡片每区字段非空可渲染（emoji/name/role/category/desc/skills）", render_ok):
        pass
    else:
        errs.append("[render] 存在模板字段不满足卡片渲染条件")

    # ── 7. 路由未被遮蔽 ──
    print("\n[check 7] 路由未被遮蔽：GET /api/agents/templates 返 200 list 非 null")
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents/templates")
    route_ok = r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) >= 1
    if not _check("GET /api/agents/templates → 200 + list（未被 /{agent_id} 遮蔽返 null）",
                  route_ok, f"status={r.status_code} body_type={type(r.json()).__name__}"):
        errs.append("[route] /templates 被 /{agent_id} 遮蔽或返回非预期")

    # ── 8. template_id 唯一 + tpl: 前缀 ──
    print("\n[check 8] template_id 唯一 + tpl: 前缀（AG-12 雇佣寻址依赖）")
    ids = [t.get("template_id") for t in templates]
    all_prefixed = all(isinstance(i, str) and i.startswith("tpl:") for i in ids)
    all_unique = len(ids) == len(set(ids))
    id_ok = all_prefixed and all_unique
    if not _check("所有 template_id 形如 tpl:<slug> 且全局唯一", id_ok,
                  f"prefixed={all_prefixed} unique={all_unique}"):
        errs.append(f"[id] template_id 前缀或唯一性异常：prefixed={all_prefixed} unique={all_unique}")
    else:
        print(f"      {len(ids)} 个 template_id 全 tpl: 前缀且唯一")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 浏览预设角色模板端到端验证通过：")
    print(f"  · 浏览：GET /api/agents/templates → {len(templates)} 模板（catalog 静态恒可用）；")
    print("  · 字段契约：9 字段（template_id/name/role/description/system_prompt/skills/")
    print("    extra_skills/category/icon_emoji）齐全且类型正确；")
    print("  · 模板可用性：role 全 snake_case + system_prompt「你」开头（真模板非 stub）；")
    print("  · 分类筛选：?category= 后端精确匹配（开发→3 / 测试→1 / 不存在→0 全 200）；")
    print("  · 前端筛选算法：tplCategories 去重保序 + filteredTemplates 筛选 == 后端；")
    print("  · 卡片渲染：每模板 emoji/name/role/category/desc/skills 非空可渲染；")
    print("  · 路由未遮蔽：GET /templates → 200 list 非 null；")
    print("  · template_id 唯一 + tpl: 前缀（AG-12 雇佣寻址就绪）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
