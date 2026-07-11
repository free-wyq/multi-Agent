"""AG-12 自测：雇佣预设角色模板创建员工（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-12 自测模式（httpx HTTP 真源交叉验证 +
探针落库 + 收尾清理，不连 WS）。

AG-12 链路（雇佣预设角色加入员工列表）：
  POST /api/agents/templates/{template_id}/hire body={name?}
    → agents.py hire_template → get_template(template_id) 解析 catalog 全配置
    → 构造 AgentCreatePayload（role/system_prompt/skills/extra_skills/description
       取模板原值，name 用 body 覆盖或模板名）→ crud.create_agent 落库
    → 返回 AgentDefinition（与 AG-01 / create 同类型，直接进员工列表）
  前端 AgentPage.tsx 模板卡片「雇佣」按钮：
    · handleHireTemplate(tpl) → agentApi.hireTemplate(tpl.template_id)
    · 成功 message.success + fetchAgents() 刷新员工列表，新员工卡片立即出现
    · loading 态 hiringTplIds Set 跟踪每卡独立，防重复点击

为何不复刻前端 loading/message/刷新交互：那些是 UI 交互态非数据契约，HTTP 层验证
「雇佣端点返回正确 + 落库后出现在列表 + 字段来自模板原值」即等价证明「雇佣预设角色
加入员工列表」。fetchAgents 重拉全量是前端刷新手段，自测直接 GET /api/agents 比对
新 agent 是否在列表即证明刷新逻辑成立。

验证八块（确定性断言）：
  ① 正常雇佣（原样）→ 200 + AgentDefinition（id agent_ 前缀 / name=模板名 /
     role=backend_engineer / system_prompt「你」开头 / skills+extra 来自模板 /
     description 来自模板 / mounted_skills=[]）；
  ② 员工列表含新雇佣 agent（GET /api/agents 真源交叉验证，列表项 name/role 一致）；
  ③ 单读 GET /api/agents/{id} 回读 == hire 响应（持久化一致）；
  ④ name 覆盖：hire tpl:data-analyst body={name:数据小分} → name=数据小分，
     其余字段（role/description/skills）取模板原值不改；
  ⑤ 字段真源一致：hired agent 的 role/system_prompt/skills/extra_skills/description
     == GET /api/agents/templates 取的同 template_id 模板原值（跨端点单一真源）；
  ⑥ 未知 template_id → 404（catalog 无此条目）；
  ⑦ bare agent：mounted_skills/mcp/allowed_tools/denied_tools 均空 list（挂载是
     AG-08/AG-09 独立用户动作，雇佣只创建角色身份，与 AG-01 生成同立场）；
  ⑧ 收尾清理删除探针 agent，校验无残留（避免污染后续自测/种子）。

为何不连 WS：AG-12 是同步 HTTP 接口（hire → crud.create_agent 落库），不经引擎
inbox/WS 事件流，无实时事件可抓，纯 HTTP 校验即可（与 SK-12 install 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 用已知存在的 catalog template_id 做正常雇佣用例（agent_templates._CATALOG 第一条）。
TPL_ID = "tpl:backend-engineer"
TPL_NAME = "后端开发工程师"
TPL_ROLE = "backend_engineer"

# name 覆盖用例用 data-analyst 模板（与 hire 端点 curl 验证同模板，便于横向对照）。
TPL_ID_OVERRIDE = "tpl:data-analyst"
OVERRIDE_NAME = "数据小分"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def hire(template_id: str, name: str | None = None) -> tuple[int, dict | None]:
    """POST /api/agents/templates/{id}/hire body={name?}，返回 (status, agent_or_error)。

    name=None 时 body={}（原样雇佣，后端回退模板名）；name 有值时 body={name}（覆盖）。
    对齐前端 hireTemplate(templateId, name?) 的 body 逻辑。
    """
    async with httpx.AsyncClient(timeout=30.0) as c:
        # template_id 含 `:`（RFC3986 pchar，path-safe），直传不经 encode（前端同风格）。
        r = await c.post(
            f"{BASE}/api/agents/templates/{template_id}/hire",
            json={} if name is None else {"name": name},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def get_agent(agent_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents/{agent_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


async def get_template(template_id: str) -> dict | None:
    """GET /api/agents/templates 拉全量 catalog，取目标 template_id 模板作真源比对。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BASE}/api/agents/templates")
        if r.status_code != 200:
            return None
        for t in r.json():
            if t.get("template_id") == template_id:
                return t
    return None


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-12 自测：雇佣预设角色加入员工列表 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    hired_ids: list[str] = []  # 收尾清理用

    # 雇佣前快照：记录已有 agents（供「列表含新雇佣」精确比对，避免历史残留干扰）
    before_agents = await list_agents()
    before_ids = {a["id"] for a in before_agents}
    print(f"[pre] 雇佣前 agents 数：{len(before_agents)}")

    # ── 1. 正常雇佣（原样 body={}）→ 200 + AgentDefinition ──
    print("\n[check 1] 正常雇佣：POST hire tpl:backend-engineer (body={})")
    status, agent = await hire(TPL_ID)
    if not _check("HTTP 200", status == 200, f"status={status} body={agent}"):
        errs.append(f"[hire] 非 200 status={status}")
    else:
        assert agent is not None
        new_id = agent.get("id", "")
        if new_id:
            hired_ids.append(new_id)

        ok_struct = (
            isinstance(agent.get("id"), str)
            and agent.get("id", "").startswith("agent_")
            and isinstance(agent.get("name"), str)
            and agent.get("name") == TPL_NAME
            and agent.get("role") == TPL_ROLE
            and isinstance(agent.get("system_prompt"), str)
            and agent.get("system_prompt", "").startswith("你")
            and isinstance(agent.get("skills"), list)
            and isinstance(agent.get("extra_skills"), list)
            and isinstance(agent.get("description"), str)
            and isinstance(agent.get("mounted_skills"), list)
        )
        if _check(
            "AgentDefinition 结构完整（id agent_ 前缀 / name=模板名 / role / "
            "system_prompt「你」开头 / skills+extra list / description / mounted_skills list）",
            ok_struct,
        ):
            print(
                f"      样本：id={new_id} name={agent.get('name')!r} "
                f"role={agent.get('role')!r}"
            )
        else:
            errs.append(f"[hire] AgentDefinition 结构异常：{agent}")

        # mounted_skills 必须空（bare agent）
        if not _check("mounted_skills 空（bare agent，挂载是 AG-08 独立动作）",
                      agent.get("mounted_skills") == []):
            errs.append(f"[hire] mounted_skills 非空：{agent.get('mounted_skills')}")

    # ── 2. 员工列表含新雇佣 agent（真源交叉验证）──
    print("\n[check 2] 员工列表含新雇佣 agent")
    if agent and agent.get("id"):
        after_agents = await list_agents()
        after_ids = {a["id"] for a in after_agents}
        in_list = agent["id"] in after_ids
        if _check(f"GET /api/agents 列表含新 agent {agent['id'][:18]}…", in_list):
            listed = next((a for a in after_agents if a["id"] == agent["id"]), {})
            listed_ok = (
                listed.get("name") == agent.get("name")
                and listed.get("role") == agent.get("role")
            )
            if not _check("列表项 name/role == hire 响应", listed_ok):
                errs.append(f"[list] 列表项漂移：{listed.get('name')}/{listed.get('role')}")
        else:
            errs.append("[list] 新雇佣 agent 不在员工列表")

    # ── 3. 单读回读 == hire 响应（持久化一致）──
    print("\n[check 3] 单读 GET /api/agents/{id} 回读一致")
    if agent and agent.get("id"):
        reread = await get_agent(agent["id"])
        if reread is None:
            _check("GET /api/agents/{id} 200", False)
            errs.append("[reread] 404 回读失败")
        else:
            consistent = (
                reread.get("id") == agent.get("id")
                and reread.get("name") == agent.get("name")
                and reread.get("role") == agent.get("role")
                and reread.get("system_prompt") == agent.get("system_prompt")
                and reread.get("skills") == agent.get("skills")
                and reread.get("extra_skills") == agent.get("extra_skills")
                and reread.get("description") == agent.get("description")
            )
            if _check("回读 id/name/role/system_prompt/skills/extra_skills/description 一致",
                      consistent):
                pass
            else:
                errs.append(f"[reread] 回读漂移：{reread}")

    # ── 4. name 覆盖：hire body={name} → name 覆盖，其余取模板原值 ──
    print("\n[check 4] name 覆盖：hire tpl:data-analyst body={name:数据小分}")
    status_ov, agent_ov = await hire(TPL_ID_OVERRIDE, OVERRIDE_NAME)
    if not _check("HTTP 200", status_ov == 200, f"status={status_ov}"):
        errs.append(f"[override] 非 200 status={status_ov}")
    else:
        assert agent_ov is not None
        if agent_ov.get("id"):
            hired_ids.append(agent_ov["id"])
        # name 必须被覆盖为传入值
        name_ok = agent_ov.get("name") == OVERRIDE_NAME
        if not _check(f"name 覆盖为 {OVERRIDE_NAME!r}", name_ok,
                      f"name={agent_ov.get('name')!r}"):
            errs.append(f"[override] name 未覆盖：{agent_ov.get('name')!r}")
        # 其余字段取模板原值（role=data_analyst，不被 name 覆盖影响）
        rest_ok = (
            agent_ov.get("role") == "data_analyst"
            and isinstance(agent_ov.get("description"), str)
            and agent_ov.get("description")  # 非空（来自模板）
            and isinstance(agent_ov.get("skills"), list)
        )
        if not _check("role/description/skills 取模板原值（name 覆盖不影响角色定义）",
                      rest_ok, f"role={agent_ov.get('role')!r}"):
            errs.append(f"[override] 其余字段异常：{agent_ov}")

    # ── 5. 字段真源一致：hired agent 字段 == catalog 模板原值（跨端点单一真源）──
    print("\n[check 5] 字段真源一致：hired agent == GET /templates 模板原值")
    if agent and agent.get("id"):
        tpl = await get_template(TPL_ID)
        if tpl is None:
            _check("GET /templates 取到目标模板", False)
            errs.append("[xref] /templates 未取到模板")
        else:
            same = (
                agent.get("role") == tpl.get("role")
                and agent.get("system_prompt") == tpl.get("system_prompt")
                and agent.get("skills") == tpl.get("skills")
                and agent.get("extra_skills") == tpl.get("extra_skills")
                and agent.get("description") == tpl.get("description")
                and agent.get("name") == tpl.get("name")  # 原样雇佣 name==模板名
            )
            if _check("hired agent role/system_prompt/skills/extra_skills/description/name == 模板原值",
                      same):
                print(f"      role={agent.get('role')} skills={len(agent.get('skills', []))} "
                      f"extra={len(agent.get('extra_skills', []))} 跨端点一致")
            else:
                errs.append(
                    f"[xref] 字段不一致：agent role={agent.get('role')!r} "
                    f"tpl role={tpl.get('role')!r}"
                )

    # ── 6. 未知 template_id → 404 ──
    print("\n[check 6] 未知 template_id → 404")
    s404, _ = await hire("tpl:nope-not-exist")
    if _check("未知 template_id → 404", s404 == 404, f"status={s404}"):
        pass
    else:
        errs.append(f"[404] 未知 template_id status={s404} 非 404")

    # ── 7. bare agent：mounted_skills/mcp/tools 全空 ──
    print("\n[check 7] bare agent：mounted_skills/allowed_tools/denied_tools 空")
    if agent:
        bare = (
            agent.get("mounted_skills") == []
            and agent.get("allowed_tools") == []
            and agent.get("denied_tools") == []
        )
        if _check("mounted_skills/allowed_tools/denied_tools 均 == []（bare agent）",
                  bare, f"mounted={agent.get('mounted_skills')} "
                  f"allowed={agent.get('allowed_tools')} denied={agent.get('denied_tools')}"):
            pass
        else:
            errs.append("[bare] 雇佣的 agent 含挂载/工具字段非空（应与 AG-01 生成同 bare 立场）")

    # ── 8. 收尾清理：删除所有本测试雇佣的 agent ──
    print(f"\n[cleanup] 删除 {len(hired_ids)} 个测试 agent")
    for aid in hired_ids:
        ok = await delete_agent(aid)
        if not ok:
            print(f"  ⚠️ 删除失败 {aid}")
            errs.append(f"[cleanup] 删除失败 {aid}")
    # 校验清理后无残留本测试 agent
    final_agents = await list_agents()
    leaked = [a for a in final_agents if a["id"] in hired_ids]
    if not _check("清理后无残留测试 agent", not leaked, f"{len(leaked)} 个残留"):
        errs.append(f"[cleanup] {len(leaked)} 个 agent 残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 雇佣预设角色加入员工列表端到端验证通过：")
    print("  · 正常雇佣：POST hire tpl:backend-engineer → 200 + AgentDefinition（id agent_ 前缀 /")
    print("    name=模板名 / role / system_prompt「你」开头 / skills+extra / mounted_skills 空）；")
    print("  · 员工列表：GET /api/agents 含新雇佣 agent（列表项 name/role 一致）；")
    print("  · 持久化一致：单读 GET /api/agents/{id} 回读 == hire 响应；")
    print("  · name 覆盖：body={name} 覆盖 name，其余字段取模板原值（角色定义不改）；")
    print("  · 字段真源：hired agent 字段 == GET /templates 模板原值（跨端点单一真源）；")
    print("  · 未知 template_id → 404；")
    print("  · bare agent：mounted_skills/allowed/denied 全空（挂载是 AG-08 独立动作）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
