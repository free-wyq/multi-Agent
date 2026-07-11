"""SK-05 自测：上传 SKILL.md 文件入库（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL/SK 自测模式（httpx HTTP 真源交叉验证）。

SK-05 链路：
  POST /api/skills/upload (multipart/form-data)
    → skills.py upload_skill(file: UploadFile, name/description/source/tags: Form)
    → 读文件内容 → UTF-8 解码 → strip → 校验非空/1MB 上限
    → name 缺省回退 Path(filename).stem（去 .md/.markdown 扩展名）
    → tags 是 JSON 编码字符串数组，json.loads 解析 + 类型校验
    → 构造 SkillCreatePayload → crud.create_skill 持久化（source=custom）
    → 返回 Skill

本自测验证「上传 SKILL.md 入库」全链路：
  正常用例（带哨兵标记，对齐 PL-06/PL-12 哨兵法）：
    1. POST /api/skills/upload multipart file + name + description + tags(JSON) → 200 + Skill。
    2. Skill 结构完整：id 非空、name 非空、source=="custom"、tags 是 list、installed 为真。
    3. content == 原文件内容（含哨兵标记）——上传是「文件→content」直传，content 必须与
       原文件字节一致（非 LLM 生成），是最强确定性证据。
    4. name 回退 stem：不传 name 时，name == 文件名去扩展名（如 sk05_probe.md → sk05_probe）。
    5. 持久化交叉验证：GET /api/skills/{id} 回读 == 上传响应；GET /api/skills 列表含该 id。
  错误用例（边界校验）：
    6. 空文件 → 400。
    7. 非法 tags（非 JSON 数组字符串）→ 400。
    8. 超大文件（>1MB）→ 413。
    9. 非 UTF-8 文件 → 400。

收尾：DELETE /api/skills/{id} 清理，避免污染后续自测（SK-09 等会 list 技能）。

为何不连 WS：SK-05 是同步 HTTP 接口（upload 内部读文件 + create_skill 完成才返回），
不经过引擎 inbox/WS 事件流，无实时事件可抓，纯 HTTP 校验即可。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 哨兵标记：上传文件内容里植入一串固定串，回读 content 含该串即证明「上传文件→content 直传」
# 路径正确（非 LLM 生成、非 fallback）。worker 不可能凭空写出这串固定标记。
SIGNATURE = "SK05_UPLOAD_SIGNATURE_OK"

# 上传文件内容：标准技能文档结构（含 Markdown 标题 + 哨兵标记 + 多行内容，覆盖多行解码）。
UPLOAD_BODY = f"""# SK-05 自测技能

{SIGNATURE}

## 用途
用于 SK-05 上传自测的探针技能文档，验证 multipart 上传→入库→回读链路。

## 适用场景
当需要验证「上传 SKILL.md 文件作为技能入库」功能时使用。

## 使用步骤
1. 准备一份 Markdown 技能文档
2. 通过上传入口提交
3. 系统读取文件内容作为技能 content 入库

## 注意事项
本文件含哨兵标记，回读 content 必须与原文件逐字节一致。
"""

# 上传文件名（含 .md 扩展名，用于验证 name 回退 stem）
UPLOAD_FILENAME = "sk05_probe.md"
EXPECTED_STEM = "sk05_probe"  # Path("sk05_probe.md").stem

# 超大文件内容（> 1MB 触发 413）
_OVERSIZE = b"x" * (1 * 1024 * 1024 + 100)

# 非 UTF-8 内容（Latin-1 无法 decode 的字节序列，触发 UTF-8 解码失败 → 400）
_NON_UTF8 = b"\xff\xfe\x00bad utf8 \x80\x81"

# 上传超时（multipart 文件读取 + create_skill 持久化，很快，但留余量）
UPLOAD_TIMEOUT = 30.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def upload_skill(
    *,
    content: bytes | str,
    filename: str = UPLOAD_FILENAME,
    name: str | None = None,
    description: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    tags_raw: str | None = None,
) -> httpx.Response:
    """POST /api/skills/upload multipart。

    tags_raw 用于直接传非法 JSON 串测 400（绕过正常 JSON 编码路径）。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    data: dict[str, str] = {}
    if name is not None:
        data["name"] = name
    if description is not None:
        data["description"] = description
    if source is not None:
        data["source"] = source
    if tags_raw is not None:
        data["tags"] = tags_raw
    elif tags is not None:
        import json
        data["tags"] = json.dumps(tags)
    async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as c:
        return await c.post(
            f"{BASE}/api/skills/upload",
            files={"file": (filename, content, "text/markdown")},
            data=data or None,
        )


