"""AG-05 自测：员工列表展示名称/描述/技能/工具（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-09 自测模式（httpx HTTP 真源交叉验证）。

AG-05 架构关键点（先读代码确认）：
  - 后端 GET /api/agents（agents.py list_agents → crud.list_agents）返回**全量** agent，
    无服务端搜索参数。按 created_at 排序。
  - AgentPage.tsx 是**纯展示页**（无搜索过滤，不像 SK-09 有 filteredSkills），卡片直接渲染
    agents 数组的 name/description/role/skills/mounted_skills 字段。故 AG-05 验证重点是
    「后端返回的 AgentDefinition 字段是否完整可被前端卡片消费」——字段契约 + 渲染条件。
  - 前端卡片渲染逻辑（AgentPage.tsx）：
      · name → agent-card-name（必有）
      · role → agent-card-role（主题色 + 图标，getRoleTheme）
      · description → agent-card-desc（非空才渲染，2 行截断）
      · 技能 → allSkills = 去重(skills + extra_skills)（合并展示，非空显 Tag 否则「暂无技能」）
      · allowed_tools/denied_tools → 非空才显工具权限区
      · mounted_skills → 非空才显「已挂载」区（skillNameMap 映射 id→name）

为何不复刻前端渲染（像 SK-09 复刻 filteredSkills）：AG-05 是展示非过滤，前端逻辑是
「字段非空即渲染 Tag」的直白条件渲染，无算法可复刻断言；改为「后端字段契约完整 +
每个展示字段符合前端渲染条件（类型正确 + 值合理）」即等价证明「列表能展示名称/描述/技能/工具」。
即：前端读什么字段，后端就返回什么字段且类型/值能让卡片正确渲染，列表展示即成立。
这是与 SK-09「复刻过滤算法」等价的展示页验证方式（字段契约 + 渲染条件而非算法断言）。

验证五块（确定性断言）：
  ① 浏览：GET /api/agents 返回列表（含种子 agent），列表非空；
  ② 字段契约：每个 AgentDefinition 含前端卡片必读字段（id/name/role/description/skills/
     extra_skills/mounted_skills/allowed_tools/denied_tools）且类型正确；
  ③ 名称/描述：每个 agent 的 name 非空 str；description 为 str 或 null（前端 desc 非空才渲染）；
  ④ 技能展示：allSkills = 去重(skills+extra_skills) 至少有一个 agent 的合并技能非空
     （种子 frontend_engineer skills=React/TS/CSS + extra=Ant Design/ReactFlow → 5 个），
     验证「技能列表能展示」；同时校验去重逻辑（若 skills 与 extra 重叠不重复）；
  ⑤ 工具权限：allowed_tools/denied_tools 均为 list（当前种子为空，验证「空时前端不渲染工具区」
     的前提——字段存在且为 list，非空才渲染）；
  ⑥ 持久化一致：GET /api/agents/{id} 单读 == 列表项（id/name/role/description/skills 一致）。

为何不连 WS：AG-05 是同步 HTTP 接口（list_agents 直接查 DB 返回），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 SK-01/SK-09 同构，比 PL 系列更简单）。

为何不种测试 agent（像 SK-09 种 3 个技能）：种子已有 3 个 agent（coord/frontend/backend），
字段齐全且互相可区分（不同 role/skills/description），直接用种子断言即可，无需种入+清理。
若种子被外部修改（如删了某个 agent），脚本用「至少有 1 个 agent + 字段齐全」的弱断言兜底，
不依赖固定 3 个种子（避免环境漂移误判）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 前端 AgentPage 卡片必读字段（含 AG-05 新增的 allowed_tools/denied_tools）。
# 卡片渲染 + allSkills 合成都依赖这些字段存在且类型正确。
REQUIRED_FIELDS = [
    "id",
    "name",
    "role",
    "description",
    "skills",
    "extra_skills",
    "mounted_skills",
    "allowed_tools",
    "denied_tools",
]


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        r.raise_for_status()
        return r.json()


async def get_agent(agent_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents/{agent_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def merge_skills(skills: list, extra: list) -> list[str]:
    """精确复刻 AgentPage.tsx allSkills 合并逻辑。

    原始 TS：
      const allSkills = Array.from(
        new Set([...(agent.skills ?? []), ...(agent.extra_skills ?? [])]),
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


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-05 自测：员工列表展示名称/描述/技能/工具 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    agents = await list_agents()
    print(f"[list] GET /api/agents 返回 {len(agents)} 个 agent")

    # ── 1. 浏览：列表非空 ──
    print("\n[check 1] 浏览：GET /api/agents 返回非空列表")
    if not _check("列表非空（至少 1 个 agent）", len(agents) >= 1,
                  f"仅 {len(agents)} 个"):
        errs.append("[browse] agent 列表为空，无法验证展示")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    for a in agents:
        print(f"  · {a.get('id')} | {a.get('name')} | role={a.get('role')}")

    # ── 2. 字段契约：每个 AgentDefinition 含前端必读字段 + 类型正确 ──
    print(f"\n[check 2] 字段契约：每个 agent 含 {REQUIRED_FIELDS} 且类型正确")
    field_ok = True
    for a in agents:
        for f in REQUIRED_FIELDS:
            if f not in a:
                field_ok = False
                errs.append(f"[fields] agent {a.get('id')} 缺字段 {f}")
                print(f"  ✗ {a.get('id')} 缺字段 {f}")
        # 类型校验：列表字段必须是 list
        for f in ["skills", "extra_skills", "mounted_skills", "allowed_tools", "denied_tools"]:
            v = a.get(f)
            if v is not None and not isinstance(v, list):
                field_ok = False
                errs.append(f"[fields] agent {a.get('id')} {f} 非 list: {type(v)}")
        # name/role 必须是非空 str
        if not (isinstance(a.get("name"), str) and a.get("name")):
            field_ok = False
            errs.append(f"[fields] agent {a.get('id')} name 非非空str: {a.get('name')!r}")
        if not (isinstance(a.get("role"), str) and a.get("role")):
            field_ok = False
            errs.append(f"[fields] agent {a.get('id')} role 非非空str: {a.get('role')!r}")
        # description 必须是 str 或 None（前端 desc 非空才渲染）
        if a.get("description") is not None and not isinstance(a.get("description"), str):
            field_ok = False
            errs.append(f"[fields] agent {a.get('id')} description 非 str|None")
    if field_ok:
        print(f"  ✓ {len(agents)} 个 agent 字段全部齐全且类型正确")

    # ── 3. 名称/描述：每个 agent name 非空；至少一个有 description ──
    print("\n[check 3] 名称/描述展示")
    names_ok = all(isinstance(a.get("name"), str) and a.get("name") for a in agents)
    if _check("所有 agent name 非空 str", names_ok):
        pass
    else:
        errs.append("[name] 存在 name 为空的 agent")
    has_desc = any(isinstance(a.get("description"), str) and a.get("description") for a in agents)
    if _check("至少 1 个 agent 有 description（前端 desc 区能渲染）", has_desc):
        # 列出每个 agent 的 description 状态
        for a in agents:
            d = a.get("description")
            print(f"      {a.get('name')}: desc={'有(' + d[:20] + '…)' if d else '空(None)'}")
    else:
        errs.append("[desc] 所有 agent description 皆空，前端 desc 区无内容可展示")

    # ── 4. 技能展示：allSkills 合并非空（至少一个 agent 有技能）+ 去重正确 ──
    print("\n[check 4] 技能展示：allSkills = 去重(skills+extra_skills)")
    has_skills = False
    dedup_ok = True
    for a in agents:
        merged = merge_skills(a.get("skills"), a.get("extra_skills"))
        # 去重正确性：合并后长度 == Set 长度（无重复）
        if len(merged) != len(set(merged)):
            dedup_ok = False
            errs.append(f"[skills] agent {a.get('id')} 合并后有重复: {merged}")
        if merged:
            has_skills = True
            print(f"      {a.get('name')}: skills={a.get('skills')} + extra={a.get('extra_skills')} → {merged}")
    if _check("至少 1 个 agent 合并技能非空（技能列表能展示）", has_skills):
        pass
    else:
        errs.append("[skills] 所有 agent 合并技能皆空，前端技能区只能显「暂无技能」占位")
    if _check("合并去重正确（无重复元素）", dedup_ok):
        pass
    else:
        errs.append("[skills] 合并去重异常")

    # ── 5. 工具权限：allowed_tools/denied_tools 为 list（空时不渲染，非空才显） ──
    print("\n[check 5] 工具权限：allowed_tools/denied_tools 字段存在且为 list")
    tools_ok = all(
        isinstance(a.get("allowed_tools"), list) and isinstance(a.get("denied_tools"), list)
        for a in agents
    )
    if _check("所有 agent allowed_tools/denied_tools 均为 list", tools_ok):
        # 当前种子为空，前端空时不渲染工具区（符合设计）；非空才显。统计非空情况。
        nonempty = [
            a for a in agents
            if (a.get("allowed_tools") or a.get("denied_tools"))
        ]
        print(f"      当前 {len(nonempty)} 个 agent 有工具权限配置（种子为空属正常，"
              f"非空时前端渲染工具 Tag 区）")
    else:
        errs.append("[tools] allowed_tools/denied_tools 字段缺失或非 list，前端工具区无法正确渲染")

    # ── 6. 持久化一致：GET /api/agents/{id} 单读 == 列表项 ──
    print("\n[check 6] 浏览数据一致性：列表项 == 单读 GET /api/agents/{id}")
    consistent = True
    for a in agents:
        reread = await get_agent(a["id"])
        if reread is None:
            consistent = False
            errs.append(f"[consistent] agent {a['id']} 单读 404")
            continue
        same = (
            reread.get("id") == a.get("id")
            and reread.get("name") == a.get("name")
            and reread.get("role") == a.get("role")
            and reread.get("description") == a.get("description")
            and reread.get("skills") == a.get("skills")
            and reread.get("extra_skills") == a.get("extra_skills")
            and reread.get("mounted_skills") == a.get("mounted_skills")
            and reread.get("allowed_tools") == a.get("allowed_tools")
            and reread.get("denied_tools") == a.get("denied_tools")
        )
        if not same:
            consistent = False
            errs.append(f"[consistent] agent {a['id']} 列表项 ≠ 单读")
            print(f"  ✗ {a['id']} 列表项 ≠ 单读")
    if consistent:
        print(f"  ✓ {len(agents)} 个 agent 列表项与单读一致（id/name/role/description/"
              "skills/extra_skills/mounted_skills/allowed_tools/denied_tools）")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 员工列表展示名称/描述/技能/工具端到端验证通过：")
    print("  · 浏览：GET /api/agents 返回非空列表；")
    print("  · 字段契约：每个 AgentDefinition 含前端卡片必读字段（id/name/role/description/")
    print("    skills/extra_skills/mounted_skills/allowed_tools/denied_tools）且类型正确；")
    print("  · 名称/描述：name 非空 + 至少 1 个有 description（卡片 desc 区可渲染）；")
    print("  · 技能展示：allSkills=去重(skills+extra_skills) 非空 + 去重正确（技能 Tag 可展示）；")
    print("  · 工具权限：allowed_tools/denied_tools 为 list（空时不渲染非空才显，契约就绪）；")
    print("  · 持久化一致：列表项 == 单读 GET /api/agents/{id}。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
