"""Group workspace directory management.

Each group gets a dedicated workspace directory under ``DATA_DIR/workspaces/``.
File tools operate inside this directory and ``safe_path`` enforces that no
resolved path escapes the workspace root (path-traversal protection).
"""
from __future__ import annotations

from pathlib import Path

from config import DATA_DIR

WORKSPACE_ROOT = Path(DATA_DIR) / "workspaces"


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
