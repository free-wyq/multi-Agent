"""MT-02 自测：指定/自动指定 Leader（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-01/AG-05 自测模式（httpx HTTP 真源交叉验证，
不连 WS）。

MT-02 链路（GroupPage 创建群组时的 Leader 指定）：
  前端 GroupPage 新建群组表单：群主 Form.Item(coordinator_id) rules=[required]（必选），
  但 PRD MT-02 要求支持「指定 Leader（或系统自动指定）」——即用户可不指定，系统自动挑。
  后端 crud.create_group 补全：payload.coordinator_id 非空 → 指定路径原样透传；
  payload.coordinator_id 为空 → 自动指定：优先 role=coordinator 的 agent，否则退化取
  agent 列表首个（保证群组至少有一个可路由 Leader，避免空 coordinator_id 的坏群）。
  群设置（GroupPage 群信息编辑 Modal）的 coordinator_id Select 也可改 Leader（update_group）。

两条路径：
  ① 指定 Leader：POST /api/groups body={coordinator_id: <agent_id>} → 落库 coordinator_id ==
     指定值（原样透传，不触发自动补全）；
  ② 自动指定 Leader：POST /api/groups body 不含 coordinator_id（或空）→ 落库 coordinator_id
     == 自动挑的 agent_id（role=coordinator 优先，退化取列表首个）。

验证八块（确定性断言）：
  ① 候选池：GET /api/agents 返回非空 agent 列表，含 role=coordinator 的 agent（自动指定的真源）；
  ② 指定路径：POST /api/groups body={coordinator_id: frontend_agent_id} → 落库
     coordinator_id == frontend_agent_id（原样透传，不被自动补全覆盖）；
  ③ 自动指定路径：POST /api/groups body 不含 coordinator_id → 落库 coordinator_id ==
     role=coordinator 的 agent_id（优先规则）；
  ④ 自动指定回填：自动路径的 coordinator_id 必须是「有效 agent」（在 agent 列表里存在），
     且 role=coordinator（除非无 coordinator 角色时退化）；
  ⑤ 群组基本信息：两条路径都返回 Group（id group_ 前缀 / name / status=active / created_at 非空）；
  ⑥ 单读回读：GET /api/groups/{id} 回读 coordinator_id == create 响应（持久化一致）；
  ⑦ 群主引擎身份：Leader 的 is_coordinator 判定 == (agent_id == coordinator_id)——
     通过 plan_get 端点 /api/groups/{id}/plan 间接验证 coordinator_id 已落库且非空
     （plan_get 读 group.coordinator_id，409 "group has no coordinator" 是空 coordinator 的标志）；
  ⑧ 收尾清理：DELETE 两个探针群组，校验无残留。

为何不连 WS：MT-02 是同步 HTTP（create_group 落库 coordinator_id），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 MT-01/AG-05 同构）。引擎为 Leader 启动（add_engine）是 MT-07 解散停引擎
+ MT-09 Leader 接收任务的范畴，本自测聚焦「Leader 指定/自动指定」的数据契约。

为何用 plan_get 间接验证 coordinator 落库：plan_get(/api/groups/{id}/plan) 内部读
group.coordinator_id，返回体含 coordinator_id 字段——是 coordinator_id 落库的独立真源
（不依赖 create 响应，避免「create 写啥回读啥」的同源幻觉）。空 coordinator 时 plan_get
返 coordinator_id="" 而非 409（plan_get 不抛错，只是 plan 空），故用 plan_get 的
coordinator_id 字段交叉验证。

为何指定路径用非 coordinator 角色 agent 当 Leader：验证「指定路径原样透传」——若指定一个
role=frontend_engineer 的 agent 当 Leader，落库 coordinator_id 必须仍是该 agent_id（不能被
「自动补全为 coordinator 角色」逻辑覆盖）。这是指定 vs 自动两条路径分叉的关键区分点。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 探针群组名（[MT-02] 前缀便于溯源 + 清理识别）。
PROBE_SPECIFIED_NAME = "[MT-02] 指定Leader探针组"
PROBE_AUTO_NAME = "[MT-02] 自动Leader探针组"


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


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def plan_get(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/plan")
        return r.json() if r.status_code == 200 else None


async def delete_group(group_id: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MT-02 自测：指定/自动指定 Leader ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []

    # ── 1. 候选池：GET /api/agents（含 role=coordinator 的 agent）──
    print("\n[check 1] 候选池：GET /api/agents（自动指定真源）")
    agents = await list_agents()
    if not _check("agent 列表非空", len(agents) >= 1, f"仅 {len(agents)} 个"):
        errs.append("[pool] agent 列表为空")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    coord_agent = next((a for a in agents if a.get("role") == "coordinator"), None)
    non_coord = next((a for a in agents if a.get("role") != "coordinator"), None)
    print(f"      候选 {len(agents)} 个")
    if coord_agent:
        print(f"      role=coordinator: {coord_agent['id']}({coord_agent['name']})")
    else:
        print("      [note] 无 role=coordinator 的 agent（自动指定将退化为列表首个）")
    if not non_coord:
        # 兜底：全是 coordinator 角色也能测指定路径（透传逻辑与角色无关）
        non_coord = agents[0] if agents else None
    agent_ids = {a["id"] for a in agents}

    # ── 2. 指定路径：POST /api/groups body={coordinator_id: non_coord} ──
    print("\n[check 2] 指定 Leader：POST /api/groups body={coordinator_id: <非coordinator角色agent>}")
    specified_leader = non_coord["id"] if non_coord else ""
    st, g_spec = await create_group({
        "name": PROBE_SPECIFIED_NAME,
        "coordinator_id": specified_leader,
        "description": "MT-02 指定路径探针",
    })
    if not _check("HTTP 200", st == 200, f"status={st} body={g_spec}"):
        errs.append(f"[specified] 非 200 status={st}")
        g_spec = None
    else:
        assert g_spec is not None
        probe_ids.append(g_spec["id"])
        # 关键：落库 coordinator_id == 指定值（原样透传，不被自动补全覆盖）
        spec_ok = (
            str(g_spec.get("id", "")).startswith("group_")
            and g_spec.get("coordinator_id") == specified_leader
            and g_spec.get("name") == PROBE_SPECIFIED_NAME
            and g_spec.get("status") == "active"
        )
        if _check(
            f"落库 coordinator_id == 指定值 {specified_leader}（原样透传，不被自动补全覆盖）",
            spec_ok,
            f"group={g_spec}",
        ):
            print(f"      样本：id={g_spec['id'][:24]}… leader={g_spec.get('coordinator_id')}"
                  f"（指定的是 non-coordinator 角色，验证透传）")
        else:
            errs.append(f"[specified] coordinator_id 漂移：{g_spec}")

    # ── 3. 自动指定路径：POST /api/groups body 不含 coordinator_id ──
    print("\n[check 3] 自动指定 Leader：POST /api/groups body 不含 coordinator_id")
    st, g_auto = await create_group({
        "name": PROBE_AUTO_NAME,
        "description": "MT-02 自动指定路径探针",
    })
    if not _check("HTTP 200", st == 200, f"status={st} body={g_auto}"):
        errs.append(f"[auto] 非 200 status={st}")
        g_auto = None
    else:
        assert g_auto is not None
        probe_ids.append(g_auto["id"])
        auto_leader = g_auto.get("coordinator_id", "")
        # 关键：自动指定后 coordinator_id 非空（自动补全生效）
        if _check(
            "自动指定后 coordinator_id 非空（自动补全生效）",
            bool(auto_leader),
            f"coordinator_id={auto_leader!r}",
        ):
            print(f"      样本：id={g_auto['id'][:24]}… auto leader={auto_leader}")
        else:
            errs.append(f"[auto] coordinator_id 空（自动补全未生效）：{g_auto}")

    # ── 4. 自动指定回填：coordinator_id 是有效 agent + 优先 coordinator 角色 ──
    print("\n[check 4] 自动指定回填：coordinator_id 是有效 agent（优先 role=coordinator）")
    if g_auto:
        auto_leader = g_auto.get("coordinator_id", "")
        valid = auto_leader in agent_ids
        if _check("自动指定的 Leader 是有效 agent（在 agent 列表里存在）", valid,
                  f"auto_leader={auto_leader} 不在 {agent_ids}"):
            pass
        else:
            errs.append(f"[auto-valid] 自动 Leader {auto_leader} 非有效 agent")

        # 优先规则：有 coordinator 角色时，自动指定应挑它
        if coord_agent:
            if _check(
                f"优先规则：有 coordinator 角色时自动指定挑它（{coord_agent['id']}）",
                auto_leader == coord_agent["id"],
                f"auto_leader={auto_leader} want={coord_agent['id']}",
            ):
                print(f"      自动挑中 role=coordinator: {coord_agent['name']}")
            else:
                errs.append(
                    f"[auto-priority] 应优先 role=coordinator "
                    f"({coord_agent['id']}) 实际挑了 {auto_leader}"
                )
        else:
            # 退化：无 coordinator 角色时挑列表首个
            first_id = agents[0]["id"] if agents else ""
            if _check(
                f"退化规则：无 coordinator 角色时挑列表首个（{first_id}）",
                auto_leader == first_id,
                f"auto_leader={auto_leader} want={first_id}",
            ):
                print(f"      无 coordinator 角色，退化为列表首个: {agents[0]['name']}")
            else:
                errs.append(f"[auto-fallback] 退化挑首个失败：{auto_leader} want={first_id}")

    # ── 5. 群组基本信息 ──
    print("\n[check 5] 群组基本信息（两条路径都返回合法 Group）")
    for tag, g in (("specified", g_spec), ("auto", g_auto)):
        if not g:
            continue
        ok = (
            str(g.get("id", "")).startswith("group_")
            and g.get("name") in (PROBE_SPECIFIED_NAME, PROBE_AUTO_NAME)
            and g.get("status") == "active"
            and bool(g.get("created_at"))
        )
        if _check(f"{tag} 群组 id group_/name/status=active/created_at", ok, f"group={g}"):
            pass
        else:
            errs.append(f"[basic-{tag}] 群组基本信息异常：{g}")

    # ── 6. 单读回读：coordinator_id == create 响应（持久化一致）──
    print("\n[check 6] 单读回读：GET /api/groups/{id} coordinator_id == create 响应")
    for tag, g in (("specified", g_spec), ("auto", g_auto)):
        if not g:
            continue
        reread = await get_group(g["id"])
        if reread is None:
            _check(f"{tag}: 回读 200", False, "404")
            errs.append(f"[reread-{tag}] 404")
            continue
        same = (
            reread.get("id") == g.get("id")
            and reread.get("coordinator_id") == g.get("coordinator_id")
            and reread.get("name") == g.get("name")
        )
        if _check(f"{tag}: 回读 coordinator_id == create 响应", same, f"reread={reread}"):
            pass
        else:
            errs.append(f"[reread-{tag}] 回读漂移：{reread}")

    # ── 7. 群主落库独立真源：plan_get 的 coordinator_id 字段交叉验证 ──
    print("\n[check 7] 群主落库独立真源：GET /api/groups/{id}/plan 的 coordinator_id 字段")
    for tag, g in (("specified", g_spec), ("auto", g_auto)):
        if not g:
            continue
        plan = await plan_get(g["id"])
        if not plan or "coordinator_id" not in (plan or {}):
            _check(f"{tag}: plan_get 返回含 coordinator_id", False, f"plan={plan}")
            errs.append(f"[plan-{tag}] plan_get 异常：{plan}")
            continue
        plan_coord = plan.get("coordinator_id", "")
        # plan_get 读 group.coordinator_id，是独立真源（非 create 响应回显）
        same = plan_coord == g.get("coordinator_id") and bool(plan_coord)
        if _check(
            f"{tag}: plan_get.coordinator_id == create 响应 且非空",
            same,
            f"plan_coord={plan_coord!r} create={g.get('coordinator_id')!r}",
        ):
            print(f"      独立真源确认 coordinator_id={plan_coord}（非空，群主已落库）")
        else:
            errs.append(f"[plan-{tag}] plan_get coordinator_id 不符：{plan_coord}")

    # ── 8. 收尾清理：DELETE 两个探针群组 ──
    print(f"\n[cleanup] 删除 {len(probe_ids)} 个探针群组")
    for gid in probe_ids:
        ok = await delete_group(gid)
        if not ok:
            print(f"  ⚠️ 删除失败 {gid}")
            errs.append(f"[cleanup] 删除失败 {gid}")

    # ── 汇总 ──
    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 指定/自动指定 Leader 端到端验证通过：")
    print("  · 候选池：GET /api/agents 含 role=coordinator agent（自动指定真源）；")
    print("  · 指定路径：coordinator_id 原样透传落库（指定非 coordinator 角色也透传，不被自动补全覆盖）；")
    print("  · 自动指定路径：未指定 coordinator_id 时后端自动挑 Leader（优先 role=coordinator，退化列表首个）；")
    print("  · 自动指定回填：coordinator_id 是有效 agent + 优先规则生效；")
    print("  · 群组基本信息合法（group_ 前缀 / name / active / created_at）；")
    print("  · 单读回读 coordinator_id == create 响应（持久化一致）；")
    print("  · plan_get 独立真源交叉验证 coordinator_id 非空（群主已落库）；")
    print("  · 收尾 DELETE 两个探针群组无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
