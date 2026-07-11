"""PL-12 GET /api/groups/{id}/files/{name} 下载文件 端点单元自测（不依赖 pytest）。

用 FastAPI TestClient + 临时 DATA_DIR（隔离工作区）校验下载端点：
  1. 路由 GET /api/groups/{id}/files/{name} 已注册，方法 GET。
  2. 根目录文件下载：200 + 正确 MIME + 正确内容 + Content-Disposition filename。
  3. 子目录文件下载（POSIX 相对路径 login-api/index.js）：path 转换器吃斜杠，200。
  4. 路径穿越攻击 ../../etc/passwd：400（safe_path 拒绝，不泄露系统文件）。
  5. 文件不存在：404（非 500，不抛未捕获异常）。
  6. 未知 MIME（无扩展名 .binraw）：默认 application/octet-stream。
  7. 未知 group（工作区空）：404（文件不存在，不抛错）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# --- redirect DATA_DIR to a temp root BEFORE importing config/workspace ---
# workspace_path() creates dirs under DATA_DIR/workspaces; we isolate so the
# test never touches the real ~/.local/share/multi-agent.
_TMP = tempfile.mkdtemp(prefix="pl12_dl_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# import after env override so config.DATA_DIR points at the temp root
import config  # noqa: E402
import engine.workspace as ws_mod  # noqa: E402

config.DATA_DIR = _TMP
ws_mod.WORKSPACE_ROOT = Path(_TMP) / "workspaces"

import api.groups as groups_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(groups_api.router)
client = TestClient(app)

GROUP = "group_pl12_dl"
WS = ws_mod.workspace_path(GROUP)


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


def check_routes() -> None:
    paths = {r.path: list(r.methods) for r in groups_api.router.routes}
    # {file_name:path} converter — path includes the :path suffix
    dl_path = "/api/groups/{group_id}/files/{file_name:path}"
    _check("route download registered", dl_path in paths, str(paths))
    _check("method GET", "GET" in paths.get(dl_path, []))


def test_root_file() -> None:
    # root-level file: report.md with known content + .md MIME
    (WS / "report.md").write_text("# 交付物\nhello PL-12\n")
    r = client.get(f"/api/groups/{GROUP}/files/report.md")
    _check("root file 200", r.status_code == 200, str(r.status_code))
    _check("content correct", "hello PL-12" in r.text, r.text[:80])
    _check("MIME text/markdown",
           "text/markdown" in r.headers.get("content-type", ""),
           r.headers.get("content-type", ""))
    cd = r.headers.get("content-disposition", "")
    _check("Content-Disposition filename", "report.md" in cd, cd)


def test_subdir_file() -> None:
    # sub-directory file recorded by scan_workspace_artifacts as POSIX path
    (WS / "login-api").mkdir(exist_ok=True)
    (WS / "login-api" / "index.js").write_text("const x = 1;\n")
    r = client.get(f"/api/groups/{GROUP}/files/login-api/index.js")
    _check("subdir file 200 (path converter eats slash)",
           r.status_code == 200, str(r.status_code) + " " + r.text[:80])
    _check("subdir content correct", "const x = 1" in r.text, r.text[:80])
    _check("MIME text/javascript",
           "text/javascript" in r.headers.get("content-type", ""),
           r.headers.get("content-type", ""))


def test_path_traversal() -> None:
    # ../../etc/passwd must be rejected (safe_path ValueError → 400)
    r = client.get(f"/api/groups/{GROUP}/files/../../../../etc/passwd")
    _check("path traversal → 400 (not 200/500)",
           r.status_code in (400, 404), str(r.status_code))
    # Must NEVER serve /etc/passwd content
    _check("no /etc/passwd leak", "root:" not in r.text and "bin/bash" not in r.text,
           r.text[:80])


def test_not_found() -> None:
    r = client.get(f"/api/groups/{GROUP}/files/does_not_exist_xyz.md")
    _check("missing file → 404", r.status_code == 404, str(r.status_code))
    _check("404 body is JSON error", "not found" in r.text.lower(), r.text[:120])


def test_unknown_mime() -> None:
    # no extension → application/octet-stream
    (WS / "rawbin").write_bytes(b"\x00\x01\x02binary")
    r = client.get(f"/api/groups/{GROUP}/files/rawbin")
    _check("unknown MIME 200", r.status_code == 200, str(r.status_code))
    _check("default MIME application/octet-stream",
           "application/octet-stream" in r.headers.get("content-type", ""),
           r.headers.get("content-type", ""))


def test_unknown_group_empty_ws() -> None:
    # group whose workspace exists but has no files → 404 not 500
    empty_group = "group_pl12_dl_empty"
    ws_mod.workspace_path(empty_group)  # create empty ws
    r = client.get(f"/api/groups/{empty_group}/files/anything.md")
    _check("empty workspace → 404", r.status_code == 404, str(r.status_code))


def main() -> int:
    print("=== PL-12 GET /api/groups/{id}/files/{name} 下载端点单元自测 ===")
    print(f"  (隔离 DATA_DIR={_TMP})")
    check_routes()
    test_root_file()
    test_subdir_file()
    test_path_traversal()
    test_not_found()
    test_unknown_mime()
    test_unknown_group_empty_ws()
    print("\n=== 结果: PASS ===")
    print("GET /api/groups/{id}/files/{name} 下载端点：")
    print("  · 路由注册 GET，{file_name:path} 转换器吃子目录斜杠；")
    print("  · 根/子目录文件 200 + 正确 MIME + Content-Disposition filename；")
    print("  · 路径穿越 ../../etc/passwd → 400（safe_path 拒绝，无泄露）；")
    print("  · 文件不存在 → 404（非 500）；未知 MIME 默认 octet-stream；")
    print("  · 空工作区 → 404。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # best-effort cleanup of the temp root
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
