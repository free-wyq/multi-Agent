"""MT-06 自测：创建后增删成员 / 改 Leader 指令（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-01/MT-05 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理，不连 WS）。

MT-06 链路（GroupPage 创建后增删成员 + 修改 Leader 指令）：
  前端 GroupPage：
    · 添加成员：群信息抽屉「添加」→ addMember Modal → groupApi.addMember
      → POST /api/groups/{id}/members body={agentId, alias}
    · 移除成员：成员条目 Popconfirm → groupApi.removeMember
      → DELETE /api/groups/{id}/members/{memberId}
    · 批量移除：「全部移除」Popconfirm → Promise.all(removeMember)
    · 改 Leader 指令：抽屉「修改指挥策略」/「编辑群信息」→ 群设置 Modal
      → groupApi.update → PUT /api/groups/{id} body={config:{leader_strategy}}
    · 换群主：群设置 Modal coordinator_id Select（候选=现有成员）
      → PUT /api/groups/{id} body={coordinator_id}
  后端：
    POST /api/groups/{id}/members → crud.add_member（uq_group_agent 唯一约束，
      route 层先检测重复 → 409「该智能体已在群组中」，替代原裸 500）
    DELETE /api/groups/{id}/members/{mid} → crud.remove_member
    PUT /api/groups/{id} → crud.update_group（config key 级 merge 保留 auto_confirm；
      换 Leader route 校验：新群主须是现有成员，非成员 → 409）

为何不复刻前端 Modal/Popconfirm 交互：那些是 UI 表现非数据契约，HTTP 层验证
「add/remove member 落库 + 重复入群 409 + 换 Leader 成员/非成员 + leader_strategy
写入回读 + config merge 保留共存键」即等价证明「创建后增删成员 + 改 Leader 指令」
链路成立（与 MT-01/MT-05 同构）。

验证十块（确定性断言，无 LLM 依赖）：
  ① 建群探针：POST /api/groups body={coordinator, member_ids:[m1]} → 200 + Group
     （id group_ 前缀 / coordinator_id == 选定群主 / 初始成员 1 个）；
  ② 增成员：POST /api/groups/{id}/members body={m2, alias} → 200 + GroupMember
     （id member_ 前缀 / agent_id == m2 / agent_name 扁平回填）+ 列表 1→2；
  ③ 重复入群：再次 add m2 → 409「该智能体已在群组中」（route 层检测，非裸 500）；
  ④ 重复加群主：add coordinator → 409「该智能体已是群主」；
  ⑤ 删成员：DELETE /api/groups/{id}/members/{m2_member_id} → 200 True + 列表 2→1；
  ⑥ 删不存在成员：DELETE 未知 member_id → 200 False（不抛错，幂等）；
  ⑦ 改 Leader 指令：PUT config={leader_strategy:'MT06策略文本'} → 200 + 回读
     config.leader_strategy == 文本（key 级 merge，不整体替换 config）；
  ⑧ config 共存键保留：写 leader_strategy 后 auto_confirm 仍保留原值
     （MT-03 key 级 merge 不丢共存键——先设 auto_confirm 再设 leader_strategy 回读两键都在）；
  ⑨ 换 Leader 到成员：PUT coordinator_id=m2（已是成员）→ 200 + 回读
     coordinator_id == m2（换群主生效，旧群主降为普通成员）；
  ⑩ 换 Leader 到非成员：PUT coordinator_id=nonmember（不在群）→ 409
     「新群主必须是该群组的现有成员」（route 层校验，防非成员提为群主）；
  收尾：DELETE 探针群 + 探针 agent，校验无残留。

为何不连 WS：MT-06 是同步 HTTP（add/remove member + PUT config/coordinator 直接
查 DB 返回），不经引擎 inbox/WS 事件流，纯 HTTP 校验即可（与 MT-01/MT-05 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

TIMEOUT = 20.0
LEADER_STRATEGY_TEXT = "[MT-06] 测试指挥策略：注重代码质量，每步自测通过再交付"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def create_agent(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/agents", json=payload)
        r.raise_for_status()
        return r.json()


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def add_member(group_id: str, agent_id: str, alias: str | None = None) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/groups/{group_id}/members",
            json={"agentId": agent_id, "alias": alias},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def remove_member(group_id: str, member_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}/members/{member_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


async def list_members(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/members")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def update_group(group_id: str, payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(f"{BASE}/api/groups/{group_id}", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def delete_group(group_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        return r.status_code == 200 and r.json() is True


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MT-06 自测：创建后增删成员 / 改 Leader 指令 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    probe_agent_ids: list[str] = []

    try:
        # ── 0. 候选池 + 探针 agent（含一个不入群的 nonmember 用于⑩）──
        print("\n[check 0] 候选池 + 创建探针 agent")
        agents = await list_agents()
        if not _check("agent 列表非空", len(agents) >= 2, f"仅 {len(agents)} 个"):
            errs.append("[pool] agent 列表不足")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        coord = next((a for a in agents if a.get("role") == "coordinator"), None) or agents[0]
        m1 = next((a for a in agents if a["id"] != coord["id"]), None) or agents[0]
        m2 = next((a for a in agents if a["id"] not in (coord["id"], m1["id"])), None) or agents[0]
        # 探针 nonmember：不入群，用于验证换 Leader 到非成员 → 409
        nonmember = await create_agent({
            "name": "[MT06] 非成员探针",
            "role": "custom",
            "system_prompt": "用于验证换 Leader 到非成员的 409 路径",
            "description": "MT-06 自测",
        })
        probe_agent_ids.append(nonmember["id"])
        print(f"      coord={coord['name']} m1={m1['name']} m2={m2['name']} "
              f"nonmember={nonmember['name']}")

        # ── 1. 建群探针：coord + [m1] ──
        print("\n[check 1] 建群探针：POST /api/groups（coord + [m1]）")
        st, g = await create_group({
            "name": "[MT-06] 增删改探针群",
            "description": "MT-06 创建后增删成员/改指令自测",
            "coordinator_id": coord["id"],
            "member_ids": [m1["id"]],
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append("[create] 非 200")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        probe_group_id = g["id"]
        ok1 = (
            g["id"].startswith("group_")
            and g.get("coordinator_id") == coord["id"]
        )
        if _check("group_ 前缀 + coordinator_id == 选定群主", ok1):
            print(f"      群 id={probe_group_id[:24]}… coord={coord['name']}")
        else:
            errs.append("[create] 群结构异常")

        # ── 2. 增成员：add m2 + alias ──
        print("\n[check 2] 增成员：POST /api/groups/{id}/members（m2 + alias）")
        st, mem = await add_member(probe_group_id, m2["id"], "小后端")
        ok2 = (
            st == 200 and mem is not None
            and mem.get("id", "").startswith("member_")
            and mem.get("agent_id") == m2["id"]
            and mem.get("alias") == "小后端"
            and bool(mem.get("agent_name"))
            and bool(mem.get("agent_role"))
        )
        if _check("200 + GroupMember（member_ 前缀 / agent_id / alias / agent_name 扁平回填）",
                  ok2, f"status={st} body={mem}"):
            print(f"      member id={mem['id'][:22]}… alias={mem.get('alias')!r}")
        else:
            errs.append("[add] 增成员异常")
        members = await list_members(probe_group_id)
        if not _check("成员列表 1→2", len(members) == 2, f"got {len(members)}"):
            errs.append(f"[add] 成员数 {len(members)} != 2")

        # ── 3. 重复入群：再 add m2 → 409 ──
        print("\n[check 3] 重复入群：再次 add m2 → 409")
        st, body = await add_member(probe_group_id, m2["id"])
        dup_ok = st == 409 and "已在群组中" in str(body)
        if _check("409 + 「该智能体已在群组中」", dup_ok, f"status={st} body={body}"):
            pass
        else:
            errs.append(f"[dup] 重复入群 status={st} body={body}")

        # ── 4. 重复加群主：add coordinator → 409 ──
        print("\n[check 4] 重复加群主：add coordinator → 409")
        st, body = await add_member(probe_group_id, coord["id"])
        coord_dup_ok = st == 409 and "群主" in str(body)
        if _check("409 + 「该智能体已是群主」", coord_dup_ok, f"status={st} body={body}"):
            pass
        else:
            errs.append(f"[dup-coord] 加群主 status={st} body={body}")

        # ── 5. 删成员：DELETE m2_member_id ──
        print("\n[check 5] 删成员：DELETE /api/groups/{id}/members/{m2}")
        m2_member = next((m for m in members if m["agent_id"] == m2["id"]), None)
        if not m2_member:
            errs.append("[remove] 找不到 m2 member id")
        else:
            st, ok = await remove_member(probe_group_id, m2_member["id"])
            if _check("200 + True", st == 200 and ok is True, f"status={st} ok={ok}"):
                pass
            else:
                errs.append(f"[remove] 删成员 status={st} ok={ok}")
            after = await list_members(probe_group_id)
            if not _check("成员列表 2→1", len(after) == 1, f"got {len(after)}"):
                errs.append(f"[remove] 成员数 {len(after)} != 1")

        # ── 6. 删不存在成员：DELETE 未知 member_id ──
        print("\n[check 6] 删不存在成员：DELETE 未知 member_id → 200 False（幂等）")
        st, ok = await remove_member(probe_group_id, "member_does_not_exist_xxx")
        if _check("200 + False（不抛错，幂等）", st == 200 and ok is False,
                  f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[remove-404] status={st} ok={ok}")

        # ── 7. 改 Leader 指令：PUT config={leader_strategy} ──
        print("\n[check 7] 改 Leader 指令：PUT config={leader_strategy}")
        # 先设 auto_confirm（测共存键），再设 leader_strategy，验证 merge 不丢
        st, _ = await update_group(probe_group_id, {"config": {"auto_confirm": False}})
        st, body = await update_group(probe_group_id, {"config": {"leader_strategy": LEADER_STRATEGY_TEXT}})
        readback = await get_group(probe_group_id)
        strat_ok = (
            st == 200 and readback is not None
            and (readback.get("config") or {}).get("leader_strategy") == LEADER_STRATEGY_TEXT
        )
        if _check("200 + 回读 config.leader_strategy == 写入文本", strat_ok,
                  f"status={st} config={readback.get('config') if readback else None}"):
            print(f"      leader_strategy={LEADER_STRATEGY_TEXT!r}")
        else:
            errs.append("[strategy] leader_strategy 写入/回读不一致")

        # ── 8. config 共存键保留：auto_confirm 仍在 ──
        print("\n[check 8] config 共存键保留：写 leader_strategy 后 auto_confirm 仍在")
        coexist_ok = (
            readback is not None
            and (readback.get("config") or {}).get("auto_confirm") is False
            and (readback.get("config") or {}).get("leader_strategy") == LEADER_STRATEGY_TEXT
        )
        if _check("auto_confirm 保留 + leader_strategy 共存（key 级 merge）", coexist_ok,
                  f"config={readback.get('config') if readback else None}"):
            pass
        else:
            errs.append("[coexist] config key 级 merge 丢失共存键")

        # ── 9. 换 Leader 到成员：PUT coordinator_id=m2（已是成员?需先重新 add m2）──
        print("\n[check 9] 换 Leader 到成员：PUT coordinator_id=现有成员")
        # m2 在 check5 已被删，重新 add 回来作为换 Leader 候选
        await add_member(probe_group_id, m2["id"])
        st, body = await update_group(probe_group_id, {"coordinator_id": m2["id"]})
        readback = await get_group(probe_group_id)
        swap_ok = (
            st == 200 and readback is not None
            and readback.get("coordinator_id") == m2["id"]
        )
        if _check("200 + 回读 coordinator_id == m2（换群主生效）", swap_ok,
                  f"status={st} coord={readback.get('coordinator_id') if readback else None}"):
            print(f"      群主 {coord['name']} → {m2['name']}")
        else:
            errs.append("[swap] 换 Leader 到成员失败")

        # ── 10. 换 Leader 到非成员：PUT coordinator_id=nonmember → 409 ──
        print("\n[check 10] 换 Leader 到非成员：PUT coordinator_id=nonmember → 409")
        st, body = await update_group(probe_group_id, {"coordinator_id": nonmember["id"]})
        nonmem_ok = st == 409 and "现有成员" in str(body)
        if _check("409 + 「新群主必须是该群组的现有成员」", nonmem_ok,
                  f"status={st} body={body}"):
            pass
        else:
            errs.append(f"[swap-nonmem] 换 Leader 到非成员 status={st} body={body}")

    finally:
        # ── 收尾清理 ──
        print(f"\n[cleanup] 删除探针（group={bool(probe_group_id)} + {len(probe_agent_ids)} agent）")
        if probe_group_id:
            if not await delete_group(probe_group_id):
                errs.append(f"[cleanup] 删除 group {probe_group_id} 失败")
        for aid in probe_agent_ids:
            if not await delete_agent(aid):
                errs.append(f"[cleanup] 删除 agent {aid} 失败")
        # 校验群无残留
        if probe_group_id:
            leftover = await get_group(probe_group_id)
            if not _check("清理后探针群已删除", leftover is None):
                errs.append("[cleanup] 探针群残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 创建后增删成员 / 改 Leader 指令端到端验证通过：")
    print("  · 建群探针：coord + [m1] → 200 + Group（group_ 前缀）；")
    print("  · 增成员：add m2 + alias → 200 + GroupMember（扁平回填）+ 列表 1→2；")
    print("  · 重复入群：再 add m2 → 409「该智能体已在群组中」（route 检测非裸 500）；")
    print("  · 重复加群主：add coord → 409「该智能体已是群主」；")
    print("  · 删成员：DELETE m2 → 200 True + 列表 2→1；")
    print("  · 删不存在成员：→ 200 False（幂等不抛错）；")
    print("  · 改 Leader 指令：PUT config.leader_strategy → 回读一致；")
    print("  · config 共存键：写 leader_strategy 后 auto_confirm 仍保留（key 级 merge）；")
    print("  · 换 Leader 到成员：PUT coordinator_id=成员 → 200 + 回读更新；")
    print("  · 换 Leader 到非成员：→ 409「新群主必须是现有成员」。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
