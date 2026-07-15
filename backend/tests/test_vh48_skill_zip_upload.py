"""VH48 回归：技能 zip/目录上传（Claude Skills 化 · 阶段三·task34）.

锁住 ``POST /api/skills/upload`` 的 zip 解包路径——SKILL.md→content +
scripts/+templates/→assets。镜像 Claude Skills「一技能一目录」自包含上传。

六段契约（直调 ``_upload_skill_zip`` helper + 真实 CRUD 往返，不依赖 live server）：
  1. flat 布局：SKILL.md+scripts+templates+README → content + 2 资产 + README 跳过
  2. 一层目录布局：myskill/SKILL.md+... → 前缀剥离正确
  3. 无 SKILL.md → 400 拒绝
  4. skill.md（小写）也识别
  5. zip bomb（解包总量超限）→ 413 拒绝
  6. 路径穿越条目被拒（合法资产仍落盘，不全失败）
"""
import asyncio
import io
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from api.skills import _upload_skill_zip  # noqa: E402
from store import crud, skill_assets  # noqa: E402


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    return buf.getvalue()


async def main():
    # 1) flat 布局：SKILL.md + scripts/run.sh + templates/tpl.md + 非资产 README.md
    z1 = make_zip({
        "SKILL.md": "# Flat skill\n哨兵标记 FLAT_OK".encode("utf-8"),
        "scripts/run.sh": b"#!/bin/bash\necho flat",
        "templates/tpl.md": "# tpl flat".encode("utf-8"),
        "README.md": b"this is not an asset",
    })
    s1 = await _upload_skill_zip(z1, "flat.zip", None, "flat desc", "custom", '["tag1"]')
    assert s1.content == "# Flat skill\n哨兵标记 FLAT_OK", s1.content
    assert set(s1.assets) == {"scripts/run.sh", "templates/tpl.md"}, s1.assets
    assert s1.name == "flat", s1.name
    assert s1.description == "flat desc"
    assert s1.tags == ["tag1"]
    print("[OK] 1 flat 布局：SKILL.md->content + 2 资产落盘 + README 跳过 + 元数据正确")
    await crud.delete_skill(s1.id)

    # 2) 一层目录布局：myskill/SKILL.md + myskill/scripts/...
    z2 = make_zip({
        "myskill/SKILL.md": "# Dir skill\n哨兵标记 DIR_OK".encode("utf-8"),
        "myskill/scripts/dep.sh": b"#!/bin/bash\nls",
        "myskill/templates/api.md": "# api tpl".encode("utf-8"),
    })
    s2 = await _upload_skill_zip(z2, "myskill.zip", None, None, "custom", None)
    assert s2.content == "# Dir skill\n哨兵标记 DIR_OK", s2.content
    assert set(s2.assets) == {"scripts/dep.sh", "templates/api.md"}, s2.assets
    print("[OK] 2 一层目录布局：前缀剥离正确，资产落 scripts/templates")
    await crud.delete_skill(s2.id)

    # 3) 无 SKILL.md -> 400
    z3 = make_zip({"scripts/x.sh": b"#!/bin/bash"})
    try:
        await _upload_skill_zip(z3, "noskill.zip", None, None, "custom", None)
        print("[FAIL] 3 无 SKILL.md 未拒绝")
        return 1
    except Exception as e:
        assert e.status_code == 400, f"want 400 got {e.status_code}: {e.detail}"; assert "SKILL.md" in str(e.detail), e.detail
        print("[OK] 3 无 SKILL.md -> 400 拒绝")

    # 4) 大写 SKILL.md 也可
    z4 = make_zip({"skill.md": "# lower case skill.md\nOK_LOWER".encode("utf-8")})
    s4 = await _upload_skill_zip(z4, "lc.zip", None, None, "custom", None)
    assert s4.content == "# lower case skill.md\nOK_LOWER"
    print("[OK] 4 skill.md（小写）也识别")
    await crud.delete_skill(s4.id)

    # 5) zip bomb 防护：zip 压缩率极高但解包总量超限 -> 413
    big = b"x" * (11 * 1024 * 1024)  # 11MB 解出来
    z5 = make_zip({"SKILL.md": "# big\nok".encode("utf-8"), "scripts/big.bin": big})
    try:
        await _upload_skill_zip(z5, "bomb.zip", None, None, "custom", None)
        print("[FAIL] 5 zip bomb 未拒绝")
        return 1
    except Exception as e:
        assert e.status_code == 413, f"want 413 got {e.status_code}: {e.detail}"; assert "超限" in str(e.detail) or "过大" in str(e.detail), e.detail
        print("[OK] 5 zip bomb（解包总量超限）-> 413 拒绝")

    # 6) 路径穿越条目（zip 内 scripts/../../etc/passwd）-> write_skill_asset 拒绝但不全失败
    z6 = make_zip({
        "SKILL.md": "# traversal\nok".encode("utf-8"),
        "scripts/../../etc/passwd": b"evil",
        "scripts/ok.sh": b"#!/bin/bash\nok",
    })
    s6 = await _upload_skill_zip(z6, "trav.zip", None, None, "custom", None)
    # 合法资产 ok.sh 应落盘，越界条目被记下但不全失败
    assert "scripts/ok.sh" in s6.assets, s6.assets
    # 越界条目不应出现
    assert all("passwd" not in a for a in s6.assets), s6.assets
    print("[OK] 6 路径穿越条目被拒（合法资产仍落盘，不全失败避免）")
    await crud.delete_skill(s6.id)

    print()
    print("结果: PASS (task34 zip 上传 6 例全过)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
