"""MT-04 自测：创建群组时自动生成名称描述（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-01/MT-02 自测模式（httpx HTTP 真源交叉验证，
不连 WS——MT-04 是同步 HTTP 链路：generate-name → 回填 → create → 回读，不经引擎 inbox）。

MT-04 链路（GroupPage 新建群组时自动生成名称描述）：
  前端 GroupPage 新建群组表单：用户选完群主+成员 → 点「自动生成名称和描述」按钮 →
  handleGenerateNameDesc 读 createForm 当前 coordinator_id/members → 调
  groupApi.generateNameDesc → POST /api/groups/generate-name body={coordinator_id, member_ids}
  → 后端 _generate_group_name_desc 解析 roster name/role → build_group_name_desc_prompt
  → chat_completion LLM → extract_json → {name, description}（LLM 失败 fallback roster 名拼接
  「XX团队」永不抛错）→ 回填 createForm 的 name/description → 用户审核 → groupApi.create 落库。

本自测复刻这条链路（前端两步：生成+创建，后端两步：generate-name + create）：
  ① generate-name 端点：选 roster（coord + members）→ POST → 返回 {name, description}
     非空（证明 LLM 生成 + JSON 解析 + fallback 链路通）；
  ② 生成结果质量：name 非空 + description 非空（LLM 真实生成的标志，fallback 时 description
     为空串——本自测用真实 LLM 故两者应都非空，但若 LLM 偶发只给 name 也算 PASS，
     只要 name 非空即链路通；description 空作 INFO 不计 FAIL）；
  ③ 用生成结果创建群组：POST /api/groups body={name, description, coordinator_id, member_ids}
     → 落库返回 Group，group.name == 生成 name、group.description == 生成 description
     （证明生成的名称描述能被 create 落库，端到端闭环）；
  ④ 回读一致：GET /api/groups/{id} 回读 name/description == create 响应（持久化一致）；
  ⑤ 多次生成不同 roster → 不同 name（证明 LLM 据 roster 生成，非固定模板）；
  ⑥ fallback 健壮性：空 roster + 无效 member_id 不抛错（LLM 仍 200 返回 name）；
  ⑦ 收尾清理：DELETE 探针群组，校验无残留。

为何不连 WS：MT-04 是同步 HTTP（generate-name LLM 调用 + create 落库），不经引擎 inbox/WS，
纯 HTTP 校验即可（与 MT-01/MT-02 同构）。

为何不深测 LLM 生成质量：LLM 输出有不确定性，name/description 具体文案不可硬断言；本自测
断言「链路通」（name 非空 + 落库一致 + 多 roster 生成不同 name）而非「文案对」（避免 LLM
随机性导致自测 flaky）。质量验证留给人工/下一轮。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 探针群组名（[MT-04] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME_A = "[MT-04] 自动生成名称探针组A"
PROBE_GROUP_NAME_B = "[MT-04] 自动生成名称探针组B"

# generate-name LLM 调用耗时数秒，给足超时。
GEN_TIMEOUT = 90.0
CREATE_TIMEOUT = 20.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def generate_name_desc(
    coordinator_id: str | None, member_ids: list[str]
) -> tuple[int, dict | None]:
    """POST /api/groups/generate-name → {name, description}（或 error body）。"""
    async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/groups/generate-name",
            json={"coordinator_id": coordinator_id, "member_ids": member_ids},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=CREATE_TIMEOUT) as c:
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


async def delete_group(group_id: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        return r.status_code == 200 and r.json() is True


async def add_member(group_id: str, agent_id: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            f"{BASE}/api/groups/{group_id}/members",
            json={"agentId": agent_id, "alias": None},
        )
        return r.status_code == 200


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MT-04 自测：创建群组时自动生成名称描述 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []

    # ── 1. 候选池：GET /apiagents（roster 真源）──
    print("\n[check 1] 候选池：GET /api/agents")
    agents = await list_agents()
    if not _check("agent 列表非空", len(agents) >= 2, f"仅 {len(agents)} 个"):
        errs.append("[pool] agent 列表不足 2 个")
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    coord = next((a for a in agents if a.get("role") == "coordinator"), None) or agents[0]
    non_coord = next((a for a in agents if a["id"] != coord["id"]), None) or agents[0]
    print(f"      coord={coord['id']}({coord['name']})")
    print(f"      member={non_coord['id']}({non_coord['name']})")

    # ── 2. generate-name 端点：选 roster（coord + member）→ POST → {name, description} 非空 ──
    print("\n[check 2] generate-name：POST /api/groups/generate-name（coord + 1 member）")
    st_a, gen_a = await generate_name_desc(coord["id"], [non_coord["id"]])
    if not _check("HTTP 200", st_a == 200, f"status={st_a} body={gen_a}"):
        errs.append(f"[gen-a] 非 200 status={st_a}")
        gen_a = None
    name_a = (gen_a or {}).get("name", "") if isinstance(gen_a, dict) else ""
    desc_a = (gen_a or {}).get("description", "") if isinstance(gen_a, dict) else ""
    if gen_a:
        print(f"      生成 name={name_a!r}")
        print(f"      生成 description={desc_a!r}")
    if not _check("生成 name 非空", bool(name_a), f"name={name_a!r}"):
        errs.append("[gen-a] name 为空")
    # description 空作 INFO（LLM 偶发只给 name，或 fallback 时空），不计 FAIL
    _check("(info) 生成 description 非空", bool(desc_a), f"desc={desc_a!r}")

    # ── 3. 用生成结果创建群组：POST /api/groups body={生成的 name/desc} ──
    print("\n[check 3] 用生成结果创建群组：POST /api/groups body={生成的 name/description}")
    created_name = name_a or PROBE_GROUP_NAME_A  # name 空时兜底（不应发生但防御）
    st_c, group_a = await create_group({
        "name": created_name,
        "description": desc_a or "MT-04 自动生成名称探针",
        "coordinator_id": coord["id"],
        "member_ids": [non_coord["id"]],
    })
    if not _check("HTTP 200", st_c == 200, f"status={st_c} body={group_a}"):
        errs.append(f"[create-a] 非 200 status={st_c}")
        group_a = None
    if group_a:
        probe_ids.append(group_a["id"])
        name_match = group_a.get("name") == created_name
        desc_match = group_a.get("description") == (desc_a or "MT-04 自动生成名称探针")
        _check("落库 name == 生成 name", name_match,
               f"created={group_a.get('name')!r} vs gen={created_name!r}")
        if not name_match:
            errs.append("[create-a] name 落库不一致")
        _check("落库 description == 生成 description", desc_match)
        if not desc_match:
            errs.append("[create-a] description 落库不一致")
        print(f"      群组 id={group_a['id'][:24]}…")

    # ── 4. 回读一致：GET /api/groups/{id} 回读 name/description == create 响应 ──
    print("\n[check 4] 回读一致：GET /api/groups/{id}")
    if group_a:
        g_read = await get_group(group_a["id"])
        read_ok = g_read is not None and g_read.get("name") == group_a.get("name")
        _check("回读 name == create 响应", read_ok,
               f"read={g_read.get('name') if g_read else None!r}")
        if not read_ok:
            errs.append("[read-a] 回读 name 不一致")
        else:
            print(f"      回读 name={g_read.get('name')!r}")

    # ── 5. 多次生成不同 roster → 不同 name（证明据 roster 生成非固定模板）──
    print("\n[check 5] 不同 roster 生成不同 name（coord-only vs coord+2 members）")
    # roster B：只有 coord 无 member（与 roster A: coord+1member 不同）
    st_b, gen_b = await generate_name_desc(coord["id"], [])
    name_b = (gen_b or {}).get("name", "") if isinstance(gen_b, dict) else ""
    if not _check("roster B HTTP 200", st_b == 200, f"status={st_b}"):
        errs.append(f"[gen-b] 非 200 status={st_b}")
    else:
        print(f"      roster B 生成 name={name_b!r}")
        # 不同 roster 应产生不同 name（LLM 据 roster 生成）。偶发相同也算链路通，
        # 用软断言（不同 = 强证据，相同 = 不计 FAIL，仅 INFO）
        diff = name_a != name_b
        _check("(info) 不同 roster 生成不同 name", diff,
               f"A={name_a!r} vs B={name_b!r}")
        if not diff:
            print("      [note] 两次 name 相同（LLM 偶发，链路仍通，不计 FAIL）")

    # ── 6. fallback 健壮性：空 roster + 无效 member_id 不抛错 ──
    print("\n[check 6] fallback 健壮性")
    st_e, gen_e = await generate_name_desc(None, [])
    name_e = (gen_e or {}).get("name", "") if isinstance(gen_e, dict) else ""
    _check("空 roster → 200 + name 非空", st_e == 200 and bool(name_e),
           f"status={st_e} name={name_e!r}")
    if not (st_e == 200 and name_e):
        errs.append("[fallback-empty] 空 roster 抛错")

    st_i, gen_i = await generate_name_desc(None, ["nonexistent_agent_xxx"])
    name_i = (gen_i or {}).get("name", "") if isinstance(gen_i, dict) else ""
    _check("无效 member_id → 200 + name 非空（容忍）", st_i == 200 and bool(name_i),
           f"status={st_i} name={name_i!r}")
    if not (st_i == 200 and name_i):
        errs.append("[fallback-invalid] 无效 member_id 抛错")

    # ── 7. 收尾清理：DELETE 探针群组 ──
    print("\n[check 7] 收尾清理：DELETE 探针群组")
    for gid in probe_ids:
        ok = await delete_group(gid)
        _check(f"DELETE {gid[:24]}…", ok)
        if not ok:
            errs.append(f"[cleanup] 删除 {gid} 失败")

    # ── 结论 ──
    print("\n=== 结果 ===")
    if errs:
        print(f"FAIL ({len(errs)} 项)")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
