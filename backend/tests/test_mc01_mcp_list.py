"""MC-01 自测：MCP 页展示已配置连接（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 SK-12 / AG-12 自测模式（httpx HTTP 真源 +
探针落库 + 收尾清理，不连 WS）。

MC-01 链路（MCP 页展示已配置连接）：
  POST /api/mcp body={McpConnectionCreatePayload}
    → mcp.py create_mcp_connection → crud.create_mcp_connection 落库
    → 返回 McpConnection（前端 McpPage 卡片渲染数据源）
  GET /api/mcp
    → list_mcp_connections → crud.list_mcp_connections
    → 返回 list[McpConnection]（前端 mcpApi.list() 消费渲染卡片网格）
  前端 McpPage.tsx：
    · fetchConnections → mcpApi.list() → setConnections 渲染卡片
    · 卡片 title 显 name + transport Tag（stdio geekblue / sse purple）
    · 卡片 body 显传输详情（stdio 启动命令 command+args / sse URL）+ env
    · 卡片 actions 显启用/禁用 + 删除

为何不复刻前端卡片渲染/loading/刷新交互：那些是 UI 交互态非数据契约，HTTP 层验证
「创建落库 + 列表含新连接 + 单读回读一致 + enable/disable 切换 + 字段来自 payload」
即等价证明「MCP 页展示已配置连接」成立。前端 fetchConnections 重拉全量是刷新手段，
自测直接 GET /api/mcp 比对新连接是否在列表即证明展示逻辑成立。

验证八块（确定性断言）：
  ① 创建 stdio 连接 → 200 + McpConnection（id mcp_ 前缀 / name / transport=stdio /
     command / args / env 非空 / enabled=True / created_at 非空）；
  ② 创建 sse 连接 → 200 + McpConnection（transport=sse / url / headers / enabled=True）；
  ③ GET /api/mcp 列表含两条探针（真源交叉验证，列表项 name/transport 一致）；
  ④ 单读 GET /api/mcp/{id} 回读 == create 响应（持久化一致）；
  ⑤ disable → enabled=False（MC-03 切换就绪）；enable → enabled=True；
  ⑥ 字段真源一致：readback 字段 == create payload 原值（跨端点单一真源）；
  ⑦ 未知 mcp_id GET → 404（或 None）；
  ⑧ 收尾清理删除探针连接，校验无残留（避免污染后续自测/种子）。

为何不连 WS：MC-01 是同步 HTTP 接口（create → crud 落库），不经引擎 inbox/WS 事件流，
无实时事件可抓，纯 HTTP 校验即可（与 AG-12 hire 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# stdio 探针：模拟 filesystem MCP server（npx 启动，不实际 spawn，仅校验字段落库）。
STDIO_PAYLOAD = {
    "name": "[自测] 文件系统 MCP",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "env": {"MCP_TEST": "mc01"},
    "enabled": True,
}

# sse 探针：模拟远程 SSE 端点（不实际连接，仅校验字段落库）。
SSE_PAYLOAD = {
    "name": "[自测] 远程 SSE 端点",
    "transport": "sse",
    "url": "http://127.0.0.1:9999/sse",
    "headers": {"Authorization": "Bearer test-token"},
    "enabled": True,
}


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create(payload: dict) -> tuple[int, dict | None]:
    """POST /api/mcp body=payload，返回 (status, conn_or_error)。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/mcp", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_mcp() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/mcp")
        return r.json() if r.status_code == 200 else []


