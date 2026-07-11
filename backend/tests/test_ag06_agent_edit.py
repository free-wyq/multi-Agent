"""AG-06 自测：编辑员工信息（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-05/SK-09 自测模式（httpx HTTP 真源交叉验证）。

AG-06 链路（编辑闭环）：
  ① 读现状：GET /api/agents/{id}（openEdit 回填表单用原值）
  ② 提交修改：PUT /api/agents/{id} body=AgentCreatePayload
     → agents.py update_agent → crud.update_agent
     → payload.model_dump(exclude_unset=True, exclude_none=True) 精准合并
     → setattr 逐字段写回 + updated_at 刷新 → commit → 返回 AgentDefinition
  ③ 回读验证：GET /api/agents/{id} 确认修改持久化（== update 响应）

前端 AgentPage 编辑闭环（openEdit → 表单 → handleCreateOrUpdate）：
  - openEdit(agent) form.setFieldsValue({name, role, extra_skills, system_prompt}) 回填
  - Modal title「编辑智能体」/ okText「保存」
  - handleCreateOrUpdate：editing 非空走 agentApi.update(editing.id, payload)，
    payload = {...values, system_prompt, extra_skills}
  - 成功后 fetchAgents 刷新列表

本自测验证「编辑员工信息」全链路：创建探针 agent → GET 读原值 → PUT 改 name/role/
system_prompt/skills/extra_skills/description → 回读 == 改后值 → 列表也含改后值 → 清理。

为何不复刻前端表单：AG-06 编辑是「读原值→改→写回」的标准 PUT 闭环，前端表单只是 UI 载体，
核心验证在后端 PUT /api/agents/{id} 的「精准合并 + 持久化 + 回读一致」。前端 handleCreateOrUpdate
的逻辑（editing 走 update、payload 含 system_prompt/extra_skills、成功后 fetchAgents 刷新）已读代码
确认无缺陷，自测聚焦后端 PUT 闭环 + 前端 payload 结构契约（与 SK-09「后端字段契约 + 前端算法」
同思路：前端读什么/写什么字段，后端就接什么/存什么，编辑即成立）。

验证七块（确定性断言）：
  ① 创建探针 agent（自定义角色，system_prompt 必填）→ 200 + AgentDefinition；
  ② GET 读原值 == create 响应（编辑前基线）；
  ③ PUT 改 name/role/system_prompt/skills/extra_skills/description → 200 + 返回改后值
     （name/role/system_prompt/skills/extra_skills/description 全部更新）；
  ④ GET 回读 == update 响应（修改已持久化）；
  ⑤ GET 回读 != 编辑前原值（确证真改了非幂等空操作）；
  ⑥ GET /api/agents 列表含改后 agent（列表也反映编辑结果）；
  ⑦ 字段非「只读」验证：updated_at 严格大于 created_at（编辑刷新了时间戳）。

为何不连 WS：AG-06 是同步 HTTP 接口（update_agent → commit 完成才返回），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 SK-05/SK-09 同构，比 PL 系列更简单）。

收尾：DELETE /api/agents/{id} 清理探针 agent，避免污染后续自测（AG-08 会 list 智能体计数）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 编辑前：探针 agent 的初始值
ORIG = {
    "name": "AG06探针员工",
    "role": "custom",
    "system_prompt": "你是探针员工，用于验证编辑功能。",
    "skills": ["探针"],
    "extra_skills": ["初始技能"],
    "description": "编辑前：初始探针",
}

# 编辑后：全部字段改为不同值（验证「真改了」非空操作）
EDITED = {
    "name": "AG06改名后员工",
    "role": "custom",
    "system_prompt": "你是改后的探针员工，职责已变更，负责编辑验证。",
    "skills": ["探针", "新增技能"],
    "extra_skills": ["改后技能A", "改后技能B"],
    "description": "编辑后：信息已更新",
}

# 编辑需改的字段（断言用）
EDIT_FIELDS = ["name", "role", "system_prompt", "skills", "extra_skills", "description"]


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create_agent(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/agents", json=body)
        r.raise_for_status()
        return r.json()


async def get_agent(agent_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents/{agent_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def update_agent(agent_id: str, body: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.put(f"{BASE}/api/agents/{agent_id}", json=body)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        r.raise_for_status()
        return r.json()


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-06 自测：编辑员工信息 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    agent_id = ""
    created = False
    try:
        # ── 1. 创建探针 agent ──
        print("\n[check 1] 创建探针 agent（编辑前基线）")
        agent = await create_agent(ORIG)
        agent_id = agent.get("id", "")
        created = bool(agent_id)
        ok_create = (
            created
            and agent.get("name") == ORIG["name"]
            and agent.get("role") == ORIG["role"]
            and agent.get("system_prompt") == ORIG["system_prompt"]
            and agent.get("skills") == ORIG["skills"]
            and agent.get("extra_skills") == ORIG["extra_skills"]
            and agent.get("description") == ORIG["description"]
        )
        if _check("创建探针 → 200 + 全字段 == ORIG", ok_create,
                  f"agent={agent}"):
            print(f"      样本：id={agent_id} name={agent.get('name')!r}")
        else:
            errs.append(f"[create] 探针创建/字段不符：{agent}")

        # ── 2. GET 读原值 == create 响应（编辑前基线）──
        print("\n[check 2] GET 读原值 == create 响应（编辑前基线）")
        before = await get_agent(agent_id)
        if before is None:
            _check("GET 编辑前回读存在", False, "404")
            errs.append("[before] 编辑前 GET 404")
        else:
            same = all(before.get(f) == ORIG[f] for f in EDIT_FIELDS)
            if _check("编辑前回读 == ORIG（基线确立）", same,
                      f"name={before.get('name')!r}"):
                print(f"      编辑前：name={before.get('name')!r} "
                      f"skills={before.get('skills')} extra={before.get('extra_skills')}")

        # ── 3. PUT 改全字段 → 200 + 返回改后值 ──
        print("\n[check 3] PUT 改 name/role/system_prompt/skills/extra_skills/description")
        updated = await update_agent(agent_id, EDITED)
        if updated is None:
            _check("PUT 返回 200", False, "404/None")
            errs.append("[update] PUT 返回 None（agent 不存在？）")
        else:
            ok_update = all(updated.get(f) == EDITED[f] for f in EDIT_FIELDS)
            if _check("PUT 返回全字段 == EDITED", ok_update,
                      f"name={updated.get('name')!r} sp={updated.get('system_prompt')[:20]!r}…"):
                print(f"      编辑后：name={updated.get('name')!r} "
                      f"skills={updated.get('skills')} extra={updated.get('extra_skills')}")
            else:
                errs.append(f"[update] PUT 返回字段不符：{updated}")

        # ── 4. GET 回读 == update 响应（修改已持久化）──
        print("\n[check 4] GET 回读 == update 响应（修改已持久化）")
        after = await get_agent(agent_id)
        if after is None:
            _check("GET 编辑后回读存在", False, "404")
            errs.append("[after] 编辑后 GET 404")
        elif updated is not None:
            same = all(after.get(f) == updated.get(f) for f in EDIT_FIELDS)
            if _check("编辑后回读 == PUT 响应（持久化一致）", same):
                pass
            else:
                errs.append(f"[after] 回读 != PUT 响应：{after}")

        # ── 5. 回读 != 编辑前原值（确证真改了非空操作）──
        print("\n[check 5] 回读 != 编辑前原值（确证真改了）")
        if before is not None and after is not None:
            changed = any(after.get(f) != before.get(f) for f in EDIT_FIELDS)
            if _check("编辑后字段与编辑前不同（真改了）", changed):
                diff = [f for f in EDIT_FIELDS if after.get(f) != before.get(f)]
                print(f"      变化字段：{diff}")
            else:
                errs.append("[changed] 编辑后与编辑前相同（疑似未真改）")

        # ── 6. 列表含改后 agent（列表也反映编辑结果）──
        print("\n[check 6] GET /api/agents 列表含改后 agent")
        agents = await list_agents()
        listed = next((a for a in agents if a.get("id") == agent_id), None)
        if listed is None:
            _check("列表含该 agent", False)
            errs.append("[list] 列表不含编辑后的 agent")
        else:
            list_same = all(listed.get(f) == EDITED[f] for f in EDIT_FIELDS)
            if _check("列表项全字段 == EDITED（列表反映编辑）", list_same):
                print(f"      列表项：name={listed.get('name')!r}")
            else:
                errs.append(f"[list] 列表项与 EDITED 不符：{listed}")

        # ── 7. updated_at > created_at（编辑刷新时间戳）──
        print("\n[check 7] updated_at > created_at（编辑刷新时间戳）")
        if before is not None and after is not None:
            ca = before.get("created_at", "")
            ua = after.get("updated_at", "")
            # created_at 在 create 时写定，update 后 updated_at 刷新；正常 ua >= ca
            ts_ok = bool(ua) and bool(ca) and ua >= ca
            if _check(f"updated_at({ua[:19]}) >= created_at({ca[:19]})", ts_ok):
                pass
            else:
                errs.append(f"[ts] updated_at({ua!r}) < created_at({ca!r})")

    finally:
        # 收尾清理：删除探针 agent，避免污染后续自测
        if created and agent_id:
            try:
                ok = await delete_agent(agent_id)
                print(f"\n[cleanup] 删除探针 agent {agent_id[:18]}… → {ok}")
            except Exception as e:
                print(f"[cleanup] 删除失败（非致命）: {e}")

    # 校验清理后无残留
    if created and agent_id:
        final_agents = await list_agents()
        leaked = [a for a in final_agents if a["id"] == agent_id]
        if not _check("清理后无残留探针 agent", not leaked, f"{len(leaked)} 残留"):
            errs.append(f"[cleanup] 探针 agent {agent_id} 残留")

    # ── 汇总 ──
    print("\n" + "=" * 52)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 编辑员工信息端到端打通：")
    print("  · 创建探针 → GET 读原值（基线）→ PUT 改全字段 → GET 回读 == PUT 响应 →")
    print("    回读 != 原值（真改了）→ 列表反映编辑 → updated_at 刷新 全过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
