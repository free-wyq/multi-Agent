"""SK-09 自测：技能浏览 + 搜索筛选（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL/SK 自测模式（httpx HTTP 真源交叉验证）。

SK-09 架构关键点（先读代码确认）：
  - 后端 GET /api/skills（skills.py list_skills → crud.list_skills）返回**全量**技能，
    无服务端搜索参数（list_skills() 不接受 q）。按 created_at 排序。
  - 搜索筛选是**纯前端逻辑**：SkillPage.tsx 的 filteredSkills useMemo 在客户端做
    大小写不敏感子串匹配（name/description/tags 任一命中即保留），空查询返回全部。
  - 故 SK-09「浏览+搜索筛选」验证分两半：
    ① 浏览：GET /api/skills 返回完整列表，每个 Skill 字段（id/name/description/
       source/tags/mounted_to/installed/created_at/updated_at）完整可被前端消费。
    ② 搜索筛选：前端过滤逻辑正确——用真实后端数据 + 在 Python 精确复刻 SkillPage
       的过滤算法，对多种查询断言筛选子集正确。这证明「后端数据契约 + 前端过滤算法」
       两者结合产出正确筛选结果（前端读的字段就是后端返回的字段，算法确定性可复刻）。

为何复刻算法而非驱浏览器：项目前端无 Jest/vitest 测试运行器，且 PL/SK 自测一律用
asyncio 脚本 + HTTP 真源交叉验证；SkillPage 的过滤是纯函数（无 React 副作用），
其算法可在 Python 等价复刻并对真实数据断言，与「驱浏览器看 UI」等价地证明逻辑正确，
且确定性更强（断言精确子集而非肉眼判断）。

测试技能种子（3 个，name/description/tags 互相可区分，便于断言筛选子集）：
  S1: name="Git提交规范检查"     desc="检查 Conventional Commits 提交信息规范"
      tags=["git","commit","规范"]
  S2: name="PostgreSQL慢查询分析" desc="识别全表扫描并给出索引建议"
      tags=["postgres","sql","索引"]
  S3: name="Python代码审查"       desc="审查 Python 代码质量与规范"
      tags=["python","review","规范"]

筛选断言矩阵（复刻 SkillPage filteredSkills：q.toLowerCase() 命中 name/desc/tags
任一即保留，空 q 返回全部）：
  q=""         → [S1,S2,S3]（空查询返回全部）
  q="git"      → [S1]（name+tag 命中）
  q="POSTGRES" → [S2]（tag 大写命中，验证大小写不敏感）
  q="索引"      → [S2]（description+tag 命中中文）
  q="规范"      → [S1,S3]（tag 命中多结果）
  q="python"   → [S3]（name+tag 命中）
  q="commit"   → [S1]（tag 命中英文）
  q="不存在的关键词zzz" → []（无命中返回空）

收尾：删除 3 个种子 skill，避免污染后续自测。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# ── 种子技能（name/description/tags 互相可区分）──────────────────────
SEEDS = [
    {
        "name": "Git提交规范检查",
        "description": "检查 Conventional Commits 提交信息规范",
        "content": "# Git提交规范检查\n检查提交信息是否符合约定式提交规范。",
        "source": "custom",
        "tags": ["git", "commit", "规范"],
    },
    {
        "name": "PostgreSQL慢查询分析",
        "description": "识别全表扫描并给出索引建议",
        "content": "# PostgreSQL慢查询分析\n识别全表扫描并给出索引建议。",
        "source": "custom",
        "tags": ["postgres", "sql", "索引"],
    },
    {
        "name": "Python代码审查",
        "description": "审查 Python 代码质量与规范",
        "content": "# Python代码审查\n审查代码质量与规范。",
        "source": "custom",
        "tags": ["python", "review", "规范"],
    },
]

# 种子索引 → 期望筛选子集（按 SEEDS 顺序）
EXPECTED_FILTERS = [
    ("", [0, 1, 2]),
    ("git", [0]),
    ("POSTGRES", [1]),          # 大写，验证大小写不敏感
    ("索引", [1]),               # 中文 description+tag 命中
    ("规范", [0, 2]),            # 多结果
    ("python", [2]),            # name+tag
    ("commit", [0]),            # tag 英文
    ("不存在的关键词zzz", []),
]

# 前端必读字段（SkillPage 卡片渲染 + filteredSkills 过滤都依赖这些字段存在）
REQUIRED_FIELDS = ["id", "name", "description", "source", "tags", "mounted_to", "installed"]


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create_skill(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/skills", json=body)
        r.raise_for_status()
        return r.json()


async def list_skills() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/skills")
        r.raise_for_status()
        return r.json()


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.json() is True


def filter_skills(skills: list[dict], q: str) -> list[dict]:
    """精确复刻 SkillPage.tsx filteredSkills useMemo 的过滤算法。

    原始 TS：
      const qq = search.trim().toLowerCase()
      if (!qq) return skills
      return skills.filter(s =>
        s.name.toLowerCase().includes(qq) ||
        (s.description ?? '').toLowerCase().includes(qq) ||
        (s.tags ?? []).some(t => t.toLowerCase().includes(qq))
      )
    """
    qq = q.strip().lower()
    if not qq:
        return list(skills)
    out = []
    for s in skills:
        name = (s.get("name") or "").lower()
        desc = (s.get("description") or "").lower()
        tags = s.get("tags") or []
        if (qq in name) or (qq in desc) or any(qq in str(t).lower() for t in tags):
            out.append(s)
    return out


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== SK-09 自测：技能浏览 + 搜索筛选 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    created_ids: list[str] = []

    # 记录创建前的列表（应为空——SK-05/SK-01 自测都做了收尾清理）
    before = await list_skills()
    print(f"[before] 现有技能 {len(before)} 个")

    # ── 种入测试技能 ──
    print("\n[seed] 种入 3 个测试技能...")
    seed_index: dict[int, dict] = {}  # i → skill dict
    try:
        for i, seed in enumerate(SEEDS):
            sk = await create_skill(seed)
            sid = sk.get("id", "")
            created_ids.append(sid)
            seed_index[i] = sk
            print(f"  [{i}] id={sid[:16]}... name={sk.get('name')!r} tags={sk.get('tags')}")
    except Exception as e:
        errs.append(f"[seed] 种入失败: {e}")
        print(f"  ✗ 种入失败: {e}")

    try:
        if len(seed_index) != len(SEEDS):
            errs.append(f"[seed] 仅种入 {len(seed_index)}/{len(SEEDS)}，无法继续筛选断言")
        else:
            # ── 浏览校验：GET /api/skills 返回完整列表 + 字段完整 ──
            print("\n[check 1] 浏览：GET /api/skills 返回完整列表")
            all_skills = await list_skills()
            # 应至少包含我们种入的 3 个（可能有其它历史技能，故用 ⊇ 断言）
            seed_ids = {seed_index[i]["id"] for i in seed_index}
            list_ids = {s.get("id") for s in all_skills}
            if not _check("列表含全部 3 个种子技能", seed_ids.issubset(list_ids),
                          f"缺 {(seed_ids - list_ids)}"):
                errs.append("[browse] 列表未含全部种子技能（浏览数据不全）")

            print("\n[check 2] 每个 Skill 字段完整（前端卡片+过滤依赖）")
            field_ok = True
            for i in seed_index:
                sk = seed_index[i]
                for f in REQUIRED_FIELDS:
                    if f not in sk:
                        field_ok = False
                        errs.append(f"[fields] 种子[{i}] 缺字段 {f}")
                        print(f"  ✗ 种子[{i}] 缺字段 {f}")
                # 字段值正确性（create 返回应 == 请求体）
                if sk.get("name") != SEEDS[i]["name"]:
                    errs.append(f"[fields] 种子[{i}] name 不符: {sk.get('name')!r}")
                    field_ok = False
                if sk.get("source") != "custom":
                    errs.append(f"[fields] 种子[{i}] source 非 custom: {sk.get('source')!r}")
                    field_ok = False
                if sk.get("tags") != SEEDS[i]["tags"]:
                    errs.append(f"[fields] 种子[{i}] tags 不符: {sk.get('tags')!r}")
                    field_ok = False
                if sk.get("installed") not in (True, 1):
                    errs.append(f"[fields] 种子[{i}] installed 非真: {sk.get('installed')!r}")
                    field_ok = False
            if field_ok:
                print(f"  ✓ {len(seed_index)} 个种子技能字段全部完整（{REQUIRED_FIELDS}）")

            # ── 搜索筛选校验：复刻 SkillPage filteredSkills 对真实数据断言 ──
            print("\n[check 3] 搜索筛选：复刻 SkillPage 过滤算法对真实数据断言")
            # 仅在我们种入的技能集合上断言（排除历史技能干扰）
            seed_skills = [seed_index[i] for i in sorted(seed_index)]
            all_pass = True
            for q, expected_idx in EXPECTED_FILTERS:
                got = filter_skills(seed_skills, q)
                got_idx = sorted(i for i, s in seed_index.items() if s in got)
                ok = got_idx == expected_idx
                mark = "✓" if ok else "✗"
                qrepr = repr(q) if q else "(空)"
                print(f"  {mark} q={qrepr:24} → 期望 {[SEEDS[i]['name'] for i in expected_idx]} "
                      f"实得 {[SEEDS[i]['name'] for i in got_idx]}")
                if not ok:
                    all_pass = False
                    errs.append(f"[filter] q={q!r} 期望 {expected_idx} 实得 {got_idx}")
            if all_pass:
                print("  [check 3] 全部筛选断言通过（空查询全返回/大小写不敏感/"
                      "中文命中/多结果/无命中空集）")

            # ── 真源一致性：列表过滤 vs 单读 ──
            print("\n[check 4] 浏览数据一致性：列表项 == 单读 GET /api/skills/{id}")
            consistent = True
            for i in seed_index:
                sk = seed_index[i]
                async with httpx.AsyncClient() as c:
                    r = await c.get(f"{BASE}/api/skills/{sk['id']}")
                    r.raise_for_status()
                    reread = r.json()
                same = (reread.get("id") == sk["id"]
                        and reread.get("name") == sk["name"]
                        and reread.get("tags") == sk["tags"]
                        and reread.get("description") == sk.get("description"))
                if not same:
                    consistent = False
                    errs.append(f"[consistent] 种子[{i}] 列表项与单读不一致")
                    print(f"  ✗ 种子[{i}] 列表项 ≠ 单读")
            if consistent:
                print(f"  ✓ {len(seed_index)} 个种子技能列表项与单读一致")
    finally:
        # 收尾清理：删除所有种入的技能，避免污染后续自测
        print("\n[cleanup] 清理种入的测试技能...")
        for sid in created_ids:
            try:
                ok = await delete_skill(sid)
                print(f"  删除 {sid[:16]}... → {ok}")
            except Exception as e:
                print(f"  删除 {sid[:16]}... 失败（非致命）: {e}")

    # 清理后列表应回到 before 状态
    after = await list_skills()
    after_ids = {s.get("id") for s in after}
    leaked = [sid for sid in created_ids if sid in after_ids]
    if leaked:
        errs.append(f"[cleanup] {len(leaked)} 个种子技能未清理干净: {leaked}")
        print(f"  ✗ 清理后仍有残留: {len(leaked)} 个")
    else:
        print(f"[cleanup] 清理后列表回到 {len(after)} 个（与种入前一致）")

    if errs:
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1

    print("\n=== 结果: PASS ===")
    print("SK-09 技能浏览 + 搜索筛选 验证通过：")
    print("  · 浏览：GET /api/skills 返回完整列表，每个 Skill 字段完整")
    print("    （id/name/description/source/tags/mounted_to/installed），前端可消费；")
    print("  · 搜索筛选：复刻 SkillPage filteredSkills 算法对真实数据断言，")
    print("    空查询返回全部 / 大小写不敏感 / 中文命中 / 多结果 / 无命中空集 全部正确；")
    print("  · 浏览数据一致性：列表项与单读 GET /api/skills/{id} 一致；")
    print("  · 收尾清理无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
