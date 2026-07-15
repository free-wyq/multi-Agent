"""Skill assets directory storage (Claude Skills 化 · 阶段三·task33).

A skill is one self-contained directory: ``SKILL.md`` (→ ``content``, stored in
DB) + ``scripts/`` + ``templates/`` subdirs (→ assets, stored on disk under
``DATA_DIR/skills/{skill_id}/``). This mirrors Claude Agent Skills'
"one skill = one directory" layout (memory ``skill-system-claude-skills-port``):
SKILL.md carries the prose instructions, scripts/ holds runnable helpers,
templates/ holds reusable file templates.

Old content-only skills (created before this, no assets dir) keep working —
``list_skill_assets`` returns ``[]`` and the model's ``assets`` field is empty.

Security (hardened for stage-4 executability): all asset paths resolve under the
skill's own directory and only into the ``scripts``/``templates`` whitelist
subdirs; path traversal (``../../etc/passwd``) and arbitrary-top-level writes
are rejected by ``safe_asset_path``. Stage-4 (task40) adds a full audit.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger("multi-agent.skill_assets")

SKILLS_ROOT = Path(DATA_DIR) / "skills"

# Claude Skills 约定：资产只落在 scripts/（可运行辅助脚本）与 templates/（文件模板）
# 两个子目录下。白名单限定 top-level，防任意目录写入。
_ASSET_SUBDIRS = ("scripts", "templates")

# 资产上限（task34 上传 + 防撑爆）：单文件 1MB，单技能资产总额 10MB。
_MAX_SINGLE_ASSET = 1 * 1024 * 1024
_MAX_TOTAL_ASSETS = 10 * 1024 * 1024


def skill_dir_path(skill_id: str) -> Path:
    """Return (creating if needed) the on-disk directory for a skill's assets."""
    p = SKILLS_ROOT / skill_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_asset_path(skill_id: str, rel: str) -> Path:
    """Resolve ``rel`` to a path inside ``skills/{skill_id}/`` and enforce constraints.

    Rules:
    - The resolved path must live under the skill's own directory (no
      ``../`` traversal escapes — path-traversal protection, PL-12 style).
    - The top-level segment must be one of the whitelisted asset subdirs
      (``scripts`` / ``templates``). Arbitrary top-level files are rejected.

    Raises ``ValueError`` on violation. Empty ``rel`` is rejected (assets must
    live under a subdir, not the skill root).
    """
    if not rel or not skill_id:
        raise ValueError("skill_id 与资产相对路径均不可为空")
    base = skill_dir_path(skill_id).resolve()
    target = (base / rel).resolve()
    # 必须在 base 之内（== base 不算资产，资产要在子目录下）
    try:
        rel_posix = target.relative_to(base).as_posix()
    except ValueError as exc:
        raise ValueError(f"资产路径越界（逃出技能目录）: {rel}") from exc
    if not rel_posix:
        raise ValueError(f"资产路径解析为技能根目录（无效）: {rel}")
    top = rel_posix.split("/")[0]
    if top not in _ASSET_SUBDIRS:
        raise ValueError(
            f"资产只允许落在 scripts/ 或 templates/ 子目录（顶层 {top!r} 不合法）: {rel}"
        )
    return target


