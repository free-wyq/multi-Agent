"""MT-07 自测：解散团队（delete group + 停止引擎）（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-01/MT-06 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理）。本自测连 HTTP 状态接口验证引擎生命周期
（不连 WS——解散是同步 DELETE，引擎停止由 GET /api/status 交叉验证）。

MT-07 链路（解散团队 = delete group + 停止引擎）：
  前端 GroupPage handleDeleteGroup：
    · groupApi.delete(chatGroupId) → DELETE /api/groups/{id}
    · 成功后 setChatGroupId(null) + setDrawerOpen(false) + fetchData()
  后端 DELETE /api/groups/{id}：
    · registry.stop_group(group_id)（MT-07 新增）——遍历该群所有 AgentEngine，
      逐个 engine.stop()（cancel run loop + unregister inbox + emit offline status），
      从 registry._engines 移除该群 key，返回停止的引擎数；
    · crud.delete_group(group_id)——级联删 members/tasks/messages + 删 group 行。
  状态真源：GET /api/status/{group_id} = registry.list_group_status(group_id)
    返回该群各 agent 的 status（idle|executing|offline）+ current_task_id。
    引擎存在 → 返回 N 条；引擎不存在（未启动/已停止）→ 返回 []。

验证关键：解散后引擎真停止（status 列表变空），不只是 DB 行删除。若 delete_group
只删 DB 不停引擎，引擎会泄漏（run loop 继续 + 持有已删群引用直到进程退出）。
本自测通过「引擎启动 → 解散 → status 列表空」证明 stop_group 真停引擎。

引擎启动策略：当前引擎在 lifespan load_from_store 时为「所有群组的 coord+member」
启动（无按需启动）。故自测需触发一次 reload（touch main.py + 等健康恢复 + 等
load_from_store 完成）让探针群的引擎启动，才能验证 stop_group 停止它们。
（这是引擎架构现状，非自测负担——后端 reload 是开发期常规操作。）

验证七块（确定性断言）：
  ① 建群探针：POST /api/groups（coord + [m1,m2]）→ 200 + Group（id group_ 前缀）；
  ② 引擎启动：touch main.py 触发 reload → 等健康恢复 + load_from_store →
     GET /api/status/{id} 返回 3 条引擎（coord+m1+m2，status=idle），
     证明探针群引擎已驻留（解散的前提是引擎在跑）；
  ③ 解散团队：DELETE /api/groups/{id} → 200 True（stop_group + delete_group 双步）；
  ④ 引擎已停：GET /api/status/{id} 解散后返回 []（list_group_status 遍历空 group
     → 空列表，证明 stop_group 把群从 _engines 移除，引擎真停止非泄漏）；
  ⑤ DB 级联清理：GET /api/groups/{id} → None（group 行删）；GET members → []
     （member 行级联删）；GET tasks?groupId={id} → []（task 行级联删）；
  ⑥ 全局列表无残留：GET /api/groups 列表不含探针群（fetchAll 刷新拿到，群已删）；
  ⑦ 边界-解散不存在群：DELETE 未知 group_id → 200 False（stop_group no-op +
     delete_group 找不到行返 False，不抛错，幂等）；
  收尾：（探针群已删，无需额外清理；非成员探针 agent 若建了则删）。

为何不连 WS：解散是同步 DELETE，引擎停止的交叉验证用 GET /api/status
（registry.list_group_status 真源）足够，无需抓 WS 的 agent_status/offline 事件
（那会引入时序竞争——offline 事件可能解散前/后到达）。status 列表空是引擎
停止的确定性强证据（list_group_status 直接遍历 _engines，引擎在则非空）。

为何用 reload 触发引擎启动：当前引擎无按需启动（add_engine 仅在 load_from_store
调用），新建群不会自动起引擎。开发期 reload（touch main.py）是让 load_from_store
重跑、为探针群起引擎的常规手段。自测复刻此开发期操作以构造「引擎在跑」前置态。
（若未来加按需启动，本自测的 reload 步骤可简化，但解散验证逻辑不变。）
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

BASE = "http://localhost:8000"
BACKEND_MAIN = "/home/wyq/work/project/multi-Agent/backend/main.py"

TIMEOUT = 20.0
RELOAD_WAIT = 40.0  # touch main.py 后等健康恢复 + load_from_store 的总时限


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def delete_group(group_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


async def group_status(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/status/{group_id}")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def list_members(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}/members")
        return r.json() if r.status_code == 200 else []


async def list_tasks(groupId: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/tasks", params={"groupId": groupId})
        return r.json() if r.status_code == 200 else []


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json() if r.status_code == 200 else []


async def wait_for_engines(group_id: str, expected: int) -> bool:
    """touch main.py 触发 reload，轮询健康 + status 直到探针群引擎数 == expected。

    reload 后 load_from_store 重跑为所有群（含探针群）启动引擎。轮询 status
    列表长度到位即返回 True；超时 RELOAD_WAIT 秒未到位返 False。
    """
    # 用 subprocess touch 而非 httpx（文件系统操作）
    os.system(f"touch {BACKEND_MAIN}")
    deadline = asyncio.get_event_loop().time() + RELOAD_WAIT
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                h = await c.get(f"{BASE}/health")
                if h.status_code == 200:
                    st = await c.get(f"{BASE}/api/status/{group_id}")
                    if st.status_code == 200:
                        engines = st.json()
                        if len(engines) >= expected:
                            return True
        except (httpx.HTTPError, Exception):
            pass  # reload 期间短暂不可用，重试
        await asyncio.sleep(1.0)
    return False


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MT-07 自测：解散团队（delete group + 停止引擎）===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    m1_id: str | None = None
    m2_id: str | None = None

    try:
        # ── 1. 建群探针：coord + [m1, m2] ──
        print("\n[check 1] 建群探针：POST /api/groups（coord + [m1, m2]）")
        agents = await list_agents()
        if not _check("agent 列表非空", len(agents) >= 3, f"仅 {len(agents)} 个"):
            errs.append("[pool] agent 列表不足 3 个")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        coord = next((a for a in agents if a.get("role") == "coordinator"), None) or agents[0]
        m1 = next((a for a in agents if a["id"] != coord["id"]), None) or agents[0]
        m2 = next((a for a in agents if a["id"] not in (coord["id"], m1["id"])), None) or agents[0]
        coord_id, m1_id, m2_id = coord["id"], m1["id"], m2["id"]

        st, g = await create_group({
            "name": "[MT-07] 解散团队探针群",
            "description": "MT-07 解散团队(delete group + 停止引擎)自测",
            "coordinator_id": coord["id"],
            "member_ids": [m1["id"], m2["id"]],
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append("[create] 非 200")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        probe_group_id = g["id"]
        ok1 = g["id"].startswith("group_") and g.get("coordinator_id") == coord["id"]
        if _check("group_ 前缀 + coordinator_id == 群主", ok1):
            print(f"      群 id={probe_group_id[:24]}… coord={coord['name']} 成员=[{m1['name']},{m2['name']}]")
        else:
            errs.append("[create] 群结构异常")

        # ── 2. 引擎启动：reload 触发 load_from_store → status 返回 3 引擎 ──
        print("\n[check 2] 引擎启动：reload 触发 load_from_store → status 返回 3 引擎")
        # reload 前先确认探针群引擎为 0（新建群未起引擎）
        pre = await group_status(probe_group_id)
        print(f"      reload 前探针群引擎数：{len(pre)}（新建群未起引擎，预期 0）")
        ready = await wait_for_engines(probe_group_id, expected=3)
        if not _check("reload 后探针群引擎数 == 3（coord+m1+m2，status=idle）", ready,
                      "reload 后引擎未到位"):
            # 即使 ready=False，也读一次最终状态诊断
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'],e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            ids = {e["id"] for e in engines}
            all_idle = all(e.get("status") == "idle" for e in engines)
            ok_engines = (
                ids == {coord_id, m1_id, m2_id} and all_idle
            )
            if _check("3 引擎 id == {coord,m1,m2} 且全 idle", ok_engines,
                      f"got ids={ids} statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎集合/idle 不符")

        # ── 3. 解散团队：DELETE /api/groups/{id} → 200 True ──
        print("\n[check 3] 解散团队：DELETE /api/groups/{id}")
        st, ok = await delete_group(probe_group_id)
        if _check("200 + True（stop_group + delete_group 双步）", st == 200 and ok is True,
                  f"status={st} ok={ok}"):
            print(f"      已解散群 {probe_group_id[:24]}…")
        else:
            errs.append(f"[disband] 解散 status={st} ok={ok}")

        # ── 4. 引擎已停：status 解散后返回 [] ──
        print("\n[check 4] 引擎已停：GET /api/status/{id} 解散后 → []")
        # 给引擎停止一点时间（stop 是 async，DELETE 返回时已 await 完，但保险起见短轮询）
        stopped = False
        for _ in range(5):
            after = await group_status(probe_group_id)
            if len(after) == 0:
                stopped = True
                break
            await asyncio.sleep(0.5)
        if _check("status 列表 == []（引擎真停止，群从 _engines 移除）", stopped,
                  f"got {len(after)} engines: {after}"):
            pass
        else:
            errs.append(f"[stopped] 解散后仍有 {len(after)} 个引擎泄漏")

        # ── 5. DB 级联清理：group/members/tasks 全删 ──
        print("\n[check 5] DB 级联清理：group/members/tasks 全删")
        g_after = await get_group(probe_group_id)
        if _check("GET /api/groups/{id} → None（group 行删）", g_after is None):
            pass
        else:
            errs.append("[db] group 行残留")
        members_after = await list_members(probe_group_id)
        if _check(f"GET members → []（member 行级联删）", len(members_after) == 0,
                  f"got {len(members_after)}"):
            pass
        else:
            errs.append(f"[db] member 残留 {len(members_after)}")
        tasks_after = await list_tasks(probe_group_id)
        if _check(f"GET tasks?groupId → []（task 行级联删）", len(tasks_after) == 0,
                  f"got {len(tasks_after)}"):
            pass
        else:
            errs.append(f"[db] task 残留 {len(tasks_after)}")

        # ── 6. 全局列表无残留：GET /api/groups 不含探针群 ──
        print("\n[check 6] 全局列表无残留：GET /api/groups")
        groups = await list_groups()
        leaked = [g for g in groups if g["id"] == probe_group_id]
        if _check("全局群组列表不含探针群", len(leaked) == 0, f"{len(leaked)} 个残留"):
            pass
        else:
            errs.append(f"[global] 探针群在全局列表残留")

        # ── 7. 边界-解散不存在群：DELETE 未知 group_id → 200 False ──
        print("\n[check 7] 边界：解散不存在的群 → 200 False（幂等）")
        st, ok = await delete_group("group_does_not_exist_mt07_xxx")
        if _check("200 + False（stop_group no-op + delete_group 找不到行）",
                  st == 200 and ok is False, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[boundary] 解散不存在群 status={st} ok={ok}")

    finally:
        # 探针群在 check 3 已删；若中途失败群可能还在，兜底清理
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 解散团队（delete group + 停止引擎）端到端验证通过：")
    print("  · 建群探针：coord + [m1,m2] → 200 + Group；")
    print("  · 引擎启动：reload → load_from_store → status 返回 3 引擎（coord+m1+m2 idle）；")
    print("  · 解散团队：DELETE /api/groups/{id} → 200 True（stop_group + delete_group）；")
    print("  · 引擎已停：status 解散后 == []（引擎真停止，非泄漏）；")
    print("  · DB 级联：group/members/tasks 全删；")
    print("  · 全局列表：不含探针群（无残留）；")
    print("  · 边界：解散不存在群 → 200 False（幂等）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
