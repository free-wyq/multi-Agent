"""BE-01 自测：POST /api/slash 解析 /tools 返回实际绑定工具。

在线集成测试（httpx 直连已起的后端进程 localhost:8000，与 test_cf_config_endpoint.py
/ test_sa_status_aggregation.py / test_be_reset_session.py 同模式：不发 pytest、
不开 TestClient）。覆盖 BE-01 slash helper 端点契约。

端点契约（backend/api/system.py slash_helper + _slash_tools）：
  POST /api/slash body={command, agent_id?, group_id?}
    command="tools" → _slash_tools(body) 聚合「agent 实际绑定的工具」：
      internal = tools_for_group(group_id) → 内置 5 工具（read_file/write_file/
        edit_file/list_dir/run_command，workspace 无关——closure 仅 invoke 时绑 workspace）
      mcp = list_mcp_tools(agent.mounted_mcp) → 各已挂载 MCP 暴露的工具（langchain-mcp-adapters
        自省，flattened）。agent_id 为空或无 mounted_mcp 时 mcp=[]
      返回 {ok:true, command:"tools", agent_id, group_id, tools:{internal, mcp}, total}
    不支持的 command → {ok:false, command, error}（不抛错，让前端 fallback）

  响应结构（与 src/services/api.ts SlashToolsResult 对齐）：
    { ok: bool, command: str, error?: str, agent_id?: str|null, group_id?: str|null,
      tools: { internal: ToolPreviewItem[], mcp: ToolPreviewItem[] }, total: int }
    ToolPreviewItem = { name: str, description: str }（description 截断 200）

验证（真起后端，不发真 LLM 任务——slash helper 是工具枚举/自省，确定性高）：
  1. POST /api/slash {command:"tools"} 200 + ok=true + command="tools"。
  2. tools 结构：{internal: list, mcp: list}，total = len(internal) + len(mcp)。
  3. internal 工具齐全：含 5 个内置工具（read_file/write_file/edit_file/list_dir/
     run_command），每条 name+description 非空，description ≤ 200 字符（截断）。
  4. 不传 agent_id：mcp=[]（无挂载 MCP 可自省），agent_id 回显 null。
  5. 传 agent_id + group_id：agent_id/group_id 原样回显；internal 仍 5 个（workspace
     无关）；mcp 视该 agent 是否挂了 MCP（挂了则非空，未挂则空——不强断言 mcp 非空，
     因种子 agent 可能未挂 MCP）。
  6. group_id 缺失仍返回 internal roster（内置工具 workspace 无关，closure 仅 invoke
     时绑 workspace，枚举不需要 workspace）。
  7. 不支持的 command → ok=false + error 非空（前端据此 fallback）。
  8. command 前导 / 容忍：传 "tools" 或 "/tools"（lstrip("/") 后等价）均 ok=true。

为何不发真 LLM 任务：
  slash helper 是工具枚举/自省（tools_for_group 同步返回 closure 工具列表 +
  list_mcp_tools 异步自省 MCP），不调 LLM。MCP 自省会 spawn stdio 子进程或连 SSE，
  但种子 agent 通常未挂 MCP（mcp=[] 快速返回），无长耗时。本测聚焦「契约正确 +
  内置工具不漂移」，MCP 真实加载由 MC 系列自测覆盖。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 内置 5 工具（engine/tools.py tools_for_group 固定集合）——校验 internal 不漂移。
_EXPECTED_INTERNAL_TOOLS = {"read_file", "write_file", "edit_file", "list_dir", "run_command"}


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health", timeout=5.0)
        return r.json().get("status") == "ok"


async def slash(command: str, agent_id: str | None = None, group_id: str | None = None) -> dict:
    async with httpx.AsyncClient() as c:
        body: dict = {"command": command}
        if agent_id is not None:
            body["agent_id"] = agent_id
        if group_id is not None:
            body["group_id"] = group_id
        r = await c.post(f"{BASE}/api/slash", json=body, timeout=30.0)
        assert r.status_code == 200, f"POST /api/slash body={body} status={r.status_code} resp={r.text}"
        return r.json()


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/agents", timeout=10.0)
        assert r.status_code == 200, f"GET /api/agents status={r.status_code}"
        return r.json()


def _check_tool_item(item: dict, errs: list[str], prefix: str) -> None:
    """校验单条 ToolPreviewItem：name 非空 str + description str + ≤200 字符。"""
    if not isinstance(item, dict):
        errs.append(f"{prefix} tool item 非 dict：{type(item).__name__}")
        return
    name = item.get("name")
    if not isinstance(name, str) or not name:
        errs.append(f"{prefix} tool name 非空 str：{name!r}")
    desc = item.get("description")
    if not isinstance(desc, str):
        errs.append(f"{prefix} tool {name} description 非 str：{desc!r}({type(desc).__name__})")
    elif len(desc) > 200:
        errs.append(f"{prefix} tool {name} description 未截断（{len(desc)} > 200）")


async def main() -> int:
    print("=== BE-01 自测：POST /api/slash 解析 /tools 返回实际绑定工具 ===")
    if not await health_ok():
        print("[fatal] backend 不在线（localhost:8000 /health 未返 ok）")
        print("        请先起后端：cd backend && python3 -m uvicorn main:app --port 8000")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # ── 步骤 1+2：POST /api/slash {command:"tools"} 结构 ──
    print("\n── 步骤1：POST /api/slash {command:'tools'} 结构 ──")
    resp = await slash("tools")
    print(f"[slash] ok={resp.get('ok')} command={resp.get('command')} total={resp.get('total')}")
    if resp.get("ok") is not True:
        errs.append(f"ok 非 True：{resp}")
    if resp.get("command") != "tools":
        errs.append(f"command 非 'tools'：{resp.get('command')!r}")
    else:
        print("[check 1] ok=true + command='tools'  OK")

    tools = resp.get("tools")
    if not isinstance(tools, dict):
        errs.append(f"tools 非 dict：{type(tools).__name__}")
    else:
        internal = tools.get("internal")
        mcp = tools.get("mcp")
        if not isinstance(internal, list):
            errs.append(f"tools.internal 非 list：{type(internal).__name__}")
        if not isinstance(mcp, list):
            errs.append(f"tools.mcp 非 list：{type(mcp).__name__}")
        total = resp.get("total")
        if isinstance(internal, list) and isinstance(mcp, list):
            if total != len(internal) + len(mcp):
                errs.append(f"total 错误：{total} != internal({len(internal)}) + mcp({len(mcp)})")
            else:
                print(f"[check 2] tools={{internal:[{len(internal)}], mcp:[{len(mcp)}]}} total={total} 一致  OK")

    # ── 步骤 3：internal 工具齐全 + shape ──
    print("\n── 步骤2：internal 内置 5 工具齐全 + shape ──")
    internal = resp.get("tools", {}).get("internal", []) if isinstance(resp.get("tools"), dict) else []
    internal_names = {t.get("name") for t in internal if isinstance(t, dict)}
    missing = _EXPECTED_INTERNAL_TOOLS - internal_names
    extra = internal_names - _EXPECTED_INTERNAL_TOOLS
    if missing:
        errs.append(f"internal 缺工具：{missing}（实际 {internal_names}）")
    if extra:
        errs.append(f"internal 多出未知工具：{extra}（实际 {internal_names}）")
    if not missing and not extra:
        print(f"[check 3] internal 含 5 内置工具 {_EXPECTED_INTERNAL_TOOLS}  OK")
    for t in internal:
        _check_tool_item(t, errs, "internal")

    # ── 步骤 4：不传 agent_id → mcp=[] + agent_id 回显 null ──
    print("\n── 步骤3：不传 agent_id → mcp=[] + agent_id 回显 null ──")
    if isinstance(mcp, list) and len(mcp) == 0:
        print(f"[check 4] 不传 agent_id 时 mcp=[]（无挂载 MCP 可自省）  OK")
    else:
        # mcp 非空说明不传 agent_id 也自省了——契约要求 agent_id 为空时 mcp=[]
        errs.append(f"不传 agent_id 时 mcp 应为空，实际 {len(mcp) if isinstance(mcp, list) else 'N/A'} 项")
    if resp.get("agent_id") is None:
        print("[check 4b] agent_id 回显 null  OK")
    else:
        errs.append(f"不传 agent_id 时 agent_id 应回显 null，实际 {resp.get('agent_id')!r}")

    # ── 步骤 5：传 agent_id + group_id → 回显 + internal 仍 5 ──
    print("\n── 步骤4：传 agent_id + group_id → 回显 + internal 仍 5 ──")
    # 找一个真实存在的 agent（种子 agent_backend_1 / group_demo_1）
    agents = await list_agents()
    agent_ids = [a.get("id") for a in agents if a.get("id")]
    test_agent = "agent_backend_1" if "agent_backend_1" in agent_ids else (agent_ids[0] if agent_ids else None)
    if not test_agent:
        print("[skip] 无 agent 可测，跳过 agent_id 回显断言")
    else:
        resp2 = await slash("tools", agent_id=test_agent, group_id="group_demo_1")
        print(f"[slash w/ agent] ok={resp2.get('ok')} agent_id={resp2.get('agent_id')} group_id={resp2.get('group_id')}")
        if resp2.get("agent_id") != test_agent:
            errs.append(f"agent_id 未回显：期望 {test_agent!r} 实际 {resp2.get('agent_id')!r}")
        if resp2.get("group_id") != "group_demo_1":
            errs.append(f"group_id 未回显：期望 'group_demo_1' 实际 {resp2.get('group_id')!r}")
        else:
            print(f"[check 5] agent_id/group_id 回显正确  OK")
        internal2 = resp2.get("tools", {}).get("internal", [])
        if isinstance(internal2, list) and {t.get("name") for t in internal2 if isinstance(t, dict)} == _EXPECTED_INTERNAL_TOOLS:
            print("[check 5b] 传 group_id 后 internal 仍 5 个（workspace 无关）  OK")
        else:
            errs.append(f"传 group_id 后 internal 漂移：{[t.get('name') for t in internal2]}")
        # mcp 视该 agent 是否挂了 MCP——不强断言非空（种子 agent 可能未挂）
        mcp2 = resp2.get("tools", {}).get("mcp", [])
        mcp_count = len(mcp2) if isinstance(mcp2, list) else "N/A"
        print(f"[info] agent {test_agent} mounted_mcp 工具数={mcp_count}（种子 agent 通常 0，不 fail）")

    # ── 步骤 6：group_id 缺失仍返回 internal roster ──
    print("\n── 步骤5：group_id 缺失仍返回 internal roster ──")
    resp3 = await slash("tools")
    internal3 = resp3.get("tools", {}).get("internal", [])
    if isinstance(internal3, list) and {t.get("name") for t in internal3 if isinstance(t, dict)} == _EXPECTED_INTERNAL_TOOLS:
        print("[check 6] group_id 缺失仍返回 5 内置工具（workspace 无关）  OK")
    else:
        errs.append(f"group_id 缺失时 internal 异常：{[t.get('name') for t in internal3]}")

    # ── 步骤 7：不支持的 command → ok=false + error ──
    print("\n── 步骤6：不支持的 command → ok=false + error ──")
    resp4 = await slash("nonexistent-cmd")
    print(f"[unknown] ok={resp4.get('ok')} error={resp4.get('error')!r}")
    if resp4.get("ok") is not False:
        errs.append(f"未知 command ok 应为 False：{resp4}")
    if not resp4.get("error"):
        errs.append(f"未知 command 缺 error 文案：{resp4}")
    else:
        print("[check 7] 未知 command → ok=false + error 非空  OK")

    # ── 步骤 8：command 前导 / 容忍 ──
    print("\n── 步骤7：command 前导 / 容忍（'/tools' == 'tools'） ──")
    resp5 = await slash("/tools")
    if resp5.get("ok") is True and resp5.get("command") == "tools":
        print("[check 8] '/tools' lstrip('/') 后等价 'tools'，ok=true  OK")
    else:
        errs.append(f"'/tools' 未被容忍：ok={resp5.get('ok')} command={resp5.get('command')!r}")

    # ── 结果 ──
    print("\n" + "=" * 50)
    if errs:
        print("=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print("POST /api/slash /tools 全链路验证通过：")
    print("  · {command:'tools'} 200 + ok=true + command='tools'；")
    print("  · tools={internal, mcp} + total = internal + mcp 一致；")
    print("  · internal 含 5 内置工具（read_file/write_file/edit_file/list_dir/run_command），shape 正确；")
    print("  · 不传 agent_id → mcp=[] + agent_id 回显 null；")
    print("  · 传 agent_id/group_id → 原样回显 + internal 仍 5（workspace 无关）；")
    print("  · group_id 缺失仍返回 internal roster；")
    print("  · 不支持 command → ok=false + error（前端可 fallback）；")
    print("  · command 前导 / 容忍（lstrip）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