async def get_mcp(mcp_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/mcp/{mcp_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def set_enabled(mcp_id: str, enabled: bool) -> dict | None:
    path = "enable" if enabled else "disable"
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/mcp/{mcp_id}/{path}")
        return r.json() if r.status_code == 200 else None


async def delete_mcp(mcp_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/mcp/{mcp_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MC-01 自测：MCP 页展示已配置连接 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []  # 收尾清理用

    # 创建前快照
    before = await list_mcp()
    before_ids = {c["id"] for c in before}
    print(f"[pre] 创建前 mcp 连接数：{len(before)}")

    # ── 1. 创建 stdio 连接 → 200 + McpConnection ──
    print("\n[check 1] 创建 stdio 连接：POST /api/mcp (stdio)")
    status, stdio_conn = await create(STDIO_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status} body={stdio_conn}"):
        errs.append(f"[create-stdio] 非 200 status={status}")
        stdio_conn = None
    else:
        assert stdio_conn is not None
        new_id = stdio_conn.get("id", "")
        if new_id:
            probe_ids.append(new_id)
        ok_struct = (
            isinstance(stdio_conn.get("id"), str)
            and stdio_conn.get("id", "").startswith("mcp_")
            and stdio_conn.get("name") == STDIO_PAYLOAD["name"]
            and stdio_conn.get("transport") == "stdio"
            and stdio_conn.get("command") == STDIO_PAYLOAD["command"]
            and stdio_conn.get("args") == STDIO_PAYLOAD["args"]
            and stdio_conn.get("env") == STDIO_PAYLOAD["env"]
            and stdio_conn.get("enabled") is True
            and isinstance(stdio_conn.get("created_at"), str)
            and stdio_conn.get("created_at")
        )
        if _check(
            "McpConnection 结构完整（id mcp_ 前缀 / name / transport=stdio / "
            "command / args / env / enabled=True / created_at 非空）",
            ok_struct,
        ):
            print(f"      样本：id={new_id} transport={stdio_conn.get('transport')!r}")
        else:
            errs.append(f"[create-stdio] 结构异常：{stdio_conn}")

    # ── 2. 创建 sse 连接 → 200 + McpConnection ──
    print("\n[check 2] 创建 sse 连接：POST /api/mcp (sse)")
    status, sse_conn = await create(SSE_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status} body={sse_conn}"):
        errs.append(f"[create-sse] 非 200 status={status}")
        sse_conn = None
    else:
        assert sse_conn is not None
        new_id = sse_conn.get("id", "")
        if new_id:
            probe_ids.append(new_id)
        ok_struct = (
            isinstance(sse_conn.get("id"), str)
            and sse_conn.get("id", "").startswith("mcp_")
            and sse_conn.get("name") == SSE_PAYLOAD["name"]
            and sse_conn.get("transport") == "sse"
            and sse_conn.get("url") == SSE_PAYLOAD["url"]
            and sse_conn.get("headers") == SSE_PAYLOAD["headers"]
            and sse_conn.get("enabled") is True
        )
        if _check(
            "McpConnection 结构完整（id mcp_ 前缀 / name / transport=sse / url / "
            "headers / enabled=True）",
            ok_struct,
        ):
            print(f"      样本：id={new_id} transport={sse_conn.get('transport')!r}")
        else:
            errs.append(f"[create-sse] 结构异常：{sse_conn}")

    # ── 3. GET /api/mcp 列表含两条探针（真源交叉验证）──
    print("\n[check 3] 列表含两条探针连接")
    after = await list_mcp()
    after_ids = {c["id"] for c in after}
    if stdio_conn and sse_conn:
        both_in = stdio_conn["id"] in after_ids and sse_conn["id"] in after_ids
        if _check(f"GET /api/mcp 列表含 stdio({stdio_conn['id'][:14]}…) + sse({sse_conn['id'][:14]}…)",
                  both_in):
            # 列表项 name/transport 与 create 响应一致
            listed_stdio = next((c for c in after if c["id"] == stdio_conn["id"]), {})
            listed_sse = next((c for c in after if c["id"] == sse_conn["id"]), {})
            listed_ok = (
                listed_stdio.get("name") == stdio_conn.get("name")
                and listed_stdio.get("transport") == stdio_conn.get("transport")
                and listed_sse.get("name") == sse_conn.get("name")
                and listed_sse.get("transport") == sse_conn.get("transport")
            )
            if not _check("列表项 name/transport == create 响应", listed_ok):
                errs.append("[list] 列表项漂移")
        else:
            errs.append("[list] 探针连接不在列表")

    # ── 4. 单读回读 == create 响应（持久化一致）──
    print("\n[check 4] 单读 GET /api/mcp/{id} 回读一致")
    for tag, conn in (("stdio", stdio_conn), ("sse", sse_conn)):
        if not conn:
            continue
        reread = await get_mcp(conn["id"])
        if reread is None:
            _check(f"{tag}: GET 回读 200", False)
            errs.append(f"[reread-{tag}] 404")
            continue
        consistent = (
            reread.get("id") == conn.get("id")
            and reread.get("name") == conn.get("name")
            and reread.get("transport") == conn.get("transport")
            and reread.get("command") == conn.get("command")
            and reread.get("args") == conn.get("args")
            and reread.get("env") == conn.get("env")
            and reread.get("url") == conn.get("url")
            and reread.get("headers") == conn.get("headers")
            and reread.get("enabled") == conn.get("enabled")
        )
        if not _check(f"{tag}: 回读 id/name/transport/传输字段/enabled 一致", consistent):
            errs.append(f"[reread-{tag}] 回读漂移：{reread}")

    # ── 5. disable → enabled=False，enable → enabled=True（MC-03 切换就绪）──
    print("\n[check 5] enable/disable 切换（MC-03 就绪）")
    if stdio_conn:
        disabled = await set_enabled(stdio_conn["id"], False)
        if _check("disable → enabled=False",
                  disabled is not None and disabled.get("enabled") is False,
                  f"enabled={disabled.get('enabled') if disabled else 'None'}"):
            pass
        else:
            errs.append("[toggle] disable 未生效")

        enabled_back = await set_enabled(stdio_conn["id"], True)
        if _check("enable → enabled=True",
                  enabled_back is not None and enabled_back.get("enabled") is True,
                  f"enabled={enabled_back.get('enabled') if enabled_back else 'None'}"):
            pass
        else:
            errs.append("[toggle] enable 未生效")

    # ── 6. 字段真源一致：readback 字段 == create payload 原值 ──
    print("\n[check 6] 字段真源一致：readback == create payload 原值")
    if stdio_conn:
        reread = await get_mcp(stdio_conn["id"])
        same = (
            reread is not None
            and reread.get("name") == STDIO_PAYLOAD["name"]
            and reread.get("transport") == STDIO_PAYLOAD["transport"]
            and reread.get("command") == STDIO_PAYLOAD["command"]
            and reread.get("args") == STDIO_PAYLOAD["args"]
            and reread.get("env") == STDIO_PAYLOAD["env"]
            and reread.get("enabled") is True
        )
        if _check("stdio readback name/transport/command/args/env/enabled == payload 原值",
                  same, f"reread={reread}"):
            pass
        else:
            errs.append("[xref-stdio] 字段不一致")
    if sse_conn:
        reread = await get_mcp(sse_conn["id"])
        same = (
            reread is not None
            and reread.get("name") == SSE_PAYLOAD["name"]
            and reread.get("transport") == SSE_PAYLOAD["transport"]
            and reread.get("url") == SSE_PAYLOAD["url"]
            and reread.get("headers") == SSE_PAYLOAD["headers"]
            and reread.get("enabled") is True
        )
        if _check("sse readback name/transport/url/headers/enabled == payload 原值",
                  same, f"reread={reread}"):
            pass
        else:
            errs.append("[xref-sse] 字段不一致")

    # ── 7. 未知 mcp_id GET → 404 ──
    print("\n[check 7] 未知 mcp_id → 404")
    none_conn = await get_mcp("mcp:nope-not-exist")
    if _check("未知 mcp_id GET → 404/None", none_conn is None):
        pass
    else:
        errs.append(f"[404] 未知 mcp_id 未返回 None：{none_conn}")

    # ── 8. 收尾清理：删除所有探针连接 ──
    print(f"\n[cleanup] 删除 {len(probe_ids)} 个测试连接")
    for mid in probe_ids:
        ok = await delete_mcp(mid)
        if not ok:
            print(f"  ⚠️ 删除失败 {mid}")
            errs.append(f"[cleanup] 删除失败 {mid}")
    # 校验清理后无残留
    final = await list_mcp()
    leaked = [c for c in final if c["id"] in probe_ids]
    if not _check("清理后无残留测试连接", not leaked, f"{len(leaked)} 个残留"):
        errs.append(f"[cleanup] {len(leaked)} 个连接残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — MCP 页展示已配置连接端到端验证通过：")
    print("  · 创建 stdio 连接：POST /api/mcp → 200 + McpConnection（id mcp_ 前缀 /")
    print("    name / transport=stdio / command / args / env / enabled=True）；")
    print("  · 创建 sse 连接：POST /api/mcp → 200 + McpConnection（transport=sse / url / headers）；")
    print("  · 列表：GET /api/mcp 含两条探针（列表项 name/transport 一致）；")
    print("  · 持久化：单读 GET /api/mcp/{id} 回读 == create 响应；")
    print("  · enable/disable：disable→enabled=False，enable→enabled=True（MC-03 就绪）；")
    print("  · 字段真源：readback == create payload 原值（跨端点单一真源）；")
    print("  · 未知 mcp_id → 404/None。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
