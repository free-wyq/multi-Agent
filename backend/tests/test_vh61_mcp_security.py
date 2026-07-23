"""VH61 安全契约：MCP stdio 命令白名单 + 敏感字段脱敏.

锁住 task-2026-07-23「MCP 安全加固」两条核心安全契约（纯单元 + 真 crud 落库，不依赖
live server / 真实 LLM，对齐 vh53 安全契约范式）：

  A. stdio command 校验（任务1）——直调 ``_validate_stdio_command`` / ``_validate_mcp_payload``，
     断言 raise ``HTTPException(400)``：
     1. 空 command（stdio）→ 400。
     2. 含 ``;`` 的 command（``"npx;rm -rf"``）→ 400。
     3. 含 ``|`` 的 command（``"npx|cat"``）→ 400。
     4. 含 `` ` `` 的 command → 400。
     5. 含 ``$`` 的 command（``"npx$VAR"``）→ 400。
     6. 含 ``\\n`` 的 command → 400。
     7. 含路径分隔符 ``/``（``"/bin/sh"`` / ``"./evil"``）→ 400。
     8. 含空格（``"npx -y"``）→ 400（command 必须裸名，args 才该有空格）。
     9. 非白名单 command（``"bash"`` / ``"sh"`` / ``"curl"`` / ``"rm"``）→ 400。
    10. 白名单 command（``"npx"`` / ``"uvx"`` / ``"python"`` / ``"node"`` / ``"uv"``）→ 不 raise。
    11. sse transport + 任意 command → 不 raise（sse 不校验 command）。

  B. 敏感字段脱敏（任务2）——直调 ``_apply_mcp_mask`` / ``_mask_sensitive``：
    12. env 含 ``API_KEY`` → GET 返 ``"***"``。
    13. env 含 ``apiKey`` / ``access_token`` / ``authorization`` / ``X-Secret`` → 全 ``"***"``
        （大小写不敏感 + 子串匹配）。
    14. env 含 ``DEBUG`` / ``PATH`` → 原值保留。
    15. headers 含 ``Authorization`` → ``"***"``。
    16. headers 含 ``X-Trace`` → 原值保留。
    17. env=None → None（不崩）。

  C. PUT ``"***"`` 保留原值（任务2）——真 crud 落一条连接再 PUT：
    18. 先 create 一条 env=``{"API_KEY":"real-secret","DEBUG":"1"}`` → 库里有原值。
    19. PUT env=``{"API_KEY":"***","DEBUG":"2"}`` → 回读 env=``{"API_KEY":"real-secret",
        "DEBUG":"2"}``（``"***"`` 保留原值，非敏感 key 正常更新）。
    20. PUT env=``{"NEW_KEY":"***"}`` → 库里无此 key → 原样落 ``"***"``（兜底）。

  D. create ``"***"`` 不特殊处理（任务2）——create env=``{"API_KEY":"***"}`` → 库里落
     ``"***"``，GET 返 ``"***"``（脱敏后仍是 ``"***"``，无信息泄露）。

C/D 用真 ``crud.create_mcp_connection`` / ``update_mcp_connection`` / ``get_mcp_connection`` 走真
SQLite，用唯一 name 前缀 ``vh61_probe_`` + 收尾 ``delete_mcp_connection`` 清理（对齐 vh53 模式）。
A/B 用纯函数直调断言。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def check(errs: list[str], label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[OK] {label}")
    else:
        errs.append(label)
        msg = f" — {detail}" if detail else ""
        print(f"[FAIL] {label}{msg}")


def expect_400(errs: list[str], label: str, fn, *args, **kw) -> None:
    """断言 fn(*args, **kw) raise HTTPException(400)。"""
    from fastapi import HTTPException

    try:
        fn(*args, **kw)
        check(errs, label, False, "未 raise（应拒）")
    except HTTPException as e:
        check(errs, label, e.status_code == 400, f"status={e.status_code}")
    except Exception as e:  # noqa: BLE001
        check(errs, label, False, f"异常类型非 HTTPException：{type(e).__name__}: {e}")


def expect_pass(errs: list[str], label: str, fn, *args, **kw) -> None:
    """断言 fn(*args, **kw) 不 raise。"""
    try:
        fn(*args, **kw)
        check(errs, label, True)
    except Exception as e:  # noqa: BLE001
        check(errs, label, False, f"不应 raise：{type(e).__name__}: {e}")


# ── A. stdio command 校验（纯函数直调） ──────────────────────────────────────────
def section_a(errs: list[str]) -> None:
    from api.mcp import _validate_stdio_command, _validate_mcp_payload
    from models import McpConnectionCreatePayload
    from fastapi import HTTPException

    print("\n=== A. stdio command 校验（_validate_stdio_command 直调）===")

    # A1 空 command
    expect_400(errs, "A1 空 command（stdio）→ 400", _validate_stdio_command, "")

    # A2-A6 shell 元字符
    expect_400(errs, "A2 含 ; 的 command → 400", _validate_stdio_command, "npx;rm -rf")
    expect_400(errs, "A3 含 | 的 command → 400", _validate_stdio_command, "npx|cat")
    expect_400(errs, "A4 含 ` 的 command → 400", _validate_stdio_command, "npx`whoami`")
    expect_400(errs, "A5 含 $ 的 command → 400", _validate_stdio_command, "npx$VAR")
    expect_400(errs, "A6 含 \\n 的 command → 400", _validate_stdio_command, "npx\nrm")

    # A7 路径分隔符
    expect_400(errs, "A7a 含 / 的 command（/bin/sh）→ 400", _validate_stdio_command, "/bin/sh")
    expect_400(errs, "A7b 含 / 的 command（./evil）→ 400", _validate_stdio_command, "./evil")

    # A8 含空格
    expect_400(errs, "A8 含空格的 command（npx -y）→ 400", _validate_stdio_command, "npx -y")

    # A9 非白名单 command
    for bad in ["bash", "sh", "curl", "rm"]:
        expect_400(errs, f"A9 非白名单 command ({bad!r}) → 400", _validate_stdio_command, bad)

    # A10 白名单 command → 不 raise
    for ok_cmd in ["npx", "uvx", "python", "node", "uv"]:
        expect_pass(errs, f"A10 白名单 command ({ok_cmd!r}) → 放行", _validate_stdio_command, ok_cmd)

    # A11 sse transport + 任意 command → 不 raise（_validate_mcp_payload 分流）
    for cmd in ["bash", "/bin/sh", "npx;rm", "anything goes"]:
        payload = McpConnectionCreatePayload(name="x", transport="sse", command=cmd)
        expect_pass(
            errs, f"A11 sse + command={cmd!r} → 不校验放行",
            _validate_mcp_payload, payload,
        )

    # A12 stdio transport 通过 _validate_mcp_payload 走 command 校验
    expect_400(
        errs, "A12 stdio + 非白名单 command（经 _validate_mcp_payload）→ 400",
        _validate_mcp_payload, McpConnectionCreatePayload(name="x", transport="stdio", command="bash"),
    )

    # A13 transport 缺省 = stdio（payload 默认）
    expect_400(
        errs, "A13 transport 缺省 + 空 command → 400（默认 stdio）",
        _validate_mcp_payload, McpConnectionCreatePayload(name="x", command=""),
    )

    # A14 ensure HTTPException is the exact exception type (not just status_code check)
    try:
        _validate_stdio_command("bash")
        check(errs, "A14 HTTPException 类型确认", False, "未 raise")
    except HTTPException:
        check(errs, "A14 HTTPException 类型确认", True)
    except Exception as e:  # noqa: BLE001
        check(errs, "A14 HTTPException 类型确认", False, f"非 HTTPException: {type(e).__name__}")


# ── B. 敏感字段脱敏（纯函数直调） ────────────────────────────────────────────────
def section_b(errs: list[str]) -> None:
    from api.mcp import _mask_sensitive, _apply_mcp_mask
    from models import McpConnection

    print("\n=== B. 敏感字段脱敏（_apply_mcp_mask / _mask_sensitive 直调）===")

    # B12 env 含 API_KEY → 脱敏
    masked = _mask_sensitive({"API_KEY": "sk-real-123", "DEBUG": "1"})
    check(errs, "B12 env API_KEY → ***", masked.get("API_KEY") == "***")

    # B13 多敏感 key（大小写不敏感 + 子串匹配）
    sensitive_keys = {
        "apiKey": "v1",
        "access_token": "v2",
        "authorization": "v3",
        "X-Secret": "v4",
        "client_secret": "v5",
        "password": "v6",
        "bearer": "v7",
        "credential": "v8",
        "private_key": "v9",
    }
    masked = _mask_sensitive(sensitive_keys)
    all_masked = all(masked[k] == "***" for k in sensitive_keys)
    check(errs, "B13 多敏感 key（apiKey/access_token/authorization/X-Secret/client_secret/"
                "password/bearer/credential/private_key）全脱敏",
          all_masked, f"masked={masked}")

    # B14 env 含 DEBUG / PATH → 原值保留
    masked = _mask_sensitive({"DEBUG": "1", "PATH": "/usr/bin"})
    check(errs, "B14 env DEBUG/PATH 原值保留",
          masked.get("DEBUG") == "1" and masked.get("PATH") == "/usr/bin")

    # B15 headers 含 Authorization → ***
    masked = _mask_sensitive({"Authorization": "Bearer real-token"})
    check(errs, "B15 headers Authorization → ***", masked.get("Authorization") == "***")

    # B16 headers 含 X-Trace → 原值保留
    masked = _mask_sensitive({"X-Trace": "abc-123"})
    check(errs, "B16 headers X-Trace 原值保留", masked.get("X-Trace") == "abc-123")

    # B17 env=None → None（不崩）
    masked = _mask_sensitive(None)
    check(errs, "B17 env=None → None（不崩）", masked is None)

    # B17b 空字典 → 空字典
    masked = _mask_sensitive({})
    check(errs, "B17b env={} → {}", masked == {})

    # B18 _apply_mcp_mask 返回新对象，不改原
    conn = McpConnection(
        id="mcp_test", name="t", transport="stdio", command="npx",
        env={"API_KEY": "secret"}, headers={"Authorization": "Bearer x"},
    )
    masked_conn = _apply_mcp_mask(conn)
    check(errs, "B18 _apply_mcp_mask env/headers 脱敏",
          masked_conn.env == {"API_KEY": "***"} and masked_conn.headers == {"Authorization": "***"})
    check(errs, "B18b _apply_mcp_mask 不改原对象（原值保留）",
          conn.env == {"API_KEY": "secret"} and conn.headers == {"Authorization": "Bearer x"})
    check(errs, "B18c _apply_mcp_mask 返回新对象（id 不同）",
          masked_conn is not conn)


# ── C. PUT "***" 保留原值（真 crud 走真 SQLite） ─────────────────────────────────
async def section_c(errs: list[str]) -> list[str]:
    """返回 probe_ids 供收尾清理。"""
    from store import crud
    from models import McpConnectionCreatePayload

    print("\n=== C. PUT *** 保留原值（真 crud 走真 SQLite）===")
    probe_ids: list[str] = []

    # C18 create env={"API_KEY":"real-secret","DEBUG":"1"} → 库里有原值
    created = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name="vh61_probe_c18", transport="stdio", command="npx", args=["-y", "x"],
        env={"API_KEY": "real-secret", "DEBUG": "1"}, enabled=True))
    probe_ids.append(created.id)
    got = await crud.get_mcp_connection(created.id)
    check(errs, "C18 create 后库中 env 含 API_KEY=real-secret",
          got is not None and got.env == {"API_KEY": "real-secret", "DEBUG": "1"},
          f"env={got.env if got else None}")

    # C19 PUT env={"API_KEY":"***","DEBUG":"2"} → 回读 env={"API_KEY":"real-secret","DEBUG":"2"}
    upd_payload = McpConnectionCreatePayload(
        name="vh61_probe_c18", transport="stdio", command="npx",
        env={"API_KEY": "***", "DEBUG": "2"},
    )
    # 走 update 路由：先 merge_masked_fields 再 crud.update_mcp_connection
    from api.mcp import _merge_masked_fields, update_mcp_connection_route
    merged = await _merge_masked_fields(created.id, upd_payload)
    check(errs, "C19a _merge_masked_fields 把 *** 替换为原值",
          merged.env == {"API_KEY": "real-secret", "DEBUG": "2"},
          f"merged.env={merged.env}")
    updated = await update_mcp_connection_route(created.id, upd_payload)
    check(errs, "C19b update 路由回读 API_KEY=real-secret + DEBUG=2",
          updated is not None and updated.env == {"API_KEY": "real-secret", "DEBUG": "2"},
          f"env={updated.env if updated else None}")

    # C20 PUT env={"NEW_KEY":"***"} → 库里无此 key → 原样落 "***"
    upd_payload2 = McpConnectionCreatePayload(
        name="vh61_probe_c18", transport="stdio", command="npx",
        env={"NEW_KEY": "***"},
    )
    merged2 = await _merge_masked_fields(created.id, upd_payload2)
    check(errs, "C20a _merge_masked_fields 库里无 NEW_KEY → 原样保留 ***",
          merged2.env == {"NEW_KEY": "***"}, f"merged2.env={merged2.env}")
    updated2 = await update_mcp_connection_route(created.id, upd_payload2)
    check(errs, "C20b update 路由库里无 NEW_KEY → 原样落 ***",
          updated2 is not None and updated2.env == {"NEW_KEY": "***"},
          f"env={updated2.env if updated2 else None}")

    return probe_ids


# ── D. create "***" 不特殊处理（真 crud 走真 SQLite） ────────────────────────────
async def section_d(errs: list[str]) -> list[str]:
    from store import crud
    from models import McpConnectionCreatePayload
    from api.mcp import create_mcp_connection_route, get_mcp_connection_route, _apply_mcp_mask

    print("\n=== D. create *** 不特殊处理（原样落库，GET 再脱敏）===")
    probe_ids: list[str] = []

    # D21 create env={"API_KEY":"***"} → 库里落 "***"（原样）
    payload = McpConnectionCreatePayload(
        name="vh61_probe_d21", transport="stdio", command="npx", args=["-y", "x"],
        env={"API_KEY": "***", "DEBUG": "1"}, enabled=True,
    )
    created = await create_mcp_connection_route(payload)
    probe_ids.append(created.id)
    # create 路由直接返 crud 结果（未脱敏），库内应原样落 "***"
    check(errs, "D21 create 路由返 env 原样（API_KEY=***）",
          created.env == {"API_KEY": "***", "DEBUG": "1"}, f"env={created.env}")

    # D22 GET 路由返脱敏后仍 "***"（无信息泄露）
    got = await get_mcp_connection_route(created.id)
    check(errs, "D22 GET 路由 env API_KEY 仍 ***（脱敏后仍是 ***）",
          got is not None and got.env == {"API_KEY": "***", "DEBUG": "1"},
          f"env={got.env if got else None}")

    # D23 直查 crud（未脱敏）确认库里原样落 "***"
    raw = await crud.get_mcp_connection(created.id)
    check(errs, "D23 crud 直查 env 原样（API_KEY=***，未特殊处理）",
          raw is not None and raw.env == {"API_KEY": "***", "DEBUG": "1"},
          f"env={raw.env if raw else None}")

    # D24 真敏感值 create（非 ***）→ GET 脱敏为 ***
    payload2 = McpConnectionCreatePayload(
        name="vh61_probe_d24", transport="stdio", command="npx", args=["-y", "x"],
        env={"API_KEY": "sk-real-secret-xyz", "DEBUG": "1"}, enabled=True,
    )
    created2 = await create_mcp_connection_route(payload2)
    probe_ids.append(created2.id)
    got2 = await get_mcp_connection_route(created2.id)
    check(errs, "D24 真敏感值 create → GET 脱敏为 ***",
          got2 is not None and got2.env == {"API_KEY": "***", "DEBUG": "1"},
          f"env={got2.env if got2 else None}")
    # 原对象未被脱敏改动（_apply_mcp_mask 返回新对象）
    raw2 = await crud.get_mcp_connection(created2.id)
    check(errs, "D24b crud 直查保留真值（脱敏不改库）",
          raw2 is not None and raw2.env == {"API_KEY": "sk-real-secret-xyz", "DEBUG": "1"},
          f"env={raw2.env if raw2 else None}")

    # D25 headers 真敏感值 create → GET 脱敏
    payload3 = McpConnectionCreatePayload(
        name="vh61_probe_d25", transport="sse", url="http://x/sse",
        headers={"Authorization": "Bearer real-token", "X-Trace": "abc"}, enabled=True,
    )
    created3 = await create_mcp_connection_route(payload3)
    probe_ids.append(created3.id)
    got3 = await get_mcp_connection_route(created3.id)
    check(errs, "D25 sse headers Authorization → ***（GET 脱敏）+ X-Trace 原值保留",
          got3 is not None
          and got3.headers == {"Authorization": "***", "X-Trace": "abc"},
          f"headers={got3.headers if got3 else None}")

    return probe_ids


# ── E. create 路由校验拒绝（真路由调真拒绝） ────────────────────────────────────
async def section_e(errs: list[str]) -> list[str]:
    """create 路由在写库前调校验，非白名单 command 应 400（不落库）。"""
    from api.mcp import create_mcp_connection_route
    from models import McpConnectionCreatePayload
    from fastapi import HTTPException

    print("\n=== E. create 路由写库前校验（拒绝不落库）===")
    probe_ids: list[str] = []

    # E26 create 路由 + 非白名单 command → 400 + 不落库
    bad_payload = McpConnectionCreatePayload(
        name="vh61_probe_e26_should_not_persist", transport="stdio", command="bash",
    )
    try:
        await create_mcp_connection_route(bad_payload)
        check(errs, "E26 create 路由非白名单 command → 400", False, "未 raise（应拒）")
    except HTTPException as e:
        check(errs, "E26 create 路由非白名单 command → 400",
              e.status_code == 400, f"status={e.status_code}")

    # E27 create 路由 + 含 shell 元字符 → 400
    bad_payload2 = McpConnectionCreatePayload(
        name="vh61_probe_e27_should_not_persist", transport="stdio", command="npx;rm -rf /",
    )
    try:
        await create_mcp_connection_route(bad_payload2)
        check(errs, "E27 create 路由含 ; → 400", False, "未 raise（应拒）")
    except HTTPException as e:
        check(errs, "E27 create 路由含 ; → 400", e.status_code == 400, f"status={e.status_code}")

    # E28 update 路由 + 非白名单 command → 400
    from api.mcp import update_mcp_connection_route
    # 先建一条合法的，再 PUT 改成非法
    from store import crud
    good = await crud.create_mcp_connection(McpConnectionCreatePayload(
        name="vh61_probe_e28", transport="stdio", command="npx", args=[], enabled=True))
    probe_ids.append(good.id)
    bad_upd = McpConnectionCreatePayload(
        name="vh61_probe_e28", transport="stdio", command="bash",
    )
    try:
        await update_mcp_connection_route(good.id, bad_upd)
        check(errs, "E28 update 路由非白名单 command → 400", False, "未 raise（应拒）")
    except HTTPException as e:
        check(errs, "E28 update 路由非白名单 command → 400", e.status_code == 400, f"status={e.status_code}")
    # 确认原 command 未被改（校验在写库前）
    got = await crud.get_mcp_connection(good.id)
    check(errs, "E28b update 拒绝后原 command 未被改（仍 npx）",
          got is not None and got.command == "npx", f"command={got.command if got else None}")

    return probe_ids


async def main() -> int:
    print("=" * 70)
    print("VH61 MCP 安全加固契约：stdio 白名单 + 敏感字段脱敏")
    print("=" * 70)

    # 隔离 DB（临时目录，不污染开发库）
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="vh61_test_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir

    probe_ids: list[str] = []
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        _db.engine = create_async_engine(
            _db.DB_URL, echo=False,
            connect_args={"check_same_thread": False}, pool_pre_ping=True,
        )
        _db.SessionLocal = async_sessionmaker(
            _db.engine, expire_on_commit=False, class_=AsyncSession,
        )
        await _db.init_db()

        errs: list[str] = []

        # A. 纯函数校验
        section_a(errs)
        # B. 纯函数脱敏
        section_b(errs)
        # C. PUT *** 保留原值（真 crud）
        probe_ids.extend(await section_c(errs))
        # D. create *** 不特殊处理（真 crud）
        probe_ids.extend(await section_d(errs))
        # E. create/update 路由写库前校验
        probe_ids.extend(await section_e(errs))

        # 清理
        from store import crud
        for mid in probe_ids:
            try:
                await crud.delete_mcp_connection(mid)
            except Exception as e:  # noqa: BLE001
                print(f"  [cleanup] 删除 {mid} 失败: {e}")

        print()
        if errs:
            print(f"结果: FAIL ({len(errs)} 项)")
            for e in errs:
                print(f"  - {e}")
            return 1
        print(f"结果: PASS（A/B/C/D/E 全过，{len(probe_ids)} 个探针已清理）")
        return 0
    finally:
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
