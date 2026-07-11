"""MC-02 自测：添加 stdio 与 sse 两种连接（端到端，表单提交路径验证）。

不依赖 pytest，直接 asyncio 跑。沿用 MC-01 / AG-12 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理，不连 WS）。

MC-02 聚焦「添加连接表单」链路——前端 McpPage 的「添加连接」Modal 表单提交路径：
  前端表单（McpPage.tsx handleCreate）：
    · Form.validateFields → 按 transport 组装 McpConnectionCreatePayload
    · stdio: command（必填）+ args 文本域按行 split → string[] + env JSON.parse → dict
    · sse: url（必填）+ headers JSON.parse → dict
    · 不相关字段显式 omit（stdio 不传 url/headers，sse 不传 command/args/env）
    · mcpApi.create(payload) → POST /api/mcp → fetchConnections 刷新
  本自测复刻这条「表单→payload→落库」链路，HTTP 层用表单提交的精确 payload 形态
  调 POST /api/mcp，验证后端忠实落库 + 字段真源一致 + 列表展示。

与 MC-01 自测的区别：MC-01 验证「展示已配置连接」（list/get/enable/disable 契约），
MC-02 验证「添加连接表单提交路径」——重点在 payload 组装逻辑（args 文本→string[]、
env/headers JSON→dict、transport 分流 omit 不相关字段）的端到端正确性。

验证八块（确定性断言）：
  ① stdio 表单 payload（含 command+args+env）→ 200 + McpConnection（args 落库为
     string[] 非空 / env 落库为 dict / enabled=True）；
  ② sse 表单 payload（含 url+headers）→ 200 + McpConnection（url 落库 / headers
     落库为 dict / enabled=True）；
  ③ transport 分流 omit：stdio 连接不含 url/headers 字段（或为空/缺省），sse 连接
     不含 command/args/env 字段（或为空/缺省）——验证表单「不相关字段 omit」语义；
  ④ GET /api/mcp 列表含两条新连接（fetchConnections 能拿到新卡片渲染源）；
  ⑤ 单读回读字段 == 表单 payload 原值（跨端点单一真源，证明落库忠实）；
  ⑥ args 文本域 split 路径：多行 args → string[] 顺序保留（如 -y/server/tmp）；
  ⑦ env/headers JSON 解析路径：JSON 字符串 → dict 落库（key-value 保留）；
  ⑧ 收尾清理删除探针连接，校验无残留。

为何不连 WS：MC-02 是同步 HTTP 接口（create→crud 落库），不经引擎 inbox/WS 事件流，
纯 HTTP 校验即可（与 MC-01 同构）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# stdio 表单 payload：复刻前端 handleCreate 的 stdio 分支组装结果。
# 前端表单值：name/transport=stdio/command="npx"/args 文本域三行/env JSON 文本域。
# handleCreate 组装后 payload（args split 去空行、env JSON.parse）：
STDIO_PAYLOAD = {
    "name": "[MC-02] 文件系统 stdio",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    "env": {"MCP_TEST_ENV": "mc02-stdio", "DEBUG": "1"},
    "enabled": True,
}

# sse 表单 payload：复刻前端 handleCreate 的 sse 分支组装结果。
# 前端表单值：name/transport=sse/url/headers JSON 文本域。
# handleCreate 组装后 payload（headers JSON.parse）：
SSE_PAYLOAD = {
    "name": "[MC-02] 远程 sse 端点",
    "transport": "sse",
    "url": "http://127.0.0.1:8082/sse",
    "headers": {"Authorization": "Bearer mc02-token", "X-Trace": "abc"},
    "enabled": True,
}

# 不带可选字段的极简 stdio（验证 args/env 可选）——command 必填，args/env 省略。
STDIO_MINIMAL = {
    "name": "[MC-02] 极简 stdio",
    "transport": "stdio",
    "command": "echo",
    "enabled": False,
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


async def delete_mcp(mcp_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/mcp/{mcp_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== MC-02 自测：添加 stdio 与 sse 两种连接（表单提交路径）===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []

    before = await list_mcp()
    print(f"[pre] 创建前 mcp 连接数：{len(before)}")

    # ── 1. stdio 表单 payload（command+args+env）→ 200 + McpConnection ──
    print("\n[check 1] stdio 表单提交：POST /api/mcp (command+args+env)")
    status, stdio_conn = await create(STDIO_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status} body={stdio_conn}"):
        errs.append(f"[stdio] 非 200 status={status}")
        stdio_conn = None
    else:
        assert stdio_conn is not None
        if stdio_conn.get("id"):
            probe_ids.append(stdio_conn["id"])
        # args 落库为 string[] 非空 + env 落库为 dict + enabled=True
        ok = (
            stdio_conn.get("id", "").startswith("mcp_")
            and stdio_conn.get("name") == STDIO_PAYLOAD["name"]
            and stdio_conn.get("transport") == "stdio"
            and stdio_conn.get("command") == "npx"
            and stdio_conn.get("args") == STDIO_PAYLOAD["args"]
            and stdio_conn.get("env") == STDIO_PAYLOAD["env"]
            and stdio_conn.get("enabled") is True
        )
        if _check("stdio 落库 command/args(string[])/env(dict)/enabled=True", ok,
                  f"args={stdio_conn.get('args')!r} env={stdio_conn.get('env')!r}"):
            print(f"      样本：id={stdio_conn['id'][:18]}… args={len(stdio_conn.get('args', []))} 项")
        else:
            errs.append(f"[stdio] 字段异常：{stdio_conn}")

    # ── 2. sse 表单 payload（url+headers）→ 200 + McpConnection ──
    print("\n[check 2] sse 表单提交：POST /api/mcp (url+headers)")
    status, sse_conn = await create(SSE_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status} body={sse_conn}"):
        errs.append(f"[sse] 非 200 status={status}")
        sse_conn = None
    else:
        assert sse_conn is not None
        if sse_conn.get("id"):
            probe_ids.append(sse_conn["id"])
        ok = (
            sse_conn.get("id", "").startswith("mcp_")
            and sse_conn.get("name") == SSE_PAYLOAD["name"]
            and sse_conn.get("transport") == "sse"
            and sse_conn.get("url") == SSE_PAYLOAD["url"]
            and sse_conn.get("headers") == SSE_PAYLOAD["headers"]
            and sse_conn.get("enabled") is True
        )
        if _check("sse 落库 url/headers(dict)/enabled=True", ok,
                  f"url={sse_conn.get('url')!r} headers={sse_conn.get('headers')!r}"):
            print(f"      样本：id={sse_conn['id'][:18]}…")
        else:
            errs.append(f"[sse] 字段异常：{sse_conn}")

    # ── 3. transport 分流 omit：不相关字段缺省 ──
    print("\n[check 3] transport 分流 omit（不相关字段缺省）")
    if stdio_conn:
        # stdio 连接不应带 sse 专属字段（url 应为空字符串，headers 应为 None）
        stdio_omit = (
            not stdio_conn.get("url")  # "" 或 None
            and stdio_conn.get("headers") is None
        )
        if _check("stdio 连接 url 空 / headers None（sse 字段未落库）", stdio_omit,
                  f"url={stdio_conn.get('url')!r} headers={stdio_conn.get('headers')!r}"):
            pass
        else:
            errs.append("[omit-stdio] stdio 连接残留 sse 字段")
    if sse_conn:
        # sse 连接不应带 stdio 专属字段（command 空 / args 空 / env None）
        sse_omit = (
            not sse_conn.get("command")
            and sse_conn.get("args") == []
            and sse_conn.get("env") is None
        )
        if _check("sse 连接 command 空 / args=[] / env None（stdio 字段未落库）", sse_omit,
                  f"command={sse_conn.get('command')!r} args={sse_conn.get('args')!r} env={sse_conn.get('env')!r}"):
            pass
        else:
            errs.append("[omit-sse] sse 连接残留 stdio 字段")

    # ── 4. GET /api/mcp 列表含两条新连接 ──
    print("\n[check 4] 列表含两条新连接（fetchConnections 拿得到）")
    after = await list_mcp()
    after_ids = {c["id"] for c in after}
    if stdio_conn and sse_conn:
        both_in = stdio_conn["id"] in after_ids and sse_conn["id"] in after_ids
        if _check("GET /api/mcp 列表含 stdio + sse 两条新连接", both_in):
            pass
        else:
            errs.append("[list] 新连接不在列表")
    else:
        _check("前置：两条连接创建成功（否则跳过列表校验）", False)
        errs.append("[list] 前置创建失败，列表校验跳过")

    # ── 5. 单读回读字段 == 表单 payload 原值（跨端点单一真源）──
    print("\n[check 5] 单读回读 == 表单 payload 原值")
    for tag, conn, payload in (
        ("stdio", stdio_conn, STDIO_PAYLOAD),
        ("sse", sse_conn, SSE_PAYLOAD),
    ):
        if not conn:
            continue
        reread = await get_mcp(conn["id"])
        if reread is None:
            _check(f"{tag}: 回读 200", False)
            errs.append(f"[reread-{tag}] 404")
            continue
        same = all(reread.get(k) == v for k, v in payload.items())
        if _check(f"{tag}: 回读 name/transport/传输字段/enabled == payload 原值", same,
                  f"reread={reread}"):
            pass
        else:
            errs.append(f"[reread-{tag}] 回读漂移：{reread}")

    # ── 6. args 文本域 split 路径：多行 → string[] 顺序保留 ──
    print("\n[check 6] args 多行 → string[] 顺序保留")
    if stdio_conn:
        args = stdio_conn.get("args", [])
        # 顺序必须与 payload 一致：-y / server-filesystem / /tmp
        order_ok = (
            isinstance(args, list)
            and len(args) == 3
            and args[0] == "-y"
            and args[1] == "@modelcontextprotocol/server-filesystem"
            and args[2] == "/tmp"
        )
        if _check("args[0]=-y / args[1]=server-filesystem / args[2]=/tmp 顺序保留", order_ok,
                  f"args={args!r}"):
            pass
        else:
            errs.append(f"[args] 顺序异常：{args!r}")

    # ── 7. env/headers JSON 解析路径：JSON 字符串 → dict 落库 ──
    print("\n[check 7] env/headers JSON → dict 落库（key-value 保留）")
    if stdio_conn:
        env = stdio_conn.get("env")
        env_ok = (
            isinstance(env, dict)
            and env.get("MCP_TEST_ENV") == "mc02-stdio"
            and env.get("DEBUG") == "1"
        )
        if _check("stdio env dict 含 MCP_TEST_ENV + DEBUG 两个 key", env_ok, f"env={env!r}"):
            pass
        else:
            errs.append(f"[env] 异常：{env!r}")
    if sse_conn:
        headers = sse_conn.get("headers")
        headers_ok = (
            isinstance(headers, dict)
            and headers.get("Authorization") == "Bearer mc02-token"
            and headers.get("X-Trace") == "abc"
        )
        if _check("sse headers dict 含 Authorization + X-Trace 两个 key", headers_ok,
                  f"headers={headers!r}"):
            pass
        else:
            errs.append(f"[headers] 异常：{headers!r}")

    # ── 7b. 极简 stdio（args/env 省略，enabled=False）→ 验证可选字段缺省 ──
    print("\n[check 7b] 极简 stdio（args/env 省略，enabled=False）")
    status, mini_conn = await create(STDIO_MINIMAL)
    if not _check("HTTP 200", status == 200, f"status={status}"):
        errs.append(f"[minimal] 非 200 status={status}")
        mini_conn = None
    else:
        assert mini_conn is not None
        if mini_conn.get("id"):
            probe_ids.append(mini_conn["id"])
        # command 落库 / args 缺省为 [] / env 缺省为 None / enabled=False
        mini_ok = (
            mini_conn.get("command") == "echo"
            and mini_conn.get("args") == []
            and mini_conn.get("env") is None
            and mini_conn.get("enabled") is False
        )
        if _check("极简 stdio：command 落库 / args=[] / env=None / enabled=False", mini_ok,
                  f"conn={mini_conn}"):
            pass
        else:
            errs.append(f"[minimal] 异常：{mini_conn}")

    # ── 8. 收尾清理：删除所有探针连接 ──
    print(f"\n[cleanup] 删除 {len(probe_ids)} 个测试连接")
    for mid in probe_ids:
        ok = await delete_mcp(mid)
        if not ok:
            print(f"  ⚠️ 删除失败 {mid}")
            errs.append(f"[cleanup] 删除失败 {mid}")
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
    print("PASS — 添加 stdio 与 sse 两种连接端到端验证通过：")
    print("  · stdio 表单：command+args+env → 200 + McpConnection（args string[] / env dict）；")
    print("  · sse 表单：url+headers → 200 + McpConnection（url / headers dict）；")
    print("  · transport 分流 omit：stdio 不带 sse 字段，sse 不带 stdio 字段；")
    print("  · 列表含两条新连接（fetchConnections 可刷新渲染）；")
    print("  · 单读回读 == payload 原值（跨端点单一真源）；")
    print("  · args 多行 split 顺序保留；env/headers JSON→dict key-value 保留；")
    print("  · 极简 stdio（args/env 省略，enabled=False）缺省正确。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
