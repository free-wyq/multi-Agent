"""SK-12 自测：一键安装市场技能（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL/SK 自测模式（httpx HTTP 真源交叉验证）。

SK-12 架构关键点（先读代码确认 skills.py install_market_skill + skill_hub.get_market_entry
/ fetch_remote_entry_content）：
  - 前端只传 entry_id（POST body {"entry_id": ...}），后端按 entry_id 解析 content 全文：
      ① catalog 条目（entry_id="catalog:<slug>"）自带 content，直接落库；
      ② remote 条目（仅 source_url）best-effort 拉取（fetch_remote_entry_content），失败 409；
      ③ 未知 entry_id → 404；空 entry_id → 400；无 content → 409。
  - 落库调既有 crud.create_skill，source 标 "market"，返回本地 Skill（有 id/installed/mounted_to
    等本地字段），与 list/create 同类型可直接进「我的技能」。
  - idempotent-ish：重复安装创建副本（不查重），前端 isMarketInstalled name 判重做 UI 防重复。

验证七块（确定性断言非语义判断）：
  ① 正常安装 catalog 条目 → 200 + Skill（source=market）+ content == catalog 全文（单一真源）；
  ② 我的技能列表含新装 skill（GET /api/skills 真源交叉验证）；
  ③ 单读 GET /api/skills/{id} 回读 == install 响应（持久化一致）；
  ④ 重复安装创建副本（idempotent-ish，前端 name 判重由 UI 层负责，后端不查重）；
  ⑤ 未知 entry_id → 404；
  ⑥ 空 entry_id → 400；
  ⑦ content == SK-10 自测 market 端点返回的同 entry_id content（跨端点单一真源一致）。

为何不连 WS：SK-12 是同步 HTTP 接口（install → crud.create_skill 落库），不经引擎 inbox/WS
事件流，无实时事件可抓，纯 HTTP 校验即可（与 SK-01/SK-05 同构，比 PL 系列更简单）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 用一个已知存在的 catalog entry_id 做正常安装用例（skill_hub._CATALOG 第一条）。
ENTRY_ID = "catalog:db-migration"
ENTRY_NAME = "数据库迁移技能"

# 用作 content 真源比对的前缀片段（catalog:db-migration 的 content 第一行）。
CONTENT_PREFIX = "# 数据库迁移技能"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def install(entry_id: str) -> tuple[int, dict | None]:
    """POST /api/skills/market/install body={entry_id}，返回 (status, skill_or_error)。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            f"{BASE}/api/skills/market/install",
            json={"entry_id": entry_id},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_skills() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/skills")
        return r.json() if r.status_code == 200 else []


