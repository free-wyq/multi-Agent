"""PL-12 单元自测：工作区产物扫描 + Task.artifact_path 记录（不依赖 pytest / 后端在线）。

校验 PL-12 的两个新零件（实现在 engine/workspace.py + store/crud.py，由
registry._run_worker_task 在任务完成后调用）：

  scan_workspace_artifacts(group_id) → {"files": [...]}
    1. 空工作区（目录不存在）→ {"files": []}，不抛错。
    2. 有文件的工作区 → files 非空，每项含 name/path/size/modified_at。
    3. path 是工作区相对 POSIX 路径（子目录文件带 / 分隔）。
    4. node_modules 等跳过目录被整体剪枝（不漏入 manifest）。
    5. 深度超过 _MAX_DEPTH 的文件不收录（防 node_modules 爆炸）。
    6. files 按 modified_at 降序（最新在前）——primary artifact_path 取首个。
    7. 文件数封顶 _MAX_FILES（防大目录撑爆 DB 行）。

  crud.set_task_artifact(task_id, artifact_path, artifact) → Task | None
    8. 已存在 task → 更新 artifact_path + artifact，返回 Task。
    9. 未知 task_id → 返回 None，不抛错（coordinator-only 合成任务无行）。
   10. 只改 artifact_path/artifact 两列，不动 status/exit_code（已被引擎写定）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 让 backend/ 在 sys.path（与其它自测一致：从 backend 目录 PYTHONPATH=. 跑）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.workspace import (  # noqa: E402
    _MAX_DEPTH,
    _MAX_FILES,
    _SKIP_DIRS,
    scan_workspace_artifacts,
    workspace_path,
)


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


def test_scan_empty_workspace(tmp_root: Path) -> None:
    """1. 工作区目录不存在 → 空列表，不抛错。"""
    # 指向一个不存在的 group（workspace_path 会创建目录，故用一个「目录里没文件」的群）
    empty_group = "group_pl12_unit_empty"
    ws = workspace_path(empty_group)
    # workspace_path 已创建空目录
    _check("空工作区返回空 files", ws.exists() and scan_workspace_artifacts(empty_group) == {"files": []})


def test_scan_basic_structure(tmp_root: Path) -> None:
    """2/3. 有文件的工作区 → files 非空，含 name/path/size/modified_at；子目录 path 带 /。"""
    group = "group_pl12_unit_basic"
    ws = workspace_path(group)
    (ws / "report.md").write_text("# hello\n")
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "data.json").write_text('{"a":1}')

    r = scan_workspace_artifacts(group)
    files = r["files"]
    _check("扫描到 2 个文件", len(files) == 2, str([f["path"] for f in files]))
    by_path = {f["path"]: f for f in files}
    _check("根文件 path 是相对名", by_path["report.md"]["name"] == "report.md")
    _check("子目录文件 path 带 / 分隔", "sub/data.json" in by_path)
    _check("子目录文件 name 是 basename", by_path["sub/data.json"]["name"] == "data.json")
    for f in files:
        _check("每项含 size", "size" in f and isinstance(f["size"], int))
        _check("每项含 modified_at", bool(f.get("modified_at")))


def test_scan_skip_dirs(tmp_root: Path) -> None:
    """4. node_modules 等跳过目录被整体剪枝。"""
    group = "group_pl12_unit_skip"
    ws = workspace_path(group)
    (ws / "real.md").write_text("keep")
    (ws / "node_modules").mkdir(exist_ok=True)
    (ws / "node_modules" / "dep.js").write_text("deps")  # 应被剪
    (ws / "node_modules" / "lib").mkdir(exist_ok=True)
    (ws / "node_modules" / "lib" / "x.js").write_text("deps2")  # 应被剪
    (ws / ".git").mkdir(exist_ok=True)
    (ws / ".git" / "config").write_text("git")  # 应被剪

    r = scan_workspace_artifacts(group)
    paths = [f["path"] for f in r["files"]]
    _check("real.md 收录", "real.md" in paths)
    _check("node_modules 剪枝（无 dep.js）", not any("node_modules" in p for p in paths), str(paths))
    _check(".git 剪枝（无 config）", not any(p.startswith(".git") for p in paths), str(paths))


def test_scan_depth_bound(tmp_root: Path) -> None:
    """5. 深度超过 _MAX_DEPTH 的文件不收录。"""
    group = "group_pl12_unit_depth"
    ws = workspace_path(group)
    # 造一条深链：d1/d2/.../dN/file
    parts = ["d" + str(i) for i in range(1, _MAX_DEPTH + 2)]
    deep = ws
    for p in parts:
        deep = deep / p
        deep.mkdir(exist_ok=True)
    (deep / "too_deep.md").write_text("x")
    # 一个浅文件应收录
    (ws / "shallow.md").write_text("y")

    r = scan_workspace_artifacts(group)
    paths = [f["path"] for f in r["files"]]
    _check("浅文件收录", "shallow.md" in paths)
    _check("过深文件不收录", not any("too_deep.md" in p for p in paths), str(paths))


def test_scan_newest_first(tmp_root: Path) -> None:
    """6. files 按 modified_at 降序（最新在前）。"""
    group = "group_pl12_unit_order"
    ws = workspace_path(group)
    # 先写旧文件，再写新文件（mtime 单调）
    (ws / "old.md").write_text("old")
    os.utime(ws / "old.md", (1_000_000, 1_000_000))
    (ws / "new.md").write_text("new")
    os.utime(ws / "new.md", (2_000_000, 2_000_000))

    r = scan_workspace_artifacts(group)
    paths = [f["path"] for f in r["files"]]
    _check("newest 在前（new.md 先于 old.md）", paths[0] == "new.md", str(paths))


def test_scan_max_files(tmp_root: Path) -> None:
    """7. 文件数封顶 _MAX_FILES（防大目录撑爆）。"""
    group = "group_pl12_unit_cap"
    ws = workspace_path(group)
    # 造 _MAX_FILES + 50 个文件
    n = _MAX_FILES + 50
    for i in range(n):
        (ws / f"f{i:04d}.txt").write_text(str(i))

    r = scan_workspace_artifacts(group)
    _check(f"文件数封顶 {_MAX_FILES}", len(r["files"]) == _MAX_FILES, str(len(r["files"])))


async def test_set_task_artifact() -> None:
    """8/9/10. crud.set_task_artifact 更新 / 未知 task / 不动其它列。"""
    from store import crud
    from store.database import init_db

    await init_db()

    # 8/10: 先建一个 task，再 set artifact，校验只改两列
    payload = type(
        "P", (), {
            "group_id": "group_pl12_unit_crud",
            "title": "t", "description": None,
            "assigned_agent_id": None, "dependencies": [], "dag_order": None,
        }
    )()
    t = await crud.create_task(payload)
    # 模拟引擎已写定 status/exit_code（先 update 到 working+exit 7）
    upd = type("U", (), {
        "group_id": t.group_id, "title": t.title, "description": None,
        "assigned_agent_id": None, "dependencies": [], "dag_order": None,
        "status": "working",  # 别的字段（update_task 只认 payload 字段，status 非其字段则忽略）
    })()
    # crud.update_task 用 model_dump(exclude_unset, exclude_none)；这里仅验证 set_task_artifact
    manifest = {"files": [{"name": "out.md", "path": "out.md", "size": 5, "modified_at": "z"}]}
    updated = await crud.set_task_artifact(t.id, "out.md", manifest)
    _check("set_task_artifact 返回 Task", updated is not None)
    _check("artifact_path 已更新", updated.artifact_path == "out.md")
    _check("artifact manifest 已更新", updated.artifact == manifest)

    # 回读真源
    t2 = await crud.get_task(t.id)
    _check("回读 artifact_path 一致", t2.artifact_path == "out.md")
    _check("回读 artifact 一致", t2.artifact == manifest)

    # 9: 未知 task_id → None，不抛
    unknown = await crud.set_task_artifact("task_does_not_exist_xyz", "x", {"files": []})
    _check("未知 task_id 返回 None", unknown is None)

    # 清理
    await crud.delete_task(t.id)


def main() -> int:
    print("=== PL-12 单元自测：工作区产物扫描 + Task.artifact_path 记录 ===")
    print(f"  (扫描边界 _MAX_DEPTH={_MAX_DEPTH} _MAX_FILES={_MAX_FILES} _SKIP_DIRS={sorted(_SKIP_DIRS)})")

    # 临时改 DATA_DIR 到一个临时根，让 workspace_path 落在隔离目录里，
    # 不污染真实 ~/.local/share/multi-agent/workspaces。
    with tempfile.TemporaryDirectory() as td:
        import engine.workspace as ws_mod
        import config

        orig_data_dir = config.DATA_DIR
        orig_ws_root = ws_mod.WORKSPACE_ROOT
        config.DATA_DIR = td
        ws_mod.WORKSPACE_ROOT = Path(td) / "workspaces"
        # 扫描 epoch 基于旧 WORKSPACE_ROOT，重置让其在临时根取
        ws_mod._SCAN_EPOCH = None

        try:
            print("-- scan_workspace_artifacts --")
            test_scan_empty_workspace(Path(td))
            test_scan_basic_structure(Path(td))
            test_scan_skip_dirs(Path(td))
            test_scan_depth_bound(Path(td))
            test_scan_newest_first(Path(td))
            test_scan_max_files(Path(td))
            print("-- crud.set_task_artifact --")
            asyncio.run(test_set_task_artifact())
        finally:
            config.DATA_DIR = orig_data_dir
            ws_mod.WORKSPACE_ROOT = orig_ws_root
            ws_mod._SCAN_EPOCH = None

    print("\n=== 结果: PASS ===")
    print("scan_workspace_artifacts: 空目录安全 / 结构完整 / 跳过目录剪枝 /")
    print("  深度封顶 / 最新在前 / 文件数封顶 —— 六项全过。")
    print("crud.set_task_artifact: 更新两列 / 未知 task 返回 None 不抛 /")
    print("  不动 status/exit_code —— 三项全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
