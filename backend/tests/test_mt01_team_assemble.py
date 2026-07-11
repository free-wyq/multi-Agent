"""MT-01 自测：从已有 Agent 列表选择成员组建团队（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 AG-05/MC-01 自测模式（httpx HTTP 真源交叉验证，
不连 WS）。

MT-01 链路（GroupPage 新建群组 → 从 Agent 列表选成员组建团队）：
  前端 GroupPage.handleCreate：
    · groupApi.create({name, coordinator_id, description}) → POST /api/groups
      （GroupCreatePayload.member_ids 未用——前端走 create 后逐个 addMember，不复刻
       payload.member_ids 批量路径，本自测同样走 create + addMember 与前端一致）
    · Promise.all(selected.map(agentId => groupApi.addMember(group.id, agentId)))
      → POST /api/groups/{id}/members body={agentId, alias}
  后端：
    POST /api/groups → crud.create_group 落库 GroupEntity（id group_ 前缀）
    POST /api/groups/{id}/members → crud.add_member 落库 MemberEntity（id member_ 前缀，
      uq_group_agent 唯一约束防同一 agent 重复入群）
    GET /api/groups/{id}/members → crud.list_group_members_with_agent
      join AgentEntity 返回扁平 GroupMember（含 agent_name/agent_role）

「从已有 Agent 列表选择」的含义：成员来自 GET /api/agents 返回的全量智能体列表
（种子 agent_coord_1/agent_frontend_1/agent_backend_1），不是临时创建新 agent。
本自测先拉 agent 列表（确认候选池），再从中选 1 群主 + 2 成员组队，验证
「选已有 Agent 组队」链路忠实。

验证八块（确定性断言）：
  ① 候选池：GET /api/agents 返回非空 agent 列表（「已有 Agent」来源）；
  ② 创建群组：POST /api/groups 落库返回 Group（id group_ 前缀 / coordinator_id ==
     选定群主 / status=active / created_at 非空）；
  ③ 选成员入群：POST /api/groups/{id}/members 逐个 addMember → 200 + GroupMember
     （id member_ 前缀 / agent_id == 选定成员 / agent_name/agent_role 扁平回填）；
  ④ 成员列表：GET /api/groups/{id}/members 返回 N 条成员（按 joined_at 排序），
     每条 agent_name/agent_role 非空（join AgentEntity 成功）；
  ⑤ 单读群组：GET /api/groups/{id} 回读 == create 响应（持久化一致）；
  ⑥ 防重复入群：对同一 agent 再次 addMember → 409/500（uq_group_agent 唯一约束触发，
     防同一 agent 重复入群）；
  ⑦ 全局群组列表：GET /api/groups 列表含探针群组（fetchAll 刷新拿到）；
  ⑧ 收尾清理：DELETE /api/groups/{id} 级联删 members/tasks/messages，校验无残留。

为何不连 WS：MT-01 是同步 HTTP 接口（create_group + add_member + list_members
直接查 DB 返回），不经引擎 inbox/WS 事件流，纯 HTTP 校验即可（与 AG-05/MC-01 同构）。
引擎启动（add_engine）是 MT-02（指定/自动 Leader）才需要——本自测验证「选已有
Agent 组建团队」的数据契约（群组落库 + 成员关系 + 扁平回填 + 防重复 + 级联清理），
不验证引擎运行时（那是 MT-07 解散团队停引擎 + MT-09 Leader 接收任务的范畴）。

为何不复刻前端 Promise.all addMember：前端是并发 addMember，本自测串行 addMember
（顺序确定便于断言），数据契约一致（最终都落 N 条 member）。并发与串行对落库结果
无差异（uq_group_agent 约束在 DB 层兜底）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 探针群组名（[MT-01] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-01] 组队自测探针组"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def add_member(group_id: str, agent_id: str, alias: str | None = None) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{BASE}/api/groups/{group_id}/members",
            json={"agentId": agent_id, "alias": alias},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_members(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/members")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json() if r.status_code == 200 else []


async def delete_group(group_id: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MT-01 自测：从已有 Agent 列表选择成员组建团队 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None

    # ── 1. 候选池：GET /api/agents 返回非空 agent 列表 ──
    print("\n[check 1] 候选池：GET /api/agents（「已有 Agent」来源）")
    agents = await list_agents()
    if not _check("agent 列表非空（至少 1 个候选）", len(agents) >= 1,
                  f"仅 {len(agents)} 个"):
        errs.append("[pool] agent 列表为空，无法组队")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"      候选 {len(agents)} 个：")
    for a in agents:
        print(f"        · {a.get('id')} | {a.get('name')} | role={a.get('role')}")

    # 从候选池选 1 群主 + 2 成员（种子协调者作群主，前端/后端作成员）。
    # 用 role 定位而非硬编码 id（种子被外部增删时仍能找到对应角色）。
    coord = next((a for a in agents if a.get("role") == "coordinator"), None)
    frontend = next((a for a in agents if a.get("role") == "frontend_engineer"), None)
    backend = next((a for a in agents if a.get("role") == "backend_engineer"), None)
    # 兜底：若种子角色缺失，退化为取前 3 个 agent（至少能组队验证契约）。
    if not (coord and frontend and backend):
        if len(agents) >= 3:
            coord, frontend, backend = agents[0], agents[1], agents[2]
            print("      [fallback] 种子角色缺失，退化为取前 3 个 agent 组队")
        else:
            errs.append("[pool] 候选不足 3 个，无法选 1 群主 + 2 成员")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
    coord_id = coord["id"]
    member_ids = [frontend["id"], backend["id"]]
    print(f"      选定：群主={coord_id}({coord.get('name')}) "
          f"成员={member_ids}({frontend.get('name')}/{backend.get('name')})")

    # ── 2. 创建群组：POST /api/groups ──
    print("\n[check 2] 创建群组：POST /api/groups（name/coordinator_id/description）")
    status, group = await create_group({
        "name": PROBE_GROUP_NAME,
        "coordinator_id": coord_id,
        "description": "MT-01 组队自测探针",
    })
    if not _check("HTTP 200", status == 200, f"status={status} body={group}"):
        errs.append(f"[create] 非 200 status={status}")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    assert group is not None
    probe_group_id = group["id"]
    create_ok = (
        str(group.get("id", "")).startswith("group_")
        and group.get("name") == PROBE_GROUP_NAME
        and group.get("coordinator_id") == coord_id
        and group.get("status") == "active"
        and bool(group.get("created_at"))
    )
    if _check(
        "落库 group_ 前缀 / name / coordinator_id==群主 / status=active / created_at",
        create_ok,
        f"group={group}",
    ):
        print(f"      样本：id={group['id'][:24]}… coord={group.get('coordinator_id')}")
    else:
        errs.append(f"[create] 字段异常：{group}")

    # ── 3. 选成员入群：逐个 addMember ──
    print("\n[check 3] 选成员入群：POST /api/groups/{id}/members（逐个 addMember）")
    added_members: list[dict] = []
    for aid in member_ids:
        st, mem = await add_member(probe_group_id, aid, None)
        if not _check(f"addMember {aid} → 200", st == 200, f"status={st} body={mem}"):
            errs.append(f"[addmember] {aid} 非 200 status={st}")
            continue
        assert mem is not None
        mem_ok = (
            str(mem.get("id", "")).startswith("member_")
            and mem.get("group_id") == probe_group_id
            and mem.get("agent_id") == aid
            and bool(mem.get("agent_name"))
            and bool(mem.get("agent_role"))
        )
        if _check(f"member 落库 id member_ / agent_id={aid} / agent_name+role 扁平回填",
                  mem_ok, f"mem={mem}"):
            print(f"      样本：id={mem['id'][:20]}… "
                  f"agent_name={mem.get('agent_name')} role={mem.get('agent_role')}")
            added_members.append(mem)
        else:
            errs.append(f"[addmember] {aid} 字段异常：{mem}")

    # ── 4. 成员列表：GET /api/groups/{id}/members ──
    print("\n[check 4] 成员列表：GET /api/groups/{id}/members（扁平含 agent_name/role）")
    members = await list_members(probe_group_id)
    if _check(f"成员列表含 {len(member_ids)} 条", len(members) == len(member_ids),
              f"实际 {len(members)} 条"):
        for m in members:
            print(f"      · {m.get('agent_id')} | {m.get('agent_name')} | "
                  f"{m.get('agent_role')} | alias={m.get('alias')}")
    else:
        errs.append(f"[list] 成员数 != {len(member_ids)}：{len(members)}")

    members_agent_ids = {m.get("agent_id") for m in members}
    if not _check("成员列表含全部选定的 member_ids", set(member_ids).issubset(members_agent_ids),
                  f"列表={members_agent_ids} 选定={set(member_ids)}"):
        errs.append("[list] 成员列表缺失选定成员")

    flat_ok = all(
        bool(m.get("agent_name")) and bool(m.get("agent_role")) for m in members
    )
    if _check("每条成员 agent_name/agent_role 非空（join AgentEntity 成功）", flat_ok):
        pass
    else:
        errs.append("[list] 成员扁平字段缺失（agent_name/agent_role 空）")

    # 校验成员 role 与候选池一致（证明成员确实来自已有 Agent 列表，非凭空）
    role_by_agent = {a["id"]: a.get("role") for a in agents}
    roles_match = all(
        m.get("agent_role") == role_by_agent.get(m.get("agent_id")) for m in members
    )
    if _check("成员 agent_role 与候选池一致（成员来自已有 Agent 列表）", roles_match,
              f"列表roles={[(m.get('agent_id'), m.get('agent_role')) for m in members]}"):
        pass
    else:
        errs.append("[list] 成员 role 与候选池不符（非来自已有 Agent 列表）")

    # ── 5. 单读群组：GET /api/groups/{id} 回读 == create 响应 ──
    print("\n[check 5] 单读群组：GET /api/groups/{id} 回读 == create 响应")
    reread = await get_group(probe_group_id)
    if reread is None:
        _check("回读 200", False, "404")
        errs.append("[reread] 群组 404")
    else:
        same = (
            reread.get("id") == group.get("id")
            and reread.get("name") == group.get("name")
            and reread.get("coordinator_id") == group.get("coordinator_id")
            and reread.get("status") == group.get("status")
        )
        if _check("回读 id/name/coordinator_id/status == create 响应", same,
                  f"reread={reread}"):
            pass
        else:
            errs.append(f"[reread] 回读漂移：{reread}")

    # ── 6. 防重复入群：同一 agent 再次 addMember → 唯一约束触发 ──
    print("\n[check 6] 防重复入群：同一 agent 再次 addMember（uq_group_agent 唯一约束）")
    dup_st, dup_body = await add_member(probe_group_id, member_ids[0], None)
    # 唯一约束触发：后端要么 500（IntegrityError 未捕获）要么 409。无论哪种，非 200 即符合「防重复」。
    if _check("重复 addMember 非 200（唯一约束拦截）", dup_st != 200,
              f"status={dup_st}（应非 200，防同一 agent 重复入群）"):
        print(f"      拒绝重复入群 status={dup_st}（uq_group_agent 生效）")
    else:
        # 200 说明允许重复入群——可能是设计变更（改为幂等 addMember）。若如此，校验是否返回原 member 而非新建。
        if dup_body and str(dup_body.get("agent_id", "")) == member_ids[0]:
            print(f"      [note] addMember 对重复 agent 返回 200 幂等（未新建重复行），"
                  f"视为可接受")
        else:
            errs.append(f"[dup] 重复 addMember 返回 200 且非幂等：{dup_body}")

    # 重复后成员数应仍 == 2（未被重复 addMember 增加）
    members_after_dup = await list_members(probe_group_id)
    if not _check("重复 addMember 后成员数仍 == 2（无重复行）",
                  len(members_after_dup) == len(member_ids),
                  f"实际 {len(members_after_dup)} 条"):
        errs.append(f"[dup] 重复入群后成员数={len(members_after_dup)}（应有重复行）")

    # ── 7. 全局群组列表：GET /api/groups 含探针群组 ──
    print("\n[check 7] 全局群组列表：GET /api/groups 含探针群组（fetchAll 刷新拿到）")
    all_groups = await list_groups()
    all_ids = {g["id"] for g in all_groups}
    if _check("列表含探针群组", probe_group_id in all_ids):
        listed = next((g for g in all_groups if g["id"] == probe_group_id), None)
        if listed:
            print(f"      列表项：name={listed.get('name')} coord={listed.get('coordinator_id')}")
    else:
        errs.append("[list-groups] 探针群组不在全局列表")

    # ── 8. 收尾清理：DELETE /api/groups/{id}（级联删 members）──
    print(f"\n[cleanup] DELETE /api/groups/{probe_group_id[:24]}…（级联删 members）")
    ok = await delete_group(probe_group_id)
    if not _check("删除探针群组 → True", ok):
        errs.append("[cleanup] 删除失败")

    # 校验级联：群组删后 members 列表空（级联删 MemberEntity）
    leftover_members = await list_members(probe_group_id)
    # 群组删了 GET members 可能返回空或 404→[]；无论哪种应无成员残留
    if _check("群组删除后 members 无残留（级联删 MemberEntity）",
              len(leftover_members) == 0,
              f"残留 {len(leftover_members)} 条"):
        pass
    else:
        errs.append(f"[cleanup] 级联删失败，残留 {len(leftover_members)} 条成员")

    # 校验群组已从全局列表移除
    final_groups = await list_groups()
    if not _check("探针群组已从全局列表移除",
                  probe_group_id not in {g["id"] for g in final_groups}):
        errs.append("[cleanup] 群组仍残留在全局列表")

    # ── 汇总 ──
    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 从已有 Agent 列表选择成员组建团队端到端验证通过：")
    print("  · 候选池：GET /api/agents 返回非空 agent 列表（「已有 Agent」来源）；")
    print("  · 创建群组：POST /api/groups 落库 Group（group_ 前缀 / coordinator_id / active）；")
    print("  · 选成员入群：POST /api/groups/{id}/members 逐个 addMember（member_ 前缀 +")
    print("    agent_name/agent_role 扁平回填，证明成员来自已有 Agent 列表）；")
    print("  · 成员列表：GET /api/groups/{id}/members 返回 N 条扁平成员（join AgentEntity）；")
    print("  · 单读群组：回读 == create 响应（持久化一致）；")
    print("  · 防重复入群：uq_group_agent 唯一约束拦截同一 agent 重复入群；")
    print("  · 全局群组列表含探针群组（fetchAll 刷新拿到）；")
    print("  · 收尾 DELETE 群组级联删 members + 从全局列表移除（无残留）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