async def get_skill(skill_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/skills/{skill_id}")
        return r.json() if r.status_code == 200 else None


async def delete_skill(skill_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/skills/{skill_id}")
        return r.status_code == 200 and r.json() is True


async def get_market_entry_content(entry_id: str) -> str | None:
    """GET /api/skills/market 拉全量 catalog，取目标 entry_id 的 content 作为真源比对。"""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(f"{BASE}/api/skills/market", params={"q": "", "limit": 200})
        if r.status_code != 200:
            return None
        for e in r.json():
            if e.get("entry_id") == entry_id:
                return e.get("content")
    return None


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def main() -> int:
    print("=== SK-12 自测：一键安装市场技能 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    installed_ids: list[str] = []  # 收尾清理用

    # 安装前快照：记录已有 skills（供「列表含新装」精确比对，避免历史残留干扰）
    before_skills = await list_skills()
    before_ids = {s["id"] for s in before_skills}
    print(f"[pre] 安装前 skills 数：{len(before_skills)}")

    # ── 1. 正常安装 catalog 条目 → 200 + Skill source=market ──
    print("\n[check 1] 正常安装：POST install catalog:db-migration")
    status, skill = await install(ENTRY_ID)
    if not _check("HTTP 200", status == 200, f"status={status} body={skill}"):
        errs.append(f"[install] 非 200 status={status}")
    else:
        assert skill is not None
        new_id = skill.get("id", "")
        if new_id:
            installed_ids.append(new_id)

        ok_struct = (
            isinstance(skill.get("id"), str)
            and skill.get("id", "").startswith("skill_")
            and isinstance(skill.get("name"), str)
            and skill.get("name") == ENTRY_NAME
            and skill.get("source") == "market"
            and skill.get("installed") is True
            and isinstance(skill.get("tags"), list)
            and isinstance(skill.get("content"), str)
            and isinstance(skill.get("mounted_to"), list)
        )
        if _check("Skill 结构完整（id skill_ 前缀 / name / source=market / installed / tags / content / mounted_to）",
                  ok_struct):
            print(f"      样本：id={new_id} name={skill.get('name')!r} source={skill.get('source')!r}")
        else:
            errs.append(f"[install] Skill 结构异常：{skill}")

        # content 非空且 == catalog 全文（单一真源：install 落库 content 来自 skill_hub._CATALOG_INDEX）
        content = skill.get("content") or ""
        if not _check("install content 非空", bool(content)):
            errs.append("[install] content 为空")
        if not _check(f"install content 以 {CONTENT_PREFIX!r} 开头", content.startswith(CONTENT_PREFIX)):
            errs.append(f"[install] content 开头不符：{content[:40]!r}")

    # ── 2. 我的技能列表含新装 skill（真源交叉验证）──
    print("\n[check 2] 我的技能列表含新装 skill")
    if skill and skill.get("id"):
        after_skills = await list_skills()
        after_ids = {s["id"] for s in after_skills}
        # 新装 skill 必须在列表里
        in_list = skill["id"] in after_ids
        if _check(f"GET /api/skills 列表含新 skill {skill['id'][:18]}…", in_list):
            # 列表项里该 skill 的 name/source 一致
            listed = next((s for s in after_skills if s["id"] == skill["id"]), {})
            listed_ok = (
                listed.get("name") == skill.get("name")
                and listed.get("source") == "market"
            )
            if not _check("列表项 name/source == install 响应", listed_ok):
                errs.append(f"[list] 列表项漂移：listed={listed.get('name')}/{listed.get('source')}")
        else:
            errs.append("[list] 新装 skill 不在列表")

    # ── 3. 单读回读 == install 响应（持久化一致）──
    print("\n[check 3] 单读 GET /api/skills/{id} 回读一致")
    if skill and skill.get("id"):
        reread = await get_skill(skill["id"])
        if reread is None:
            _check("GET /api/skills/{id} 200", False)
            errs.append("[reread] 404 回读失败")
        else:
            consistent = (
                reread.get("id") == skill.get("id")
                and reread.get("name") == skill.get("name")
                and reread.get("source") == skill.get("source")
                and reread.get("content") == skill.get("content")
                and reread.get("tags") == skill.get("tags")
            )
            if _check("回读 id/name/source/content/tags 严格一致", consistent):
                pass
            else:
                errs.append(f"[reread] 回读漂移：{reread}")

    # ── 4. 重复安装创建副本（idempotent-ish，后端不查重）──
    print("\n[check 4] 重复安装创建副本（前端 name 判重由 UI 负责）")
    status2, skill2 = await install(ENTRY_ID)
    if status2 == 200 and skill2 and skill2.get("id"):
        installed_ids.append(skill2["id"])
        is_copy = (
            skill2["id"] != skill.get("id")  # 不同 id（副本）
            and skill2.get("name") == skill.get("name")  # 同名
            and skill2.get("source") == "market"
        )
        if _check("重复安装 → 不同 id 同名 source=market（副本）", is_copy,
                  f"id1={skill.get('id')[:12]} id2={skill2['id'][:12]}"):
            print(f"      副本 id={skill2['id']}（前端 isMarketInstalled 应判已安装 disable 按钮）")
        else:
            errs.append(f"[dup] 重复安装非副本：{skill2}")
    else:
        _check("重复安装 200", False, f"status={status2}")
        errs.append(f"[dup] 重复安装失败 status={status2}")

    # ── 5. 未知 entry_id → 404 ──
    print("\n[check 5] 未知 entry_id → 404")
    s404, _ = await install("catalog:nope-nope-not-exist")
    if _check("未知 entry_id → 404", s404 == 404, f"status={s404}"):
        pass
    else:
        errs.append(f"[404] 未知 entry_id status={s404} 非 404")

    # ── 6. 空 entry_id → 400 ──
    print("\n[check 6] 空 entry_id → 400")
    s400, _ = await install("")
    if _check("空 entry_id → 400", s400 == 400, f"status={s400}"):
        pass
    else:
        errs.append(f"[400] 空 entry_id status={s400} 非 400")

    # ── 7. install content == market 端点返回的同 entry_id content（跨端点单一真源）──
    print("\n[check 7] install content == market 端点 content（跨端点真源一致）")
    if skill and skill.get("content"):
        market_content = await get_market_entry_content(ENTRY_ID)
        if market_content is None:
            _check("market 端点取到目标 entry content", False)
            errs.append("[xref] market 端点未取到 entry content")
        else:
            same = skill["content"] == market_content
            if _check("install 落库 content == market 端点 content（逐字节相等）", same):
                print(f"      content 长度 {len(skill['content'])} 字，跨端点一致")
            else:
                errs.append(
                    f"[xref] content 不一致：install={skill['content'][:40]!r} "
                    f"market={market_content[:40]!r}"
                )

    # ── 收尾清理：删除所有本测试安装的 skill ──
    print(f"\n[cleanup] 删除 {len(installed_ids)} 个测试 skill")
    for sid in installed_ids:
        ok = await delete_skill(sid)
        if not ok:
            print(f"  ⚠️ 删除失败 {sid}")
            errs.append(f"[cleanup] 删除失败 {sid}")
    # 校验清理后无残留本测试 skill
    final_skills = await list_skills()
    leaked = [s for s in final_skills if s["id"] in installed_ids]
    if not _check("清理后无残留测试 skill", not leaked, f"{len(leaked)} 个残留"):
        errs.append(f"[cleanup] {len(leaked)} 个 skill 残留")

    # ── 汇总 ──
    print("\n" + "=" * 52)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 一键安装市场技能端到端打通：正常安装/列表/回读/重复副本/404/400/真源一致全过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
