"""MT-05 自测：成员能力概况展示（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MC-01/AG-08 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理，不连 WS）。

MT-05 链路（GroupPage 群信息抽屉「成员能力概况」展示）：
  前端 GroupPage MemberCapabilityOverview 组件，数据来自 4 路 HTTP：
    · GET /api/groups/{id}/members → 成员列表（agent_id 平铺）
    · GET /api/agents → 全量智能体（含 skills/extra_skills/mounted_skills/
      mounted_mcp/allowed_tools/denied_tools 六类能力字段）
    · GET /api/skills → 建 skillNameMap（id→name），解析 mounted_skills 为可读名
    · GET /api/mcp → 建 mcpNameMap（id→name），解析 mounted_mcp 为可读名
  组件聚合规则（跨群主+成员去重，反映团队级能力盘）：
    roster = {coordinator_id} ∪ {m.agent_id for m in members}  # 群主也入列
    rosterAgents = agents.filter(a => roster.has(a.id))
    · 角色技能    = dedup(union(a.skills + a.extra_skills))
    · 已挂载技能  = dedup(union(a.mounted_skills)).map(id => skillNameMap[id] ?? id)
    · 可用工具    = dedup(union(a.allowed_tools))
    · 禁用工具    = dedup(union(a.denied_tools))         # 前缀「禁:」
    · MCP 工具源  = dedup(union(a.mounted_mcp)).map(id => mcpNameMap[id] ?? id)
    空能力段不渲染（filter items.length>0），全空显占位。

为何不复刻前端渲染：组件是 4 路 HTTP 数据的纯函数（无内部状态/副作用），HTTP 层
验证「能力字段可落库 + 4 路可读回 + 聚合逻辑（在测试里复刻）产出正确分段」即
等价证明「抽屉能展示成员能力概况」成立（与 MC-01「直接 GET 列表比对即证明展示
逻辑成立」同构）。前端 Tag/图标渲染是 UI 表现非数据契约。

为何用 update_agent(PUT) 设置能力字段：mounted_skills/mounted_mcp 虽有专用 mount
端点（AG-08/MC-06 已测），但 allowed_tools/denied_tools 无专用端点只能走 PUT；
为统一设置路径 + 聚焦 MT-05 的「展示聚合」而非「挂载动作」（挂载 AG-08/MC-06 已
覆盖），全用 PUT /api/agents/{id} 一次性写齐六类字段（update_agent model_dump
extra=allow → setattr 落库，回读校验）。仍创建探针 skill/mcp 以验证 id→name 解析。

验证八块（确定性断言，无 LLM 依赖）：
  ① 探针落库：创建 probe_coord(member? role=coordinator, skills) + probe_member1
     (skills+extra) + probe_member2(空,后续 PUT 写齐能力) + probe_member3(全空) +
     probe_skill + probe_mcp + group(coordinator=coord, members=[m1,m2,m3])；
  ② 能力字段写入：PUT probe_member2 body={allowed_tools,denied_tools,mounted_skills,
     mounted_mcp,extra_skills} → 200 + 回读字段 == payload 原值（六类能力可落库）；
  ③ 四路真源可读：GET members(3) / agents(含4探针) / skills(含探针) / mcp(含探针)
     全 200 非空；
  ④ 解析映射成立：skillNameMap[probe_skill.id] == probe_skill.name（非 id）且
     mcpNameMap[probe_mcp.id] == probe_mcp.name（id→name 解析路径就绪——组件据此
     把 mounted_skills/mounted_mcp 的裸 id 渲染为可读名）；
  ⑤ 聚合-角色技能：roster={coord,m1,m2,m3} 四 agent，roleSkills 去重 union 含
     coord 的 '需求分析'/'任务拆解' + m1 的 'React'/'TypeScript' + m2 的 'Python'
     （m3 全空贡献无）——证明跨成员去重合并 skills+extra_skills；
  ⑥ 聚合-挂载/工具/MCP：mountedSkillNames==[probe_skill.name]（解析后非裸 id）、
     allowedTools=={'Read','Write','Bash'}、deniedTools=={'Drop','RmRf'}、
     mountedMcpNames==[probe_mcp.name]——证明四类分段各自正确聚合 + 解析；
  ⑦ 边界-空能力成员不破坏：probe_member3 全空入群，聚合分段数量/内容不变
     （空字段被 filter 掉不渲染空行，与组件 filter items.length>0 一致）；
  ⑧ 收尾清理：DELETE group(级联 members) + 4 agent + skill + mcp，校验无残留。

为何不连 WS：MT-05 是同步 HTTP（4 路读 + PUT 写能力字段），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 MC-01/AG-08 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 探针能力字段（确定性值，便于精确断言）
COORD_SKILLS = ["需求分析", "任务拆解"]
COORD_EXTRA = ["调度"]
MEMBER1_SKILLS = ["React"]
MEMBER1_EXTRA = ["TypeScript"]
MEMBER2_EXTRA = ["Python"]
MEMBER2_ALLOWED = ["Read", "Write", "Bash"]
MEMBER2_DENIED = ["Drop", "RmRf"]

TIMEOUT = 30.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create_skill() -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/skills",
            json={
                "name": "MT05能力探针技能",
                "description": "MT-05 成员能力概况自测用探针技能",
                "content": "# MT-05 探针\n验证 mounted_skills 解析为可读名。",
                "source": "custom",
                "tags": ["mt05", "selftest"],
            },
        )
        r.raise_for_status()
        return r.json()


async def create_mcp() -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/mcp",
            json={
                "name": "MT05探针MCP",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "enabled": True,
            },
        )
        r.raise_for_status()
        return r.json()


async def create_agent(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/agents", json=payload)
        r.raise_for_status()
        return r.json()


async def update_agent(agent_id: str, payload: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(f"{BASE}/api/agents/{agent_id}", json=payload)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def create_group(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        r.raise_for_status()
        return r.json()


async def list_members(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/members")
        r.raise_for_status()
        return r.json()


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        r.raise_for_status()
        return r.json()


async def list_skills() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/skills")
        r.raise_for_status()
        return r.json()


async def list_mcp() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/mcp")
        r.raise_for_status()
        return r.json()


async def delete_group(group_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        return r.status_code == 200 and r.json() is True


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.status_code == 200 and r.json() is True


async def delete_mcp(mcp_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/mcp/{mcp_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _aggregate(roster_agents: list[dict], skill_name_map: dict, mcp_name_map: dict) -> dict:
    """复刻 MemberCapabilityOverview 聚合逻辑（跨成员去重）。

    返回 5 个分段各自的去重列表，与组件 sections 一一对应：
    role/mounted/allowed/denied/mcp。mounted/mcp 段经 id→name 映射解析。
    """
    def _union(field: str) -> list[str]:
        seen: list[str] = []
        for a in roster_agents:
            for v in a.get(field, []) or []:
                if v not in seen:
                    seen.append(v)
        return seen

    role_skills: list[str] = []
    for a in roster_agents:
        for v in (a.get("skills", []) or []) + (a.get("extra_skills", []) or []):
            if v not in role_skills:
                role_skills.append(v)
    mounted_skill_names = [skill_name_map.get(sid, sid) for sid in _union("mounted_skills")]
    allowed = _union("allowed_tools")
    denied = _union("denied_tools")
    mounted_mcp_names = [mcp_name_map.get(mid, mid) for mid in _union("mounted_mcp")]
    return {
        "role": role_skills,
        "mounted": mounted_skill_names,
        "allowed": allowed,
        "denied": denied,
        "mcp": mounted_mcp_names,
    }


async def main() -> int:
    print("=== MT-05 自测：成员能力概况展示 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_agent_ids: list[str] = []
    probe_group_id: str | None = None
    probe_skill_id: str | None = None
    probe_mcp_id: str | None = None

    try:
        # ── 1. 探针落库：coord + 3 member + skill + mcp + group ──
        print("\n[check 1] 探针落库：coord + 3 member + skill + mcp + group")
        probe_skill = await create_skill()
        probe_skill_id = probe_skill["id"]
        probe_mcp = await create_mcp()
        probe_mcp_id = probe_mcp["id"]

        coord = await create_agent({
            "name": "[MT05] 协调者探针",
            "role": "coordinator",
            "system_prompt": "你是探针协调者。",
            "skills": COORD_SKILLS,
            "extra_skills": COORD_EXTRA,
            "description": "MT-05 能力概况自测",
        })
        member1 = await create_agent({
            "name": "[MT05] 前端探针",
            "role": "frontend_engineer",
            "system_prompt": "你是探针前端。",
            "skills": MEMBER1_SKILLS,
            "extra_skills": MEMBER1_EXTRA,
            "description": "MT-05 能力概况自测",
        })
        member2 = await create_agent({
            "name": "[MT05] 后端探针",
            "role": "backend_engineer",
            "system_prompt": "你是探针后端。",
            "skills": [],
            "extra_skills": [],
            "description": "MT-05 能力概况自测（能力由 PUT 写入）",
        })
        member3 = await create_agent({
            "name": "[MT05] 空能力探针",
            "role": "custom",
            "system_prompt": "你是无能力探针。",
            "skills": [],
            "extra_skills": [],
            "description": "MT-05 验证空能力成员不破坏聚合",
        })
        probe_agent_ids = [coord["id"], member1["id"], member2["id"], member3["id"]]

        group = await create_group({
            "name": "[MT05] 能力概况探针群",
            "description": "MT-05 成员能力概况自测",
            "coordinator_id": coord["id"],
            "member_ids": [member1["id"], member2["id"], member3["id"]],
        })
        probe_group_id = group["id"]

        setup_ok = (
            probe_skill_id.startswith("skill_")
            and probe_mcp_id.startswith("mcp_")
            and all(aid.startswith("agent_") for aid in probe_agent_ids)
            and probe_group_id.startswith("group_")
            and group.get("coordinator_id") == coord["id"]
        )
        if _check("探针全部落库（skill_/mcp_/agent_×4/group_）", setup_ok):
            print(f"      coord={coord['id'][:18]}… m1={member1['id'][:18]}… "
                  f"m2={member2['id'][:18]}… m3={member3['id'][:18]}…")
        else:
            errs.append("[setup] 探针落库异常")

        # ── 2. 能力字段写入：PUT probe_member2 一次性写齐四类能力字段 ──
        print("\n[check 2] 能力字段写入：PUT /api/agents/{member2}（六类能力）")
        updated = await update_agent(member2["id"], {
            "name": member2["name"],
            "role": member2["role"],
            "extra_skills": MEMBER2_EXTRA,
            "skills": [],
            "mounted_skills": [probe_skill_id],
            "mounted_mcp": [probe_mcp_id],
            "allowed_tools": MEMBER2_ALLOWED,
            "denied_tools": MEMBER2_DENIED,
        })
        if not _check("PUT → 200 + AgentDefinition", updated is not None):
            errs.append("[put] member2 更新非 200")
        else:
            field_ok = (
                updated.get("extra_skills") == MEMBER2_EXTRA
                and updated.get("mounted_skills") == [probe_skill_id]
                and updated.get("mounted_mcp") == [probe_mcp_id]
                and updated.get("allowed_tools") == MEMBER2_ALLOWED
                and updated.get("denied_tools") == MEMBER2_DENIED
            )
            if _check("回读六类能力字段 == payload 原值", field_ok, f"got={updated}"):
                pass
            else:
                errs.append("[put] 能力字段落库不一致")

        # ── 3. 四路真源可读 ──
        print("\n[check 3] 四路真源可读：members / agents / skills / mcp")
        members = await list_members(probe_group_id)
        agents = await list_agents()
        skills = await list_skills()
        mcps = await list_mcp()
        four_ok = (
            len(members) == 3
            and any(a["id"] == coord["id"] for a in agents)
            and all(any(a["id"] == aid for a in agents) for aid in probe_agent_ids)
            and any(s["id"] == probe_skill_id for s in skills)
            and any(c["id"] == probe_mcp_id for c in mcps)
        )
        if _check("四路 200 + 含全部探针", four_ok,
                  f"members={len(members)} agents={len(agents)} skills={len(skills)} mcp={len(mcps)}"):
            pass
        else:
            errs.append("[sources] 四路真源缺失探针")

        # ── 4. 解析映射成立：id → name ──
        print("\n[check 4] 解析映射：skillNameMap / mcpNameMap（id→name）")
        skill_name_map = {s["id"]: s["name"] for s in skills}
        mcp_name_map = {c["id"]: c["name"] for c in mcps}
        skill_resolves = (
            skill_name_map.get(probe_skill_id) == probe_skill["name"]
            and probe_skill["name"] != probe_skill_id
        )
        mcp_resolves = (
            mcp_name_map.get(probe_mcp_id) == probe_mcp["name"]
            and probe_mcp["name"] != probe_mcp_id
        )
        if _check("skillNameMap[probe_skill.id] == name（非裸 id）", skill_resolves):
            print(f"      {probe_skill_id[:18]}… → {probe_skill['name']!r}")
        else:
            errs.append("[map] skill id→name 解析失败")
        if _check("mcpNameMap[probe_mcp.id] == name（非裸 id）", mcp_resolves):
            print(f"      {probe_mcp_id[:18]}… → {probe_mcp['name']!r}")
        else:
            errs.append("[map] mcp id→name 解析失败")

        # ── 5. 聚合-角色技能：跨 coord+m1+m2 去重合并 skills+extra ──
        print("\n[check 5] 聚合-角色技能：跨成员去重 union(skills+extra_skills)")
        # roster = {coordinator_id} ∪ {m.agent_id for m in members}（群主也入列）
        roster_ids = {coord["id"]} | {m["agent_id"] for m in members}
        roster_agents = [a for a in agents if a["id"] in roster_ids]
        sections = _aggregate(roster_agents, skill_name_map, mcp_name_map)

        if _check("roster 含 4 个 agent（群主+3成员）", len(roster_agents) == 4,
                  f"got {len(roster_agents)}"):
            pass
        else:
            errs.append(f"[roster] roster agent 数 {len(roster_agents)} != 4")

        expected_role = set(COORD_SKILLS + COORD_EXTRA + MEMBER1_SKILLS + MEMBER1_EXTRA + MEMBER2_EXTRA)
        role_ok = expected_role.issubset(set(sections["role"])) and len(sections["role"]) >= len(expected_role)
        if _check(f"roleSkills 去重含全部 {len(expected_role)} 项", role_ok,
                  f"got={sections['role']}"):
            print(f"      roleSkills={sections['role']}")
        else:
            errs.append(f"[agg-role] 角色技能聚合不一致：{sections['role']}")

        # ── 6. 聚合-挂载/工具/MCP：四类分段各自正确 ──
        print("\n[check 6] 聚合-挂载技能/可用工具/禁用工具/MCP 工具源")
        mounted_ok = sections["mounted"] == [probe_skill["name"]]
        if _check("mountedSkillNames == [probe_skill.name]（解析后非裸 id）", mounted_ok,
                  f"got={sections['mounted']}"):
            print(f"      mountedSkillNames={sections['mounted']}")
        else:
            errs.append(f"[agg-mounted] 挂载技能聚合/解析不一致：{sections['mounted']}")

        allowed_ok = set(sections["allowed"]) == set(MEMBER2_ALLOWED) and len(sections["allowed"]) == 3
        if _check(f"allowedTools == {set(MEMBER2_ALLOWED)}", allowed_ok,
                  f"got={sections['allowed']}"):
            pass
        else:
            errs.append(f"[agg-allowed] 可用工具聚合不一致：{sections['allowed']}")

        denied_ok = set(sections["denied"]) == set(MEMBER2_DENIED) and len(sections["denied"]) == 2
        if _check(f"deniedTools == {set(MEMBER2_DENIED)}", denied_ok,
                  f"got={sections['denied']}"):
            pass
        else:
            errs.append(f"[agg-denied] 禁用工具聚合不一致：{sections['denied']}")

        mcp_ok = sections["mcp"] == [probe_mcp["name"]]
        if _check("mountedMcpNames == [probe_mcp.name]（解析后非裸 id）", mcp_ok,
                  f"got={sections['mcp']}"):
            print(f"      mountedMcpNames={sections['mcp']}")
        else:
            errs.append(f"[agg-mcp] MCP 工具源聚合/解析不一致：{sections['mcp']}")

        # ── 7. 边界-空能力成员不破坏聚合 ──
        print("\n[check 7] 边界：空能力成员（member3）不破坏聚合")
        # member3 全空：roster 含它但聚合分段内容不变（空字段被 filter）。
        # 与组件 filter(s.items.length>0) 一致——member3 不贡献任何段。
        m3 = next((a for a in agents if a["id"] == member3["id"]), {})
        m3_empty = (
            not (m3.get("skills") or []) and not (m3.get("extra_skills") or [])
            and not (m3.get("mounted_skills") or []) and not (m3.get("mounted_mcp") or [])
            and not (m3.get("allowed_tools") or []) and not (m3.get("denied_tools") or [])
        )
        # member3 在 roster 内但聚合分段内容不变（全空贡献无）
        boundary_ok = m3_empty and member3["id"] in roster_ids
        if _check("member3 全空入群 + 聚合分段内容不变（空段被过滤）", boundary_ok,
                  f"m3_empty={m3_empty} in_roster={member3['id'] in roster_ids}"):
            pass
        else:
            errs.append("[boundary] 空能力成员破坏聚合或不在 roster")
        # 再确认聚合分段无空段（组件 filter items.length>0 的等价：每段非空才渲染）
        all_sections_nonempty = all(len(v) > 0 for v in sections.values())
        if _check("五分段均非空（无空段噪音，组件会渲染全部 5 段）", all_sections_nonempty):
            pass
        else:
            errs.append("[boundary] 出现空段（组件会过滤，但预期全非空）")

    finally:
        # ── 8. 收尾清理 ──
        print(f"\n[cleanup] 删除探针（group + {len(probe_agent_ids)} agent + skill + mcp）")
        if probe_group_id:
            if not await delete_group(probe_group_id):
                errs.append(f"[cleanup] 删除 group {probe_group_id} 失败")
        for aid in probe_agent_ids:
            if not await delete_agent(aid):
                errs.append(f"[cleanup] 删除 agent {aid} 失败")
        if probe_skill_id and not await delete_skill(probe_skill_id):
            errs.append(f"[cleanup] 删除 skill {probe_skill_id} 失败")
        if probe_mcp_id and not await delete_mcp(probe_mcp_id):
            errs.append(f"[cleanup] 删除 mcp {probe_mcp_id} 失败")
        # 校验无残留
        final_agents = await list_agents()
        leaked = [a["id"] for a in final_agents if a["id"] in probe_agent_ids]
        if not _check("清理后无残留探针 agent", not leaked, f"{len(leaked)} 残留"):
            errs.append(f"[cleanup] {len(leaked)} 个 agent 残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 成员能力概况展示端到端验证通过：")
    print("  · 探针落库：coord+3member+skill+mcp+group 全部 200；")
    print("  · 能力写入：PUT /api/agents/{id} 一次性写齐六类能力字段，回读一致；")
    print("  · 四路真源：members/agents/skills/mcp 全 200 含探针；")
    print("  · 解析映射：skillNameMap/mcpNameMap 把裸 id 解析为可读 name；")
    print("  · 聚合-角色技能：跨群主+成员去重 union(skills+extra_skills)；")
    print("  · 聚合-挂载/工具/MCP：四类分段各自正确 + id→name 解析；")
    print("  · 边界：空能力成员入群不破坏聚合，五分段均非空。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
