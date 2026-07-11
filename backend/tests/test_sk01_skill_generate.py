"""SK-01 自测：自然语言描述生成标准技能文档（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 PL/SK 自测模式（httpx HTTP 真源交叉验证）。

SK-01 链路：
  POST /api/skills/generate {description}
    → skills.py generate_skill
    → _generate_skill_via_llm：调 chat_completion（_GENERATE_PROMPT 要求 LLM 按
      {name, description, content(Markdown: 用途/适用场景/使用步骤/注意事项), tags}
      纯 JSON 回复）→ extract_json 解析
    → crud.create_skill 持久化（source=custom）
    → 返回 Skill（id/name/description/content/tags/source）

本自测验证「自然语言 → 标准技能文档」全链路：
  1. POST /api/skills/generate 带一段具体自然语言描述（含可识别关键词），返回 200 + Skill。
  2. 返回的 Skill 结构完整：id 非空、name 非空、source=="custom"、tags 是 list。
  3. content 是标准技能文档（Markdown，含 # 标题分节）——这是「LLM 真的生成了结构化文档」
     的确定性证据：fallback 路径（LLM 失败）会把 content 设成原 description 裸文本，
     无 Markdown 标题分节，可据此区分「真生成」vs「降级回显」。
  4. content 含 prompt 规定的标准小节（用途/适用场景/使用步骤/注意事项）至少 2 个——
     证明 LLM 遵循了「标准技能文档」结构规范，而非自由发挥。
  5. content 语义相关：含描述里的关键词（提交/规范/commit）——证明是针对该描述生成，
     非万能模板。
  6. 持久化交叉验证：GET /api/skills/{id} 回读 == 生成响应；GET /api/skills 列表含该 id。

收尾：DELETE /api/skills/{id} 清理，避免污染后续自测（SK-05/09 等会 list 技能）。

为何不连 WS：SK-01 是同步 HTTP 接口（generate 内部 await LLM 完成才返回），
不经过引擎 inbox/WS 事件流，无实时事件可抓，纯 HTTP 校验即可，比 PL 系列更简单。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 描述带可识别关键词（提交/规范/Conventional Commits），便于校验生成内容语义相关。
# 选「Git 提交规范检查」这类具体场景而非泛泛「一个技能」，迫使 LLM 产出针对性文档，
# 万能模板无法通过关键词校验。
DESC = (
    "生成一个用于 Git 提交规范检查的技能：检查提交信息是否符合约定式提交规范"
    "（Conventional Commits，如 feat:/fix:/docs: 前缀），不符合时给出修改建议。"
)

# prompt 里规定的标准技能文档四小节标题关键词——LLM 按规范生成则 content 含其中若干。
# 用「子串包含」而非精确匹配（LLM 可能写「## 适用场景」或「### 适用场景」或「## 二、适用场景」），
# 只要含「适用场景」等关键词即算该小节存在。
STD_SECTIONS = ["用途", "适用场景", "使用步骤", "注意事项"]

# 描述里的关键词——生成内容应语义相关（含至少一个），证明非万能模板。
DESC_KEYWORDS = ["提交", "规范", "commit", "Conventional", "约定式"]

# LLM 调用可能较慢（生成 Markdown 文档），给足超时。
GEN_TIMEOUT = 120.0


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def generate_skill(description: str) -> dict:
    async with httpx.AsyncClient(timeout=GEN_TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/skills/generate",
            json={"description": description},
        )
        r.raise_for_status()
        return r.json()


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
    print("=== SK-01 自测：自然语言描述生成标准技能文档 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []

    # 生成
    try:
        skill = await generate_skill(DESC)
    except Exception as e:
        print(f"[fatal] generate 请求失败: {e}")
        return 2
    print(f"[generate] 返回 skill id={skill.get('id', '')[:16]}...")

    skill_id = skill.get("id", "")
    created = False
    try:
        # 校验 1：Skill 结构完整
        if not _check("id 非空", bool(skill_id), f"id={skill_id!r}"):
            errs.append("生成的 Skill id 为空")
        if not _check("name 非空", bool(skill.get("name")), f"name={skill.get('name')!r}"):
            errs.append("生成的 Skill name 为空")
        else:
            print(f"  name = {skill.get('name')!r}")
        if not _check("source == custom", skill.get("source") == "custom",
                      f"source={skill.get('source')!r}"):
            errs.append(f"source 非 custom: {skill.get('source')!r}")
        if not _check("tags 是 list", isinstance(skill.get("tags"), list),
                      f"tags={skill.get('tags')!r}"):
            errs.append(f"tags 非 list: {type(skill.get('tags'))}")
        else:
            print(f"  tags = {skill.get('tags')}")

        content = skill.get("content") or ""
        # 校验 2：content 非空
        if not _check("content 非空", bool(content), "content 为空"):
            errs.append("生成的 content 为空（LLM 未产出文档？）")
        else:
            print(f"  content 长度 = {len(content)} 字符")

        # 校验 3：content 是 Markdown（含 # 标题分节）——确定性区分「LLM 真生成」vs「fallback 回显」
        # fallback 路径 content=原 description 裸文本，无 # 标题。
        has_md_headers = "# " in content or "\n#" in content
        if not _check("content 是 Markdown（含 # 标题）", has_md_headers,
                      "无 # 标题，疑似 fallback 裸文本"):
            errs.append("content 无 Markdown 标题，疑似 LLM 失败走 fallback 回显原描述")

        # 校验 4：content 含 prompt 规定的标准小节至少 2 个
        present_sections = [s for s in STD_SECTIONS if s in content]
        if not _check("content 含标准小节 ≥2 个",
                      len(present_sections) >= 2,
                      f"仅含 {present_sections}"):
            errs.append(f"content 未含标准小节≥2（仅 {present_sections}），LLM 未遵循文档结构规范")
        else:
            print(f"  含标准小节: {present_sections}")

        # 校验 5：content 语义相关（含描述关键词至少 1 个）
        hit_keywords = [k for k in DESC_KEYWORDS if k.lower() in content.lower()]
        if not _check("content 语义相关（含描述关键词 ≥1）",
                      len(hit_keywords) >= 1,
                      f"未命中任何关键词 {DESC_KEYWORDS}"):
            errs.append("content 不含描述关键词，疑似万能模板非针对描述生成")
        else:
            print(f"  命中关键词: {hit_keywords}")

        created = True

        # 校验 6：持久化交叉验证——GET /api/skills/{id} 回读
        if skill_id:
            reread = await get_skill(skill_id)
            if reread is None:
                _check("GET /api/skills/{id} 回读存在", False, "404")
                errs.append(f"GET /api/skills/{skill_id} 返回 404（未持久化）")
            else:
                same = (reread.get("id") == skill_id
                        and reread.get("name") == skill.get("name")
                        and reread.get("content") == content)
                if _check("GET 回读 == 生成响应", same,
                          f"name={reread.get('name')!r} vs {skill.get('name')!r}"):
                    print("[check 6] 持久化回读一致")
                else:
                    errs.append("GET 回读与生成响应不一致（未正确持久化？）")

            # 校验 7：列表含该 skill
            skills = await list_skills()
            ids = [s.get("id") for s in skills]
            if not _check("GET /api/skills 列表含该 id", skill_id in ids,
                          f"列表 {len(ids)} 项不含 {skill_id[:16]}..."):
                errs.append("生成后的 skill 未出现在 /api/skills 列表中")
            else:
                print(f"[check 7] 列表含该 skill（共 {len(ids)} 项）")
    finally:
        # 收尾清理：删除生成的 skill，避免污染后续自测（SK-05/09 会 list 技能）
        if created and skill_id:
            try:
                ok = await delete_skill(skill_id)
                print(f"[cleanup] 删除 skill {skill_id[:16]}... → {ok}")
            except Exception as e:
                print(f"[cleanup] 删除失败（非致命）: {e}")

    if errs:
        print("\n=== 结果: FAIL ===")
        for e in errs:
            print(f"  - {e}")
        return 1

    print("\n=== 结果: PASS ===")
    print("SK-01 自然语言描述生成标准技能文档 全链路验证通过：")
    print("  · POST /api/skills/generate 带自然语言描述 → 200 + Skill 结构完整")
    print("    （id/name/source=custom/tags=list）；")
    print("  · content 是 Markdown 标准技能文档（含 # 标题分节，非 fallback 裸文本）；")
    print("  · content 含 prompt 规定的标准小节（用途/适用场景/使用步骤/注意事项）≥2 个，")
    print("    证明 LLM 遵循了「标准技能文档」结构规范；")
    print("  · content 语义相关（含描述关键词），证明针对描述生成非万能模板；")
    print("  · 持久化交叉验证：GET /api/skills/{id} 回读一致 + 列表含该 skill。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