def _total_assets_bytes(skill_id: str) -> int:
    d = SKILLS_ROOT / skill_id
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def write_skill_asset(skill_id: str, rel: str, data: bytes) -> Path:
    """Write ``data`` to ``skills/{skill_id}/{rel}`` (enforced safe + bounded).

    Returns the written path. Raises ``ValueError`` on path violation or size
    limit breach (single-file or per-skill total cap).
    """
    if len(data) > _MAX_SINGLE_ASSET:
        raise ValueError(
            f"单资产过大（{len(data)} 字节，上限 {_MAX_SINGLE_ASSET} 字节）: {rel}"
        )
    if _total_assets_bytes(skill_id) + len(data) > _MAX_TOTAL_ASSETS:
        raise ValueError(
            f"技能资产总额超限（上限 {_MAX_TOTAL_ASSETS} 字节）: {rel}"
        )
    p = safe_asset_path(skill_id, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def read_skill_asset(skill_id: str, rel: str) -> bytes:
    """Read an asset's bytes (enforced safe path). Raises if missing."""
    p = safe_asset_path(skill_id, rel)
    if not p.exists():
        raise FileNotFoundError(f"资产不存在: {rel}")
    return p.read_bytes()


def list_skill_assets(skill_id: str) -> list[str]:
    """List relative asset paths under ``scripts/`` and ``templates/`` (sorted).

    Returns ``[]`` for skills with no assets dir (old content-only skills).
    """
    d = SKILLS_ROOT / skill_id
    if not d.exists():
        return []
    out: list[str] = []
    for sub in _ASSET_SUBDIRS:
        sd = d / sub
        if not sd.exists():
            continue
        for f in sorted(sd.rglob("*")):
            if f.is_file():
                out.append(str(f.relative_to(d)))
    return out


def delete_skill_assets(skill_id: str) -> None:
    """Remove the entire on-disk asset directory for a skill (on skill delete).

    No-op if the directory doesn't exist (content-only skill). Best-effort:
    filesystem errors are logged, not raised, so a stuck rmtree doesn't block a
    DB-side skill deletion (B31 错误处理重巡航——不静默吞，log 后继续).

    NOTE: this removes the whole ``skills/{skill_id}/`` tree, which since task37
    also contains the sandbox ``workspace/`` subdir. That is intentional — a
    deleted skill's workspace is ephemeral run state and should not survive
    the skill itself.
    """
    d = SKILLS_ROOT / skill_id
    if not d.exists():
        return
    try:
        shutil.rmtree(d)
    except Exception:  # noqa: BLE001
        logger.warning("[skill_assets] rmtree failed for %s", skill_id, exc_info=True)


# ── skill execution sandbox (Claude Skills 化 · 阶段四·task37) ──────────
# A skill that declares ``requires_tools`` gets a dedicated sandbox workspace
# under its own directory: ``DATA_DIR/skills/{skill_id}/workspace/``. The
# controlled tools (file_read/file_write/bash_run, see engine.tools.tools_for_skill)
# are cwd-bound here; products land in ``workspace/output/``. Path-safety
# mirrors ``engine.workspace.safe_path`` (no escape via ``../``).
#
# MVP = 目录隔离（cwd 限制 + 路径校验）+ bash denylist。LocalWorkspace 直接操作
# 本地 FS 是安全债——真正的隔离要容器/受限 shell（见 sandbox-design 记忆·长期债）。

_SKILL_WORKSPACE_SUBDIR = "workspace"
_SKILL_OUTPUT_SUBDIR = "output"


def skill_workspace_path(skill_id: str) -> Path:
    """Return (creating if needed) the sandbox workspace dir for a skill.

    ``DATA_DIR/skills/{skill_id}/workspace/`` — sibling to the skill's
    ``scripts/`` + ``templates/`` asset dirs. The ``output/`` subdir is created
    alongside so the run endpoint (task38) has a stable products location to
    report back. Idempotent (``mkdir parents=True, exist_ok=True``).
    """
    if not skill_id:
        raise ValueError("skill_id 不可为空")
    p = SKILLS_ROOT / skill_id / _SKILL_WORKSPACE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    (p / _SKILL_OUTPUT_SUBDIR).mkdir(parents=True, exist_ok=True)
    return p


def skill_output_path(skill_id: str) -> Path:
    """Return (creating if needed) the ``workspace/output/`` products dir."""
    p = skill_workspace_path(skill_id) / _SKILL_OUTPUT_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_skill_path(skill_id: str, rel: str) -> Path:
    """Resolve ``rel`` to a path inside the skill's sandbox workspace.

    ``rel`` may be empty (the workspace root itself), a relative path, or even
    an absolute path — but the final resolved path must live inside the
    workspace root. Anything that escapes (e.g. ``../../etc/passwd``) raises
    ``ValueError``. Mirrors ``engine.workspace.safe_path`` so the two workspace
    types share one path-safety contract (no drift).
    """
    if not skill_id:
        raise ValueError("skill_id 不可为空")
    ws = skill_workspace_path(skill_id).resolve()
    if not rel or rel == ".":
        return ws
    candidate = (ws / rel).resolve()
    try:
        candidate.relative_to(ws)
    except ValueError as exc:
        raise ValueError(
            f"路径越界（逃出技能工作区）: {rel}"
        ) from exc
    return candidate
