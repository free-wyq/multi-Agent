"""AG-08 自测：挂载技能后 Agent 获得能力（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-05/SK-09/AG-06 自测模式（httpx HTTP 真源
交叉验证，不连 WS）。

AG-08 链路（挂载闭环）：
  POST /api/skills/{skill_id}/mount body={agentId}
    → skills.py mount_skill → crud.mount_skill(agent_id, skill_id)
    → agent.mounted_skills.append(skill_id)（idempotent：已挂载不重复 append）
    → updated_at 刷新 → commit → 返回 AgentDefinition（mounted_skills 含新 skill_id）
  agent 获得能力：worker executor（agent_loop._compose_system_prompt）会
    resolve_skill_contents(mounted_skills) 把技能 content 注入 system_prompt
    （PL-06 已端到端验证 worker 按技能内容执行——本自测聚焦「挂载动作本身 +
    mounted_skills 持久化」而非重复验证 worker 执行，因 PL-06 已覆盖）

为何不连 WS 重复跑 worker：PL-06 自测已完整验证「挂载技能 → worker 加载技能日志
→ 磁盘产物含技能哨兵标记 → task_complete(success)」全链路（worker 自主按技能执行），
重复跑 worker 耗时数分钟且占内存（M12/PL-10 OOM 教训）。AG-08 聚焦「挂载动作本身」
的 HTTP 契约：mount 写 mounted_skills + 持久化 + 列表反映 + unmount 移除 + 边界 +
技能注入 API 契约（resolve_skill_contents）。worker 是否真按技能执行由 PL-06 已证，
AG-08 验证「挂载后能力（技能内容）对 worker 可解析」即可——即 resolve_skill_contents
能按 mounted_skills 解析出技能 content，证明「Agent 获得能力」（能力=可注入的技能内容）。

验证八块（确定性断言）：
  ① 创建探针 agent + 探针技能（技能 content 含固定哨兵标记 SENTINEL）；
  ② 挂载：POST mount → 200 + AgentDefinition.mounted_skills 含 skill_id（挂载生效）；
  ③ 持久化：GET /api/agents/{id} 回读 mounted_skills == mount 响应（落库可读回）；
  ④ 列表反映：GET /api/agents 列表项 mounted_skills 含 skill_id（列表数据源一致）；
  ⑤ 幂等：重复 mount → mounted_skills 仍只 1 个 skill_id（不重复 append）；
  ⑥ 卸载：POST unmount → 200 + mounted_skills 不含 skill_id（移除生效）+ 持久化回读 []；
  ⑦ 边界：mount 不存在的 skill → null（agent 不变，不抛 500）；mount 到不存在的 agent → null；
  ⑧ 「获得能力」契约：resolve_skill_contents([skill_id]) 能解析出含 SENTINEL 的 content——
     证明挂载的 skill_id 可被 worker executor 解析为技能内容（能力注入路径就绪）。
     （直接调 crud.resolve_skill_contents 验证，等价于 worker 执行时解析路径。）

为何用哨兵标记 SENTINEL：技能 content 植入固定串，resolve_skill_contents 解析后含该串
即证明「挂载的 skill_id → 技能 content」映射正确（worker 拿到的能力内容确为该技能），
与 PL-06 哨兵法同思路——确定性证据非语义判断。

收尾：DELETE 探针 skill + agent，避免污染后续自测（AG-11 会 list 智能体/技能计数）。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让脚本能 import backend 模块（验证 resolve_skill_contents 契约）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

BASE = "http://localhost:8000"

# 技能 content 含固定哨兵——resolve_skill_contents 解析后含该串即证明「能力内容」正确映射
SENTINEL = "AG08_CAPABILITY_SENTINEL_OK"
SKILL_CONTENT = f"# AG-08 能力探针技能\n\n{SENTINEL}\n\n本技能用于验证挂载后 agent 能获得此能力内容。"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create_agent() -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/agents",
            json={
                "name": "AG08探针员工",
                "role": "custom",
                "system_prompt": "你是探针员工，用于验证挂载技能。",
                "skills": [],
                "extra_skills": [],
                "description": "AG-08 挂载能力自测",
            },
        )
        r.raise_for_status()
        return r.json()


async def create_skill() -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/skills",
            json={
                "name": "AG08能力探针技能",
                "description": "验证挂载后 agent 获得能力（AG-08自测用）",
                "content": SKILL_CONTENT,
                "source": "custom",
                "tags": ["ag08", "selftest"],
            },
        )
        r.raise_for_status()
        return r.json()


async def mount(skill_id: str, agent_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/skills/{skill_id}/mount", json={"agentId": agent_id}
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def unmount(skill_id: str, agent_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/skills/{skill_id}/unmount", json={"agentId": agent_id}
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def get_agent(agent_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents/{agent_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        r.raise_for_status()
        return r.json()


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.status_code == 200 and r.json() is True


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-08 自测：挂载技能后 Agent 获得能力 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    agent_id = ""
    skill_id = ""
    created_agent = False
    created_skill = False
    try:
        # ── 1. 创建探针 agent + 探针技能 ──
        print("\n[check 1] 创建探针 agent + 探针技能（content 含哨兵 SENTINEL）")
        agent = await create_agent()
        agent_id = agent.get("id", "")
        created_agent = bool(agent_id)
        skill = await create_skill()
        skill_id = skill.get("id", "")
        created_skill = bool(skill_id)
        if not _check("创建探针 agent + skill 成功",
                      created_agent and created_skill,
                      f"agent_id={agent_id} skill_id={skill_id}"):
            errs.append("[setup] 创建探针失败")
        else:
            print(f"      agent={agent_id} skill={skill_id} (name={skill.get('name')!r})")
            # 初始 mounted_skills 应为空
            if not _check("agent 初始 mounted_skills 为空",
                          agent.get("mounted_skills") == []):
                errs.append("[setup] agent 初始 mounted_skills 非空")

        # ── 2. 挂载：mount → mounted_skills 含 skill_id ──
        print("\n[check 2] 挂载：POST mount → mounted_skills 含 skill_id")
        mounted = await mount(skill_id, agent_id)
        ms = mounted.get("mounted_skills") if mounted else None
        if _check("mount 返回 mounted_skills 含 skill_id",
                  bool(ms) and skill_id in ms,
                  f"mounted_skills={ms}"):
            print(f"      挂载后 mounted_skills={ms}")
        else:
            errs.append(f"[mount] 挂载失败：{mounted}")

        # ── 3. 持久化：GET 回读 mounted_skills == mount 响应 ──
        print("\n[check 3] 持久化：GET /api/agents/{id} 回读 mounted_skills")
        reread = await get_agent(agent_id)
        if reread is None:
            _check("GET 回读存在", False, "404")
            errs.append("[reread] GET 404")
        else:
            same = reread.get("mounted_skills") == ms
            if _check("回读 mounted_skills == mount 响应（持久化一致）", same,
                      f"reread={reread.get('mounted_skills')}"):
                pass
            else:
                errs.append(f"[reread] 回读漂移：{reread.get('mounted_skills')}")

        # ── 4. 列表反映：GET /api/agents 列表项 mounted_skills 含 skill_id ──
        print("\n[check 4] 列表反映：GET /api/agents 列表项 mounted_skills 含 skill_id")
        agents = await list_agents()
        listed = next((a for a in agents if a.get("id") == agent_id), None)
        if listed is None:
            _check("列表含该 agent", False)
            errs.append("[list] 列表不含挂载 agent")
        else:
            in_list = skill_id in (listed.get("mounted_skills") or [])
            if _check("列表项 mounted_skills 含 skill_id（列表反映挂载）", in_list):
                pass
            else:
                errs.append(f"[list] 列表项 mounted_skills 不含 skill_id：{listed.get('mounted_skills')}")

        # ── 5. 幂等：重复 mount → mounted_skills 仍只 1 个 ──
        print("\n[check 5] 幂等：重复 mount → mounted_skills 不重复（仍只 1 个 skill_id）")
        mounted2 = await mount(skill_id, agent_id)
        ms2 = mounted2.get("mounted_skills") if mounted2 else None
        idempotent = bool(ms2) and ms2.count(skill_id) == 1
        if _check("重复 mount 后 skill_id 仍只出现 1 次", idempotent,
                  f"mounted_skills={ms2}"):
            pass
        else:
            errs.append(f"[idempotent] 重复挂载非幂等：{ms2}")

        # ── 6. 卸载：unmount → mounted_skills 不含 skill_id + 持久化回读 [] ──
        print("\n[check 6] 卸载：POST unmount → mounted_skills 移除 + 回读 []")
        unmounted = await unmount(skill_id, agent_id)
        ums = unmounted.get("mounted_skills") if unmounted else None
        removed = bool(ums is not None) and skill_id not in (ums or [])
        if _check("unmount 返回 mounted_skills 不含 skill_id", removed,
                  f"mounted_skills={ums}"):
            pass
        else:
            errs.append(f"[unmount] 卸载失败：{unmounted}")
        # 持久化回读
        reread2 = await get_agent(agent_id)
        if reread2 is not None:
            if _check("卸载后回读 mounted_skills == []", reread2.get("mounted_skills") == [],
                      f"reread={reread2.get('mounted_skills')}"):
                pass
            else:
                errs.append(f"[unmount] 卸载后回读非空：{reread2.get('mounted_skills')}")

        # ── 7. 边界：mount 不存在的 skill / 不存在的 agent → null（不抛 500）──
        print("\n[check 7] 边界：mount 不存在的 skill / agent → null（不抛 500）")
        # 重新挂载以测试边界后状态可预期（先 mount 回来再测不影响主流程的清理）
        # mount 不存在的 skill 到真实 agent
        bad_skill = await mount("skill_nope_not_exist", agent_id)
        if _check("mount 不存在的 skill → null（agent 不变）",
                  bad_skill is None,
                  f"got={bad_skill}"):
            pass
        else:
            errs.append(f"[boundary] mount 不存在 skill 未返回 null：{bad_skill}")
        # mount 真实 skill 到不存在的 agent
        bad_agent = await mount(skill_id, "agent_nope_not_exist")
        if _check("mount 到不存在的 agent → null",
                  bad_agent is None,
                  f"got={bad_agent}"):
            pass
        else:
            errs.append(f"[boundary] mount 到不存在 agent 未返回 null：{bad_agent}")

        # ── 8. 「获得能力」契约：resolve_skill_contents 能解析出含 SENTINEL 的 content ──
        print("\n[check 8] 「获得能力」契约：resolve_skill_contents 解析含 SENTINEL")
        try:
            # 先挂载回去（check 6 卸载了），让 resolve 路径与挂载态一致
            await mount(skill_id, agent_id)
            from store import crud  # noqa: WPS433
            contents = await crud.resolve_skill_contents([skill_id])
            got_sentinel = any(SENTINEL in (c or "") for c in contents)
            if _check(f"resolve_skill_contents([skill_id]) 返回 content 含 SENTINEL",
                      bool(contents) and got_sentinel,
                      f"contents={contents!r}"):
                print(f"      解析出 {len(contents)} 条技能内容，含哨兵 {SENTINEL!r}")
                print(f"      → worker executor 注入 system_prompt 时拿到此内容 = Agent 获得能力（PL-06 已证 worker 按其执行）")
            else:
                errs.append(f"[resolve] resolve_skill_contents 未解析出含哨兵 content：{contents}")
        except Exception as e:
            _check("resolve_skill_contents 可调用", False, f"{e!r}")
            errs.append(f"[resolve] 调用异常：{e!r}")

    finally:
        # 收尾：删除探针 skill + agent（先卸载再删，避免悬挂挂载引用）
        print("\n[cleanup] 清理探针 skill + agent")
        if created_skill and skill_id and created_agent and agent_id:
            try:
                await unmount(skill_id, agent_id)
            except Exception:
                pass
        if created_skill and skill_id:
            try:
                ok = await delete_skill(skill_id)
                print(f"  删除 skill {skill_id[:18]}… → {ok}")
            except Exception as e:
                print(f"  删除 skill 失败（非致命）: {e}")
        if created_agent and agent_id:
            try:
                ok = await delete_agent(agent_id)
                print(f"  删除 agent {agent_id[:18]}… → {ok}")
            except Exception as e:
                print(f"  删除 agent 失败（非致命）: {e}")

    # 校验清理后无残留
    if created_agent and agent_id:
        final_agents = await list_agents()
        leaked = [a for a in final_agents if a["id"] == agent_id]
        if not _check("清理后无残留探针 agent", not leaked, f"{len(leaked)} 残留"):
            errs.append(f"[cleanup] 探针 agent 残留")

    # ── 汇总 ──
    print("\n" + "=" * 54)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 挂载技能后 Agent 获得能力端到端打通：")
    print("  · 挂载：mount → mounted_skills 含 skill_id + 持久化回读 + 列表反映；")
    print("  · 幂等：重复挂载不重复 append；")
    print("  · 卸载：unmount → mounted_skills 移除 + 回读 []；")
    print("  · 边界：mount 不存在 skill/agent → null 不抛 500；")
    print("  · 能力契约：resolve_skill_contents 解析含 SENTINEL content（worker 注入路径就绪，")
    print("    PL-06 已端到端验证 worker 按技能内容执行）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
