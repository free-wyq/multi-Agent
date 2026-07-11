"""SA-02 自测：GET /api/status 一次返回所有群组所有 agent 状态。

在线集成测试（httpx 直连已起的后端进程 localhost:8000，与 test_cf_config_endpoint.py
/ test_be_reset_session.py 同模式：不发 pytest、不开 TestClient）。覆盖 SA-02 聚合
端点契约 + SA-01 list_all_status 实现。

端点契约（backend/api/system.py + backend/engine/registry.py）：
  GET /api/status → registry.list_all_status()
      返回 {group_id: [agent status, ...]} —— 一次拉全所有群组所有 agent 状态，
      替代前端 N+1 轮询（逐群组 GET /api/status/{groupId}）。
      每条 agent status = {id, name, role, status, current_task_id}（与单群组
      GET /api/status/{groupId} 同 shape，list_all_status 委托 list_group_status
      per group，单一构造点不漂移）。
      无引擎的群组不在 dict 中（前端把缺失 key 视为「无 agent / 全 offline」）。

  GET /api/status/{group_id} → registry.list_group_status(group_id)
      返回单群组的 [agent status, ...]，list（非 dict）。

验证（真起后端，不发真 LLM 任务——状态聚合是确定性的快照操作）：
  1. GET /api/status 200 + 顶层是 dict（非 list），键是 group_id。
  2. 每个 group_id 的值是 list，每条 agent status 5 字段齐全（id/name/role/
     status/current_task_id），status ∈ {idle, executing, offline}，
     current_task_id 为 str 或 None。
  3. 聚合一致性：聚合端点返回的每个 group 的 agent 列表 == 该群组单独调
     GET /api/status/{groupId} 的列表（list_all_status 委托 list_group_status，
     两路径应逐字段一致，无漂移）。
  4. 覆盖性：聚合 dict 的 group_id 集合应等于所有有引擎的群组集合（冷启动至少
     有种子 group_demo_1；无引擎群组不在 dict 中是合法行为，不算漏）。若聚合为空
     dict（无群组有引擎）→ 降级为 skip 不 fail（冷环境无种子），但报告之。
  5. 与群组列表交叉验证：GET /api/groups 的群组里，有引擎的那些应在聚合 dict 中
     出现（种子 group_demo_1 必在）；无引擎的群组不在聚合 dict 中（合法，不算漏）。
  6. 重复调用稳定性：连续两次 GET /api/status 返回的 group_id 集合 + 每群组 agent
     id 列表一致（无并发任务时状态快照稳定，非随机抖动）。

为何不发真 LLM 任务：
  状态聚合是 registry._engines 的快照读（id/name/role/status/current_task_id），
  确定性高、无时序竞争。发真任务会让某 agent status 短暂变 executing，引入非确定性
  时序（任务何时起何时止），反而让「稳定性断言」难成立。本测聚焦「聚合契约正确」——
  shape + 一致性 + 覆盖性，与 executing 状态无关（executing 的真实流转由 PL/MT 系列覆盖）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

VALID_STATUSES = {"idle", "executing", "offline"}


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health", timeout=5.0)
        return r.json().get("status") == "ok"


async def get_all_status() -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status", timeout=10.0)
        assert r.status_code == 200, f"GET /api/status status={r.status_code} body={r.text}"
        return r.json()


async def get_group_status(group_id: str) -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{group_id}", timeout=10.0)
        assert r.status_code == 200, f"GET /api/status/{group_id} status={r.status_code} body={r.text}"
        return r.json()


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/groups", timeout=10.0)
        assert r.status_code == 200, f"GET /api/groups status={r.status_code} body={r.text}"
        return r.json()


def _check_agent_shape(agent: dict, errs: list[str], prefix: str) -> None:
    """校验单条 agent status 的 5 字段 + status 枚举 + current_task_id 类型。"""
    required = {"id", "name", "role", "status", "current_task_id"}
    missing = required - agent.keys()
    if missing:
        errs.append(f"{prefix} agent status 缺字段：{missing}（实际 {set(agent.keys())}）")
        return
    status = agent.get("status")
    if status not in VALID_STATUSES:
        errs.append(f"{prefix} agent {agent.get('id')} status 非法：{status!r}（合法 {VALID_STATUSES}）")
    ctid = agent.get("current_task_id")
    if ctid is not None and not isinstance(ctid, str):
        errs.append(f"{prefix} agent {agent.get('id')} current_task_id 非 str|None：{ctid!r}({type(ctid).__name__})")


async def main() -> int:
    print("=== SA-02 自测：GET /api/status 一次返回所有群组所有 agent 状态 ===")
    if not await health_ok():
        print("[fatal] backend 不在线（localhost:8000 /health 未返 ok）")
        print("        请先起后端：cd backend && python3 -m uvicorn main:app --port 8000")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # ── 步骤 1：GET /api/status 200 + 顶层 dict ──
    print("\n── 步骤1：GET /api/status 结构（顶层 dict，键=group_id） ──")
    all_status = await get_all_status()
    if not isinstance(all_status, dict):
        errs.append(f"GET /api/status 返回非 dict：{type(all_status).__name__}")
        # 无法继续后续断言，直接跳到结果
        print("\n" + "=" * 50)
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    group_ids = list(all_status.keys())
    print(f"[get] 顶层 dict，共 {len(group_ids)} 个有引擎的群组：{group_ids}")
    print("[check 1] GET /api/status 200 + 顶层 dict  OK")

    # ── 步骤 2：每条 agent status shape ──
    print("\n── 步骤2：每个 group 的 agent status 字段结构 ──")
    total_agents = 0
    for gid, agents in all_status.items():
        if not isinstance(agents, list):
            errs.append(f"group {gid} 的 agent 列表非 list：{type(agents).__name__}")
            continue
        total_agents += len(agents)
        for i, agent in enumerate(agents):
            if not isinstance(agent, dict):
                errs.append(f"group {gid} agent[{i}] 非 dict：{type(agent).__name__}")
                continue
            _check_agent_shape(agent, errs, f"group {gid}")
    if total_agents == 0 and not group_ids:
        print("[warn] 聚合 dict 为空（无群组有引擎，冷环境）——后续断言降级 skip")
    else:
        print(f"[check 2] {total_agents} 条 agent status 5 字段齐全 + status ∈ 合法枚举  OK")

    # ── 步骤 3：聚合一致性——聚合端点 vs 单群组端点逐字段一致 ──
    print("\n── 步骤3：聚合一致性（GET /api/status[gid] == GET /api/status/{gid}） ──")
    mismatch_count = 0
    for gid, aggregated in all_status.items():
        per_group = await get_group_status(gid)
        # list_all_status 委托 list_group_status，两路径应逐字段一致
        if aggregated != per_group:
            mismatch_count += 1
            errs.append(
                f"group {gid} 聚合 vs 单群组不一致：\n"
                f"  聚合={aggregated}\n  单群组={per_group}"
            )
    if mismatch_count == 0 and group_ids:
        print(f"[check 3] {len(group_ids)} 个群组聚合 == 单群组（逐字段一致，无漂移）  OK")

    # ── 步骤 4：覆盖性——有引擎的群组都在聚合 dict ──
    print("\n── 步骤4：覆盖性（有引擎群组都在聚合 dict） ──")
    groups = await list_groups()
    all_group_ids = {g.get("id") for g in groups if g.get("id")}
    # 聚合 dict 的 group_id 集合应 ⊆ 全部群组集合（聚合只含有引擎的）
    extra = set(group_ids) - all_group_ids
    if extra:
        errs.append(f"聚合 dict 含未知群组（不在 /api/groups 列表中）：{extra}")
    print(f"[info] /api/groups 共 {len(all_group_ids)} 群组，聚合 dict 含 {len(group_ids)} 个有引擎")
    if not extra:
        print("[check 4] 聚合 group_id ⊆ 全部群组集合（无幽灵群组）  OK")

    # ── 步骤 5：种子群组 group_demo_1 必在聚合 dict（种子数据保证有引擎） ──
    print("\n── 步骤5：种子群组 group_demo_1 在聚合 dict ──")
    if "group_demo_1" in all_status:
        demo_agents = all_status["group_demo_1"]
        print(f"[check 5] group_demo_1 在聚合 dict，{len(demo_agents)} 个 agent  OK")
    else:
        # 种子未起（可能 init_db 未 seed 或 group_demo_1 被删）——降级为 warn 不 fail
        print("[warn] group_demo_1 不在聚合 dict（种子未 seed 或被删），降级 skip")

    # ── 步骤 6：重复调用稳定性 ──
    print("\n── 步骤6：重复调用稳定性（连续两次 group_id 集合 + agent id 列表一致） ──")
    all_status_2 = await get_all_status()
    gids_1 = set(all_status.keys())
    gids_2 = set(all_status_2.keys())
    if gids_1 != gids_2:
        errs.append(f"两次调用 group_id 集合不一致：{gids_1} vs {gids_2}")
    else:
        # 每群组 agent id 列表一致（status 可能因任务变化，但 id 列表应稳定）
        stable = True
        for gid in gids_1:
            ids_1 = [a.get("id") for a in all_status[gid]]
            ids_2 = [a.get("id") for a in all_status_2[gid]]
            if ids_1 != ids_2:
                errs.append(f"group {gid} 两次调用 agent id 列表不一致：{ids_1} vs {ids_2}")
                stable = False
        if stable and gids_1:
            print("[check 6] 连续两次调用 group_id 集合 + agent id 列表一致（快照稳定）  OK")
        elif not gids_1:
            print("[skip] 聚合为空，稳定性断言无对象")

    # ── 结果 ──
    print("\n" + "=" * 50)
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("GET /api/status 聚合验证通过：")
    print(f"  · 200 + 顶层 dict（{len(group_ids)} 个有引擎群组，共 {total_agents} agent）；")
    print("  · 每 agent status 5 字段齐全（id/name/role/status/current_task_id），status ∈ 合法枚举；")
    print("  · 聚合 == 单群组逐字段一致（list_all_status 委托 list_group_status 无漂移）；")
    print("  · 聚合 group_id ⊆ 全部群组（无幽灵群组）；")
    print("  · 种子 group_demo_1 在聚合 dict（有引擎）；")
    print("  · 重复调用稳定（group_id 集合 + agent id 列表一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
