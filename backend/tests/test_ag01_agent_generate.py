"""AG-01 自测：自然语言描述生成完整智能体配置（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-01/SK-12 自测模式（httpx HTTP 真源交叉验证）。

AG-01 链路：
  POST /api/agents/generate {description}
    → agents.py generate_agent
    → _generate_agent_via_llm：调 chat_completion（build_agent_generate_prompt 要求 LLM 按
      {name, role, system_prompt, skills, extra_skills, description} 纯 JSON 回复）→ extract_json 解析
    → _sanitize_role：role 规整为 snake_case（[^a-z0-9]+→_ + strip + 空串兜底 custom_agent）
    → crud.create_agent 持久化（mounted_skills/mcp/tools 全空——挂载是 AG-08 独立动作）
    → 返回 AgentDefinition

「真生成」vs「fallback 降级」的确定性判据：
  fallback（LLM 失败/JSON 解析失败）返回 role="custom_agent" + system_prompt="" + skills=[] +
  extra_skills=[] + name=description[:32] + description=description[:200]。
  真生成则 system_prompt 非空（以「你是」开头）+ skills 非空 list + role 是 snake_case 英文。
  故「system_prompt 非空且以「你」开头 + skills 非空」是「LLM 真生成」的最强确定性证据，
  与 SK-01「content 含 # 标题区分 fallback 裸文本」同思路。

验证八块（确定性断言非语义判断）：
  ① POST /api/agents/generate 带具体描述 → 200 + AgentDefinition 结构完整
     （id agent_ 前缀 / name / role / system_prompt / skills / extra_skills / description）；
  ② role 是 snake_case 英文（匹配 ^[a-z0-9_]+$）——证明 _sanitize_role 生效；
  ③ system_prompt 非空且以「你」开头——证明 LLM 真生成（非 fallback 空串）；
  ④ skills 是非空 list[str]（3-5 个）——证明 LLM 真生成（fallback 为空 []）；
  ⑤ extra_skills 是 list（可空，类型校验）；
  ⑥ description 非空（一句话定位）；
  ⑦ mounted_skills/mounted_mcp/allowed_tools/denied_tools 全空——证明生成不含挂载（AG-08 独立动作）；
  ⑧ system_prompt 语义相关（含描述关键词 ≥1）——证明针对描述生成非万能模板；
  ⑨ 持久化交叉验证：GET /api/agents/{id} 回读 == 生成响应 + GET /api/agents 列表含该 agent；
  ⑩ 边界：空 description → 400。

为何不连 WS：AG-01 是同步 HTTP 接口（generate 内部 await LLM 完成才返回），不经过引擎
inbox/WS 事件流，无实时事件可抓，纯 HTTP 校验即可（与 SK-01/SK-12 同构，比 PL 系列更简单）。

收尾：DELETE /api/agents/{id} 清理，避免污染后续自测（AG-05/06/08 会 list 智能体计数）。
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx

BASE = "http://localhost:8000"

# 描述带可识别关键词（数据/SQL/报表/分析/pandas/PostgreSQL），便于校验生成内容语义相关。
# 选「数据分析师」这类具体角色而非泛泛「一个智能体」，迫使 LLM 产出针对性配置
# （role=data_analyst / skills 含 SQL/报表 等），万能模板无法通过关键词校验。
DESC = (
    "一个负责数据清洗、SQL 查询和报表生成的数据分析师，"
    "熟悉 Python pandas 和 PostgreSQL，能从原始数据提炼业务洞察。"
)

# 描述里的关键词——生成的 system_prompt 应语义相关（含至少一个），证明非万能模板。
DESC_KEYWORDS = ["数据", "SQL", "报表", "分析", "pandas", "PostgreSQL", "洞察"]

# snake_case 英文 role 正则（_sanitize_role 的输出契约）。
ROLE_RE = re.compile(r"^[a-z0-9_]+$")

# LLM 调用可能较慢（生成配置），给足超时。
GEN_TIMEOUT = 120.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def generate_agent(description: str) -> tuple[int, dict | None]:
    """POST /api/agents/generate body={description}，返回 (status, agent_or_error)。"""
    async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/agents/generate",
            json={"description": description},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


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


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== AG-01 自测：自然语言描述生成完整智能体配置 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # 生成前快照：记录已有 agents（供「列表含新 agent」精确比对，避免历史残留干扰）
    before_agents = await list_agents()
    before_ids = {a["id"] for a in before_agents}
    print(f"[pre] 生成前 agents 数：{len(before_agents)}")

    # ── 生成 ──
    print("\n[generate] POST /api/agents/generate 带自然语言描述")
    status, agent = await generate_agent(DESC)
    if not _check("HTTP 200", status == 200, f"status={status} body={agent}"):
        errs.append(f"[generate] 非 200 status={status}")
        # 无法继续，直接收尾
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1

    assert agent is not None
    agent_id = agent.get("id", "")
    created = False
    try:
        # ── 1. AgentDefinition 结构完整 ──
        print("\n[check 1] AgentDefinition 结构完整")
        ok_struct = (
            isinstance(agent.get("id"), str)
            and agent.get("id", "").startswith("agent_")
            and isinstance(agent.get("name"), str)
            and bool(agent.get("name"))
            and isinstance(agent.get("role"), str)
            and bool(agent.get("role"))
            and isinstance(agent.get("system_prompt"), str)
            and isinstance(agent.get("skills"), list)
            and isinstance(agent.get("extra_skills"), list)
            and isinstance(agent.get("description"), str)
        )
        if _check("结构完整（id agent_ 前缀 / name / role / system_prompt / skills / extra_skills / description）",
                  ok_struct):
            print(f"      样本：id={agent_id} name={agent.get('name')!r} role={agent.get('role')!r}")
        else:
            errs.append(f"[struct] AgentDefinition 结构异常：{agent}")

        # ── 2. role 是 snake_case 英文 ──
        print("\n[check 2] role 是 snake_case 英文（_sanitize_role 生效）")
        role = agent.get("role", "")
        if _check(f"role 匹配 ^[a-z0-9_]+$（role={role!r}）", bool(ROLE_RE.match(role))):
            pass
        else:
            errs.append(f"[role] role 非 snake_case：{role!r}")

        # ── 3. system_prompt 非空且以「你」开头（真生成 vs fallback 判据）──
        print("\n[check 3] system_prompt 非空且以「你」开头（真生成证据）")
        sp = agent.get("system_prompt", "") or ""
        if not _check("system_prompt 非空", bool(sp), "为空（疑似 fallback）"):
            errs.append("[system_prompt] 为空，疑似 LLM 失败走 fallback")
        else:
            print(f"      system_prompt 长度 = {len(sp)} 字符")
            print(f"      首句：{sp[:60]!r}")
        if not _check("system_prompt 以「你」开头", sp.startswith("你"),
                      f"首字符={sp[:1]!r}"):
            errs.append(f"[system_prompt] 未以「你」开头：{sp[:20]!r}")

        # ── 4. skills 是非空 list[str]（真生成 vs fallback 判据）──
        print("\n[check 4] skills 是非空 list[str]（真生成证据）")
        skills = agent.get("skills", [])
        ok_skills = (
            isinstance(skills, list)
            and len(skills) >= 1
            and all(isinstance(s, str) and s for s in skills)
        )
        if _check(f"skills 非空 list[str]（{len(skills)} 个）", ok_skills,
                  f"skills={skills!r}"):
            print(f"      skills = {skills}")
        else:
            errs.append(f"[skills] skills 异常：{skills!r}")

        # ── 5. extra_skills 是 list（可空）──
        print("\n[check 5] extra_skills 是 list[str]（可空）")
        extra = agent.get("extra_skills", [])
        ok_extra = (
            isinstance(extra, list)
            and all(isinstance(s, str) for s in extra)
        )
        if _check(f"extra_skills 是 list[str]（{len(extra)} 个）", ok_extra,
                  f"extra_skills={extra!r}"):
            print(f"      extra_skills = {extra}")
        else:
            errs.append(f"[extra_skills] extra_skills 异常：{extra!r}")

        # ── 6. description 非空 ──
        print("\n[check 6] description 非空（一句话定位）")
        desc = agent.get("description", "") or ""
        if _check("description 非空", bool(desc), "为空"):
            print(f"      description = {desc!r}")
        else:
            errs.append("[description] description 为空")

        # ── 7. mounted_skills/mounted_mcp/allowed_tools/denied_tools 全空 ──
        print("\n[check 7] 挂载字段全空（生成不含挂载，AG-08 独立动作）")
        mounted_empty = (
            agent.get("mounted_skills", []) == []
            and agent.get("mounted_mcp", []) == []
            and agent.get("allowed_tools", []) == []
            and agent.get("denied_tools", []) == []
        )
        if _check("mounted_skills/mounted_mcp/allowed_tools/denied_tools 全空",
                  mounted_empty,
                  f"mounted={agent.get('mounted_skills')} mcp={agent.get('mounted_mcp')} "
                  f"allowed={agent.get('allowed_tools')} denied={agent.get('denied_tools')}"):
            pass
        else:
            errs.append("[mounted] 生成时意外填充了挂载字段（应全空，挂载是 AG-08 动作）")

        # ── 8. system_prompt 语义相关（含描述关键词 ≥1）──
        print("\n[check 8] system_prompt 语义相关（含描述关键词 ≥1）")
        if sp:
            hit = [k for k in DESC_KEYWORDS if k.lower() in sp.lower()]
            if _check(f"system_prompt 含描述关键词 ≥1（命中 {hit}）",
                      len(hit) >= 1,
                      f"未命中任何 {DESC_KEYWORDS}"):
                pass
            else:
                errs.append("[relevance] system_prompt 不含描述关键词，疑似万能模板非针对描述生成")

        created = True

        # ── 9. 持久化交叉验证：GET 回读 + 列表含 ──
        print("\n[check 9] 持久化交叉验证：GET /api/agents/{id} 回读 + 列表含")
        if agent_id:
            reread = await get_agent(agent_id)
            if reread is None:
                _check("GET /api/agents/{id} 回读存在", False, "404")
                errs.append(f"[reread] GET /api/agents/{agent_id} 404（未持久化）")
            else:
                same = (
                    reread.get("id") == agent_id
                    and reread.get("name") == agent.get("name")
                    and reread.get("role") == agent.get("role")
                    and reread.get("system_prompt") == agent.get("system_prompt")
                    and reread.get("skills") == agent.get("skills")
                    and reread.get("extra_skills") == agent.get("extra_skills")
                    and reread.get("description") == agent.get("description")
                )
                if _check("GET 回读 id/name/role/system_prompt/skills/extra_skills/description 严格一致",
                          same):
                    print("[check 9] 持久化回读一致")
                else:
                    errs.append(f"[reread] 回读漂移：{reread}")

            after_agents = await list_agents()
            after_ids = {a["id"] for a in after_agents}
            in_list = agent_id in after_ids
            if _check(f"GET /api/agents 列表含新 agent（共 {len(after_agents)} 项）",
                      in_list,
                      f"列表 {len(after_ids)} 项不含 {agent_id[:18]}…"):
                pass
            else:
                errs.append("生成后的 agent 未出现在 /api/agents 列表中")

    finally:
        # 收尾清理：删除生成的 agent，避免污染后续自测（AG-05/06/08 会 list 智能体计数）
        if created and agent_id:
            try:
                ok = await delete_agent(agent_id)
                print(f"[cleanup] 删除 agent {agent_id[:18]}… → {ok}")
            except Exception as e:
                print(f"[cleanup] 删除失败（非致命）: {e}")

    # ── 10. 边界：空 description → 400 ──
    print("\n[check 10] 边界：空 description → 400")
    s400, _ = await generate_agent("")
    if _check("空 description → 400", s400 == 400, f"status={s400}"):
        pass
    else:
        errs.append(f"[400] 空 description status={s400} 非 400")

    # ── 校验清理后无残留 ──
    if created and agent_id:
        final_agents = await list_agents()
        leaked = [a for a in final_agents if a["id"] == agent_id]
        if not _check("清理后无残留测试 agent", not leaked, f"{len(leaked)} 个残留"):
            errs.append(f"[cleanup] agent {agent_id} 残留未删干净")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 自然语言生成完整智能体配置端到端打通：")
    print("  · 结构完整 / role snake_case / system_prompt 真生成 / skills 非空 /")
    print("    挂载全空 / 语义相关 / 持久化回读 + 列表含 / 空描述 400 全过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
