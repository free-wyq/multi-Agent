"""Group workspace directory management.

Each group gets a dedicated workspace directory under ``DATA_DIR/workspaces/``.
File tools operate inside this directory and ``safe_path`` enforces that no
resolved path escapes the workspace root (path-traversal protection).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

WORKSPACE_ROOT = Path(DATA_DIR) / "workspaces"

# PL-12 artifact scan bounds. A worker may generate large dependency trees
# (e.g. ``node_modules``) that must not dominate the manifest or blow up the
# DB row. ``_MAX_DEPTH`` bounds the recursion, ``_MAX_FILES`` caps the count,
# and ``_SKIP_DIRS`` are pruned wholesale (well-known non-artifact trees).
_MAX_DEPTH = 4
_MAX_FILES = 200
_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    ".next", "dist", "build", ".cache", "target",
}


def workspace_path(group_id: str) -> Path:
    """Return (creating if needed) the workspace directory for a group."""
    p = WORKSPACE_ROOT / group_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_path(group_id: str, rel: str) -> Path:
    """Resolve ``rel`` to a path inside the group workspace.

    ``rel`` may be empty (meaning the workspace root itself), a relative path,
    or even an absolute path — but the final resolved path must live inside the
    workspace root. Anything that escapes (e.g. ``../../etc/passwd``) raises
    ``ValueError``.
    """
    ws = workspace_path(group_id)
    # Treat empty / "." as the workspace root itself.
    if not rel or rel == ".":
        return ws
    candidate = (ws / rel).resolve()
    # Ensure the resolved candidate is still inside the workspace root.
    try:
        candidate.relative_to(ws.resolve())
    except ValueError as exc:
        raise ValueError(
            f"path '{rel}' escapes workspace root for group '{group_id}'"
        ) from exc
    return candidate


# ── artifact scanning (PL-12) ────────────────────────────────────────────


# timestamp of the registry process start — used by scan_workspace_artifacts
# to distinguish files that exist before a task from those produced during it.
# Set lazily on first scan if missing (defensive against import-order races
# when the engine module is loaded before this module).
_SCAN_EPOCH: float | None = None


def _get_scan_epoch() -> float:
    """Return (lazily initializing) the registry-process epoch timestamp.

    A module-level float captured at first call; used as the cutoff so a scan
    only reports files whose mtime is *at or after* this epoch. The epoch is
    the process start time, which precedes every task the engine has run —
    so all workspace files existing at process boot are included as
    baseline artifacts, and new files produced by a task are included too.
    """
    global _SCAN_EPOCH
    if _SCAN_EPOCH is None:
        # Use stat() of the workspace root dir itself as a stable, filesystem
        # timebase rather than time.time() — avoids wall-clock drift and is
        # guaranteed <= the mtimes of files created inside it after boot.
        _SCAN_EPOCH = WORKSPACE_ROOT.stat().st_mtime
    return _SCAN_EPOCH


def scan_workspace_artifacts(group_id: str) -> dict[str, list[dict]]:
    """Scan the group workspace for file artifacts (PL-12).

    Walks the workspace tree (shallow depth-bounded) and returns a dict
    describing the files present, so the engine can record them on the
    completing task's ``artifact_path`` (single primary path) and
    ``artifact`` (structured manifest) fields.

    The walk is limited to ``_MAX_DEPTH`` levels and ``_MAX_FILES`` entries
    to stay cheap — a worker that writes thousands of generated files (e.g.
    ``node_modules``) won't blow up the manifest or the DB row.

    Returns::

        {
            "files": [
                {"name": "report.md",
                 "path": "report.md",      # workspace-relative, POSIX
                 "size": 1234,
                 "modified_at": "2026-...Z"},
                ...
            ],
        }

    ``path`` is workspace-relative (POSIX separators) so it pairs cleanly with
    the download endpoint ``GET /api/groups/{id}/files/{name}``. ``name`` is the
    file's basename. The primary ``artifact_path`` recorded on the task is the
    first (newest-first) file's relative path, or ``None`` if the workspace is
    empty — so the task card shows the most relevant artifact.
    """
    ws = workspace_path(group_id)
    if not ws.exists():
        return {"files": []}

    files: list[dict] = []
    root = ws.resolve()
    cutoff = _get_scan_epoch()

    # depth-limited walk: collect files, skip heavy/irrelevant trees.
    # rglob yields entries depth-first; we resolve each relative to the
    # workspace root both to compute its depth (for the bound) and to prune
    # well-known generated trees (node_modules etc.) so they can't dominate
    # the manifest. Resolving once and reusing the parts list keeps this
    # cheap even when the workspace holds a large dependency tree.
    for entry in sorted(ws.rglob("*")):
        if len(files) >= _MAX_FILES:
            break
        try:
            rel_parts = entry.resolve().relative_to(root).parts
        except ValueError:
            continue
        if len(rel_parts) > _MAX_DEPTH:
            continue
        # Skip well-known non-artifact trees so they don't dominate the
        # manifest (a generated node_modules easily holds 10k+ files).
        # rel_parts[:-1] is the parent dir chain; if any is a skip dir,
        # the file lives under a pruned tree.
        if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        rel_posix = "/".join(rel_parts)
        files.append(
            {
                "name": rel_parts[-1],
                "path": rel_posix,
                "size": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )

    # newest first — so the primary artifact_path points at the most recently
    # produced file (the one the task most likely just wrote), not a stale
    # pre-existing file. Ties broken by name for determinism.
    files.sort(key=lambda f: (f["modified_at"], f["path"]), reverse=True)
    return {"files": files}

