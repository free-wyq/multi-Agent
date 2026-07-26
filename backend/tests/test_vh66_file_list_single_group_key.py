"""VH66 回归：任务14a——文件列表按单聊/群聊 key 校验.

锁住 ``backend/store/crud.py:list_files`` + ``backend/api/groups.py:358`` +
新补的 ``backend/api/conversations.py`` 单聊文件路由（任务14a）：

  背景：Path C 单聊分实体后，单聊驻留 worker engine 构造时 ``group_id=conversation_id``
  （见 ``registry.ensure_engine`` + ``direct.route_direct_message``），其 ``file_write``
  工具落盘到 ``DATA_DIR/workspaces/{conversation_id}/``。但文件「列表/下载」路由长期只有
  ``GET /api/groups/{groupId}/files[/{name}]``——单聊前端要列产物得滥用 group 路由
  （语义错位：单聊不是 group）。任务14a 校验 ``crud.list_files`` 是 key 无关的纯
  ``workspace_path(key)`` 查找（无 group-entity 依赖），并补 ``/api/conversations/{id}/files``
  单聊命名空间路由，让单聊/群聊各自走自己的命名空间列/下产物。

  设计真源见 memory ``single-chat-entity-split-c2-2026-07-23``（C2 共享底层：Message/流式/
  workspace 都是 conversation_id 替代 group_id 的同一代码路径）+ ``skill-system-stage4-executability``
  （受控工具池 file_write 落 workspace）。

五段契约（纯静态 + TestClient 真 HTTP，不依赖 live server / 真实 LLM）：

  A. 静态源码锁——crud.list_files key 无关 + groups 路由就位
    1. ``crud.list_files`` 函数体只 ``workspace_path(group_id)`` 查找，无 group-entity
       （get_group/create_group）依赖——key 无关，conversation_id 同路径服务.
    2. ``groups.py:358`` 的 ``list_files`` 路由仍就位（GET /api/groups/{group_id}/files）.
    3. ``groups.py`` 的 download 路由仍就位（GET /api/groups/{group_id}/files/{file_name:path}）.

  B. conversations 单聊文件路由就位——任务14a 新补
    4. ``conversations.py`` 注册 ``GET /api/conversations/{conversation_id}/files``（list）.
    5. ``conversations.py`` 注册 ``GET /api/conversations/{conversation_id}/files/{file_name:path}``（download）.

  C. 真实 HTTP——单聊 conversation_id key 列/下产物
    6. 单聊 conversation 工作区有文件 → ``GET /api/conversations/{id}/files`` 返 200 + 含该文件.
    7. ``GET /api/conversations/{id}/files/{name}`` 下载 200 + 正确 MIME + 正确内容.
    8. 单聊工作区不存在 → ``GET /api/conversations/{id}/files`` 返 200 + ``[]``（非 500）.

  D. 群聊 group_id key 回归不破——同一 crud.list_files 同一路径
    9. 群聊 group 工作区有文件 → ``GET /api/groups/{id}/files`` 仍返 200 + 含该文件.
   10. 群聊下载 ``GET /api/groups/{id}/files/{name}`` 仍 200 + 正确内容（PL-12 不破）.

  E. 安全——单聊下载路径穿越拒绝
   11. ``GET /api/conversations/{id}/files/../../../../etc/passwd`` → 400/404，绝不泄露
       /etc/passwd 内容（safe_path 拒绝穿越，复用 PL-12 download 同一守卫）.

为何不依赖 live server：本测锁的是「路由就位 + crud key 无关 + 路径穿越守卫」三件静态 +
TestClient 可验的契约，不需要真 LLM / 真 engine execute 产文件——手工往临时工作区写
文件即模拟 worker 产物（与 test_pl12_download_endpoint 同模式）。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# --- redirect DATA_DIR to a temp root BEFORE importing config/workspace ---
# workspace_path() creates dirs under DATA_DIR/workspaces; we isolate so the
# test never touches the real ~/.local/share/multi-agent.
_TMP = tempfile.mkdtemp(prefix="vh66_files_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# import after env override so config.DATA_DIR points at the temp root
import config  # noqa: E402
import engine.workspace as ws_mod  # noqa: E402

config.DATA_DIR = _TMP
ws_mod.WORKSPACE_ROOT = Path(_TMP) / "workspaces"

import api.conversations as conversations_api  # noqa: E402
import api.groups as groups_api  # noqa: E402
import store.crud as crud_mod  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
CRUD_PY = BACKEND / "store" / "crud.py"
GROUPS_PY = BACKEND / "api" / "groups.py"
CONVERSATIONS_PY = BACKEND / "api" / "conversations.py"

app = FastAPI()
app.include_router(conversations_api.router)
app.include_router(groups_api.router)
client = TestClient(app)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


def assert_contract() -> list[str]:
    errs: list[str] = []
    crud_src = _read(CRUD_PY)
    conv_src = _read(CONVERSATIONS_PY)
    groups_src = _read(GROUPS_PY)

    # ── A. crud.list_files key 无关 + groups 路由就位 ──────────────────────
    # A1 list_files body 只 workspace_path(key) 查找，无 group-entity 依赖
    try:
        body = _fn_body(crud_src, "list_files")
        if not body:
            errs.append("[A1] crud.list_files 函数体未找到")
        else:
            has_ws_lookup = "workspace_path(" in body
            # group-entity 依赖 = 把 key 当 group_id 查 get_group/create_group 之类
            # （list_files 只用参数名 group_id 作 workspace key，不查 group 表——key 无关）
            has_entity_dep = bool(re.search(r"\bget_group\s*\(|\bcreate_group\s*\(", body))
            if not has_ws_lookup:
                errs.append("[A1] crud.list_files 缺 workspace_path( 调用（key 无关查找没起作用）")
            elif has_entity_dep:
                errs.append("[A1] crud.list_files 含 get_group/create_group 依赖（key 不应耦合 group 实体）")
            else:
                print("[A1] OK  crud.list_files 只 workspace_path(key) 查找，无 group-entity 依赖（key 无关，conversation_id 同路径）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A1] 检查异常：{type(e).__name__}: {e}")

    # A2 groups list_files 路由就位
    try:
        paths = {r.path for r in groups_api.router.routes}
        if "/api/groups/{group_id}/files" not in paths:
            errs.append(f"[A2] groups 路由缺 /api/groups/{{group_id}}/files（paths={sorted(paths)}）")
        else:
            print("[A2] OK  GET /api/groups/{group_id}/files 路由就位（群聊列表回归）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A2] 检查异常：{type(e).__name__}: {e}")

    # A3 groups download 路由就位
    try:
        paths = {r.path for r in groups_api.router.routes}
        if "/api/groups/{group_id}/files/{file_name:path}" not in paths:
            errs.append(f"[A3] groups 路由缺 /api/groups/{{group_id}}/files/{{file_name:path}}（PL-12 下载回归）")
        else:
            print("[A3] OK  GET /api/groups/{group_id}/files/{file_name:path} 路由就位（PL-12 下载回归）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[A3] 检查异常：{type(e).__name__}: {e}")

    # ── B. conversations 单聊文件路由就位（任务14a 新补） ──────────────────
    # B4 list
    try:
        paths = {r.path for r in conversations_api.router.routes}
        if "/api/conversations/{conversation_id}/files" not in paths:
            errs.append(f"[B4] conversations 路由缺 /api/conversations/{{conversation_id}}/files（任务14a 单聊列表未补）")
        else:
            print("[B4] OK  GET /api/conversations/{conversation_id}/files 路由就位（任务14a 单聊列表）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B4] 检查异常：{type(e).__name__}: {e}")

    # B5 download
    try:
        paths = {r.path for r in conversations_api.router.routes}
        if "/api/conversations/{conversation_id}/files/{file_name:path}" not in paths:
            errs.append(f"[B5] conversations 路由缺 /api/conversations/{{conversation_id}}/files/{{file_name:path}}（任务14a 单聊下载未补）")
        else:
            print("[B5] OK  GET /api/conversations/{conversation_id}/files/{file_name:path} 路由就位（任务14a 单聊下载）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B5] 检查异常：{type(e).__name__}: {e}")

    # B6 静态：conversations.py 含 list_files + download_file 函数 + safe_path 守卫
    try:
        has_list = "async def list_files(" in conv_src
        has_download = "async def download_file(" in conv_src
        has_safe = "safe_path(" in conv_src
        if not has_list:
            errs.append("[B6] conversations.py 缺 async def list_files(...)")
        elif not has_download:
            errs.append("[B6] conversations.py 缺 async def download_file(...)")
        elif not has_safe:
            errs.append("[B6] conversations.py 缺 safe_path( 守卫（下载穿越防护缺失）")
        else:
            print("[B6] OK  conversations.py 含 list_files + download_file + safe_path 守卫")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[B6] 检查异常：{type(e).__name__}: {e}")

    # ── C. 真实 HTTP——单聊 conversation_id key 列/下产物 ──────────────────
    # C7 单聊有文件 → list 200 含该文件
    try:
        conv_id = "conv_vh66_a"
        ws = ws_mod.workspace_path(conv_id)
        (ws / "single_report.md").write_text("# 单聊产物\nhello vh66 single\n")
        r = client.get(f"/api/conversations/{conv_id}/files")
        files = r.json()
        names = [f["name"] for f in files] if isinstance(files, list) else []
        if r.status_code != 200:
            errs.append(f"[C7] 单聊 list 状态 {r.status_code}（应 200）")
        elif "single_report.md" not in names:
            errs.append(f"[C7] 单聊 list 未含 single_report.md（files={files}）")
        else:
            f0 = next(f for f in files if f["name"] == "single_report.md")
            has_size = isinstance(f0.get("size"), int) and f0["size"] > 0
            has_mtime = bool(f0.get("modified_at"))
            if not (has_size and has_mtime):
                errs.append(f"[C7] 单聊文件条目缺 size/modified_at（{f0}）")
            else:
                print(f"[C7] OK  单聊 list 200 含 single_report.md（size={f0['size']}, modified_at 有）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C7] 单聊 list 测试异常：{type(e).__name__}: {e}")

    # C8 单聊下载 200 + MIME + 内容
    try:
        r = client.get("/api/conversations/conv_vh66_a/files/single_report.md")
        ct = r.headers.get("content-type", "")
        if r.status_code != 200:
            errs.append(f"[C8] 单聊下载状态 {r.status_code}（应 200）")
        elif "text/markdown" not in ct:
            errs.append(f"[C8] 单聊下载 MIME 错（{ct}，应 text/markdown）")
        elif "hello vh66 single" not in r.text:
            errs.append(f"[C8] 单聊下载内容错（{r.text[:80]!r}）")
        else:
            print(f"[C8] OK  单聊下载 200 + text/markdown + 内容正确")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C8] 单聊下载测试异常：{type(e).__name__}: {e}")

    # C9 单聊工作区不存在 → 200 + []（非 500）
    try:
        r = client.get("/api/conversations/conv_vh66_nonexistent/files")
        body = r.json()
        if r.status_code != 200:
            errs.append(f"[C9] 不存在工作区 list 状态 {r.status_code}（应 200）")
        elif body != []:
            errs.append(f"[C9] 不存在工作区 list 应返 []（got {body}）")
        else:
            print("[C9] OK  单聊工作区不存在 → 200 + []（非 500，workspace_path 兜底空 list）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[C9] 不存在工作区 list 测试异常：{type(e).__name__}: {e}")

    # ── D. 群聊 group_id key 回归不破 ─────────────────────────────────────
    # D10 群聊 list 仍 200 含文件
    try:
        group_id = "group_vh66_a"
        ws = ws_mod.workspace_path(group_id)
        (ws / "group_doc.md").write_text("# 群聊产物\nhello vh66 group\n")
        r = client.get(f"/api/groups/{group_id}/files")
        files = r.json()
        names = [f["name"] for f in files] if isinstance(files, list) else []
        if r.status_code != 200:
            errs.append(f"[D10] 群聊 list 状态 {r.status_code}（应 200）")
        elif "group_doc.md" not in names:
            errs.append(f"[D10] 群聊 list 未含 group_doc.md（files={files}）")
        else:
            print("[D10] OK  群聊 list 200 含 group_doc.md（同一 crud.list_files 路径回归不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D10] 群聊 list 测试异常：{type(e).__name__}: {e}")

    # D11 群聊下载仍 200 + 内容（PL-12 不破）
    try:
        r = client.get("/api/groups/group_vh66_a/files/group_doc.md")
        if r.status_code != 200:
            errs.append(f"[D11] 群聊下载状态 {r.status_code}（应 200）")
        elif "hello vh66 group" not in r.text:
            errs.append(f"[D11] 群聊下载内容错（{r.text[:80]!r}）")
        else:
            print("[D11] OK  群聊下载 200 + 内容正确（PL-12 download 路由回归不破）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[D11] 群聊下载测试异常：{type(e).__name__}: {e}")

    # ── E. 安全——单聊下载路径穿越拒绝 ───────────────────────────────────
    # E12 ../../etc/passwd 不泄露
    try:
        r = client.get("/api/conversations/conv_vh66_a/files/../../../../etc/passwd")
        # safe_path ValueError → 400；若路径最终落在工作区内不存在的文件 → 404
        # 两者都acceptable，关键是绝不能 200 + 泄露 /etc/passwd 内容
        leaked = "root:" in r.text or "bin/bash" in r.text
        if r.status_code == 200 and leaked:
            errs.append(f"[E12] 单聊下载穿越泄露 /etc/passwd（status={r.status_code}）")
        elif r.status_code not in (400, 404):
            errs.append(f"[E12] 单聊下载穿越状态 {r.status_code}（应 400/404）")
        else:
            print(f"[E12] OK  单聊 ../../etc/passwd → {r.status_code}（safe_path 守卫拒绝，无泄露）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[E12] 路径穿越测试异常：{type(e).__name__}: {e}")

    # ── F. 隔离验证——单聊和群聊 key 不串 ─────────────────────────────────
    # F13 单聊只列自己的产物，不串群聊
    try:
        r = client.get("/api/conversations/conv_vh66_a/files")
        names = {f["name"] for f in r.json()}
        if "group_doc.md" in names:
            errs.append(f"[F13] 单聊 list 串到群聊产物（names={names}）")
        elif "single_report.md" not in names:
            errs.append(f"[F13] 单聊 list 缺自己的产物（names={names}）")
        else:
            print("[F13] OK  单聊只列自己的产物（single_report.md），不串群聊（workspace key 隔离）")
    except Exception as e:  # noqa: BLE001
        errs.append(f"[F13] 隔离验证异常：{type(e).__name__}: {e}")

    return errs


def main() -> int:
    print("=== VH66 回归：任务14a 文件列表按单聊/群聊 key 校验 ===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "文件列表按单聊/群聊 key 校验锁定：\n"
        "  · A crud.list_files key 无关（只 workspace_path(key) 查找，无 group-entity 依赖）+ groups 路由就位；\n"
        "  · B conversations 补 /api/conversations/{id}/files + /{id}/files/{name:path}（list + download + safe_path）；\n"
        "  · C 单聊 conversation_id key 列/下产物 200 + 正确 MIME/内容，工作区不存在返 []；\n"
        "  · D 群聊 group_id key 回归不破（同一 crud.list_files 路径）；\n"
        "  · E 单聊下载路径穿越 → 400/404（safe_path 守卫，无 /etc/passwd 泄露）；\n"
        "  · F 单聊/群聊 workspace key 隔离不串。"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
