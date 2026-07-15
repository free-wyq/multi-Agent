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
    """
    d = SKILLS_ROOT / skill_id
    if not d.exists():
        return
    try:
        shutil.rmtree(d)
    except Exception:  # noqa: BLE001
        logger.warning("[skill_assets] rmtree failed for %s", skill_id, exc_info=True)