async def get_skill(skill_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/skills/{skill_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def list_skills() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/skills")
        r.raise_for_status()
        return r.json()


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== SK-05 自测：上传 SKILL.md 入库 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    created_ids: list[str] = []

    try:
        # ── 正常用例：带 name + description + tags 的完整上传 ──
        print("\n[case 1] 正常上传（name + description + tags）")
        resp = await upload_skill(
            content=UPLOAD_BODY,
            filename=UPLOAD_FILENAME,
            name="SK05探针技能",
            description="SK-05 上传自测探针",
            tags=["sk05", "upload", "探针"],
        )
        if resp.status_code != 200:
            errs.append(f"[case1] 上传期望 200，实得 {resp.status_code}: {resp.text[:200]}")
            print(f"  ✗ 上传失败: {resp.status_code} {resp.text[:200]}")
        else:
            skill = resp.json()
            skill_id = skill.get("id", "")
            if skill_id:
                created_ids.append(skill_id)
            print(f"  [upload] 返回 skill id={skill_id[:16]}... name={skill.get('name')!r}")

            # 校验 1：Skill 结构完整
            if not _check("id 非空", bool(skill_id), f"id={skill_id!r}"):
                errs.append("[case1] 上传返回的 Skill id 为空")
            if not _check("name 非空", bool(skill.get("name")), f"name={skill.get('name')!r}"):
                errs.append("[case1] 上传返回的 Skill name 为空")
            if not _check("source == custom", skill.get("source") == "custom",
                          f"source={skill.get('source')!r}"):
                errs.append(f"[case1] source 非 custom: {skill.get('source')!r}")
            if not _check("tags 是 list", isinstance(skill.get("tags"), list),
                          f"tags={skill.get('tags')!r}"):
                errs.append(f"[case1] tags 非 list: {type(skill.get('tags'))}")
            else:
                print(f"  tags = {skill.get('tags')}")
            if not _check("installed 为真", skill.get("installed") in (True, 1),
                          f"installed={skill.get('installed')!r}"):
                errs.append(f"[case1] installed 非 真: {skill.get('installed')!r}")

            # 校验 2：content == 原文件内容（含哨兵标记）——最强确定性证据
            content = skill.get("content") or ""
            if not _check("content 非空", bool(content), "content 为空"):
                errs.append("[case1] 上传返回 content 为空")
            else:
                print(f"  content 长度 = {len(content)} 字符")
            has_sig = SIGNATURE in content
            if not _check("content 含哨兵标记", has_sig, f"未找到 {SIGNATURE!r}"):
                errs.append("[case1] content 不含哨兵标记，文件内容未正确直传为 content")
            # 上传是文件→content 直传，strip() 后应与原 body strip() 一致
            content_match = content == UPLOAD_BODY.strip()
            if not _check("content == 原文件内容(strip 后)", content_match,
                          f"长度 {len(content)} vs {len(UPLOAD_BODY.strip())}"):
                errs.append("[case1] content 与原文件内容不一致（非逐字节直传）")

            # 校验 5：持久化交叉验证——GET /api/skills/{id} 回读
            if skill_id:
                reread = await get_skill(skill_id)
                if reread is None:
                    _check("GET /api/skills/{id} 回读存在", False, "404")
                    errs.append(f"[case1] GET /api/skills/{skill_id} 返回 404（未持久化）")
                else:
                    same = (reread.get("id") == skill_id
                            and reread.get("name") == skill.get("name")
                            and reread.get("content") == content
                            and reread.get("tags") == skill.get("tags"))
                    if _check("GET 回读 == 上传响应", same,
                              f"name={reread.get('name')!r} vs {skill.get('name')!r}"):
                        print("  [check 5] 持久化回读一致")
                    else:
                        errs.append("[case1] GET 回读与上传响应不一致（未正确持久化？）")

                # 校验 5b：列表含该 skill
                skills = await list_skills()
                ids = [s.get("id") for s in skills]
                if not _check("GET /api/skills 列表含该 id", skill_id in ids,
                              f"列表 {len(ids)} 项不含 {skill_id[:16]}..."):
                    errs.append("[case1] 上传后的 skill 未出现在 /api/skills 列表中")
                else:
                    print(f"  [check 5b] 列表含该 skill（共 {len(ids)} 项）")

        # ── 正常用例 2：name 回退 stem（不传 name）──
        print("\n[case 2] name 回退文件 stem（不传 name）")
        resp2 = await upload_skill(
            content=UPLOAD_BODY,
            filename=UPLOAD_FILENAME,
            description="测 name 回退 stem",
            tags=["sk05-stem"],
        )
        if resp2.status_code != 200:
            errs.append(f"[case2] 上传期望 200，实得 {resp2.status_code}: {resp2.text[:200]}")
            print(f"  ✗ 上传失败: {resp2.status_code} {resp2.text[:200]}")
        else:
            skill2 = resp2.json()
            sid2 = skill2.get("id", "")
            if sid2:
                created_ids.append(sid2)
            name2 = skill2.get("name", "")
            if not _check("name 回退为文件 stem", name2 == EXPECTED_STEM,
                          f"name={name2!r} != stem={EXPECTED_STEM!r}"):
                errs.append(f"[case2] name 未回退 stem: {name2!r} (期望 {EXPECTED_STEM!r})")
            else:
                print(f"  name = {name2!r}（回退自 {UPLOAD_FILENAME}）")

        # ── 错误用例 1：空文件 → 400 ──
        print("\n[case 3] 空文件 → 400")
        resp3 = await upload_skill(content=b"", filename="empty.md")
        if not _check("空文件 → 400", resp3.status_code == 400,
                      f"实得 {resp3.status_code}: {resp3.text[:120]}"):
            errs.append(f"[case3] 空文件期望 400，实得 {resp3.status_code}")

        # ── 错误用例 2：非法 tags（非 JSON 数组）→ 400 ──
        print("\n[case 4] 非法 tags（非 JSON）→ 400")
        resp4 = await upload_skill(
            content=UPLOAD_BODY,
            filename="badtags.md",
            tags_raw="not-a-json-array",
        )
        if not _check("非法 tags → 400", resp4.status_code == 400,
                      f"实得 {resp4.status_code}: {resp4.text[:120]}"):
            errs.append(f"[case4] 非法 tags 期望 400，实得 {resp4.status_code}")
            # 若误入库需清理
            if resp4.status_code == 200:
                created_ids.append(resp4.json().get("id", ""))

        # ── 错误用例 3：超大文件 >1MB → 413 ──
        print("\n[case 5] 超大文件 (>1MB) → 413")
        resp5 = await upload_skill(content=_OVERSIZE, filename="oversize.md")
        if not _check("超大文件 → 413", resp5.status_code == 413,
                      f"实得 {resp5.status_code}: {resp5.text[:120]}"):
            errs.append(f"[case5] 超大文件期望 413，实得 {resp5.status_code}")
            if resp5.status_code == 200:
                created_ids.append(resp5.json().get("id", ""))

        # ── 错误用例 4：非 UTF-8 文件 → 400 ──
        print("\n[case 6] 非 UTF-8 文件 → 400")
        resp6 = await upload_skill(content=_NON_UTF8, filename="binary.md")
        if not _check("非 UTF-8 → 400", resp6.status_code == 400,
                      f"实得 {resp6.status_code}: {resp6.text[:120]}"):
            errs.append(f"[case6] 非 UTF-8 期望 400，实得 {resp6.status_code}")
            if resp6.status_code == 200:
                created_ids.append(resp6.json().get("id", ""))

    finally:
        # 收尾清理：删除所有上传成功的测试 skill，避免污染后续自测（SK-09 会 list 技能）
        print("\n[cleanup] 清理上传的测试 skill...")
        for sid in created_ids:
            try:
                ok = await delete_skill(sid)
                print(f"  删除 {sid[:16]}... → {ok}")
            except Exception as e:
                print(f"  删除 {sid[:16]}... 失败（非致命）: {e}")

    if errs:
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1

    print("\n=== 结果: PASS ===")
    print("SK-05 上传 SKILL.md 入库 全链路验证通过：")
    print("  · POST /api/skills/upload multipart(file+name+desc+tags) → 200 + Skill 结构完整")
    print("    （id/name/source=custom/tags=list/installed=真）；")
    print("  · content == 原文件内容（含哨兵标记，逐字节直传非 LLM 生成）；")
    print("  · name 缺省回退文件 stem（去 .md 扩展名）；")
    print("  · 持久化交叉验证：GET /api/skills/{id} 回读一致 + 列表含该 skill；")
    print("  · 边界校验：空文件→400 / 非法 tags→400 / 超大文件→413 / 非 UTF-8→400。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
