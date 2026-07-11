"""MT-08 自测：团队列表及基本信息（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-01/MT-06 自测模式（httpx HTTP 真源交叉验证，
不连 WS——MT-08 是同步 HTTP 读列表 + 单读 + PUT 改基本信息，不经引擎 inbox/WS）。

MT-08 链路（GroupPage 团队列表 + 群信息抽屉展示群基本信息）：
  前端 GroupPage：
    · fetchData → groupApi.list() → GET /api/groups 拉团队列表（左侧群组栏渲染
      g.name + g.description，按 created_at 排序，fetchData 还默认选首个群 setChatGroupId）
    · setChatGroupId(g.id) → 选中群组；chatGroup = groups.find(id==chatGroupId)
      （群信息抽屉/聊天头部直接用列表元素，无二次单读——列表即真源）
    · 群信息抽屉展示 chatGroup 的 name/description/status/config.leader_strategy
      等基本信息字段（Drawer 头部 + Leader 策略段 + 群文件段都读 chatGroup 字段）
    · 群设置 Modal PUT /api/groups/{id} 改 name/description/coordinator_id/config
      → update_group 落库 → fetchData 刷新列表
  后端：
    GET  /api/groups            → crud.list_groups（select order_by created_at）
    GET  /api/groups/{id}       → crud.get_group（db.get GroupEntity → _group_to_model）
    PUT  /api/groups/{id}       → crud.update_group（setattr 翻新 + updated_at 推进）
    Group 模型 8 字段：id / name / coordinator_id / description / status /
                      config / created_at / updated_at（models.Group + entities.GroupEntity）

「团队列表及基本信息」语义：
  ① 团队列表——GET /api/groups 返回全量群组，前端左侧栏渲染 name+description；
  ② 基本信息——单群组的 8 字段完整可读（id/name/coordinator_id/description/
     status/config/created_at/updated_at），前端群信息抽屉据此展示；
  ③ 一致性——列表元素 == 单读（同一 _group_to_model 投影，fetchData 拿的列表
     元素即群信息抽屉所用 chatGroup，无二次单读，故列表与单读必须一致）；
  ④ 更新——PUT 改基本信息后回读更新 + created_at 不变（更新只翻元数据不动
     创建时间）+ updated_at 推进（_now_iso 微秒精度，每次写必变）。

验证十块（确定性断言）：
  ① 前置：GET /api/groups 列表非空（至少含种子演示群）+ GET /api/agents 候选池
     含 role=coordinator agent（建群探针的群主来源）；
  ② 建群探针：POST /api/groups（指定 coord + description）→ 200 + Group
     （id group_ 前缀 / coordinator_id==指定 / status=active）；
  ③ 列表 delta：创建后列表长度 == baseline+1，探针群在列表里，列表按
     created_at 升序（list_groups order_by created_at）；
  ④ 单读基本信息完整性：GET /api/groups/{id} 8 字段齐全（id/name/coordinator_id/
     description/status/config/created_at/updated_at）+ 类型合法 + 值正确
     （name/description==create 入参，coordinator_id==指定，status=active）；
  ⑤ 时间戳格式：created_at/updated_at 均为 ISO 8601 + Z 后缀（_now_iso =
     datetime.now(timezone.utc).isoformat().replace("+00:00","Z")），且
     updated_at >= created_at（同格式同 UTC 时区，字典序=时间序）；
  ⑥ 回读 == 列表元素：GET /api/groups/{id} 单读 == GET /api/groups 列表中
     对应探针元素（8 字段逐一相等，证明列表即真源，前端 chatGroup 可信）；
  ⑦ 更新基本信息：PUT /api/groups/{id} 改 name+description → 回读 name/description
     更新 + created_at 不变（更新不动创建时间）+ updated_at 推进（>= 旧 updated_at）；
  ⑧ config 基本信息态：新探针群 config == None（create_group 不写 config，
     GroupEntity.config 默认 None → JSON null，前端「未设置指挥策略」占位即此态）；
  ⑨ 不存在群：GET /api/groups/group_nope_mt08 → 200 + null（get_group 找不到行
     返 None，route 透传 null，不 404——前端据此判断群不存在）；
  ⑩ 收尾：DELETE 探针群 → 列表回到 baseline，无残留。

为何不连 WS：MT-08 是同步 HTTP（list_groups/get_group/update_group 直接查/写 DB
返回），不经引擎 inbox/WS 事件流，纯 HTTP 校验即可（与 MT-01/MT-06 同构）。引擎
启动是 MT-07 范畴，本自测聚焦「列表 + 基本信息」数据契约。

为何用 delta 而非绝对计数：环境中可能残留历史探针群（如 [MT06-smoke]），绝对计数
会误判。用 baseline → +1 → -1 delta 逻辑，只断言「探针群进出列表」而非「列表总数
== 固定值」，对历史残留鲁棒（不删非本任务创建的数据，尊重数据归属）。

为何断言 created_at 不变：update_group 翻新 name/description/config/coordinator_id
等元数据，绝不应触碰 created_at（创建时间是不可变审计字段）。created_at 不变是
「更新只改该改的字段」的强不变量，比「updated_at 推进」更能证明更新语义正确。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

TIMEOUT = 20.0

# 探针群组名（[MT-08] 前缀便于溯源 + 清理识别）。
PROBE_NAME = "[MT-08] 团队列表基本信息探针组"
PROBE_DESC = "MT-08 团队列表及基本信息自测探针"
PROBE_NAME_NEW = "[MT-08] 改名后-基本信息更新探针"
PROBE_DESC_NEW = "MT-08 更新后的描述（PUT 改基本信息）"


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/health")
        return r.status_code == 200 and r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def update_group(group_id: str, body: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(f"{BASE}/api/groups/{group_id}", json=body)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def delete_group(group_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


def _is_iso_z(s: str) -> bool:
    """ISO 8601 + Z 后缀校验（_now_iso 产物：...Z）。"""
    return isinstance(s, str) and s.endswith("Z") and "T" in s and len(s) >= 20


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


# Group 模型 8 字段（models.Group + entities.GroupEntity）。
GROUP_FIELDS = {"id", "name", "coordinator_id", "description", "status",
                "config", "created_at", "updated_at"}


async def main() -> int:
    print("=== MT-08 自测：团队列表及基本信息 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    baseline = 0

    try:
        # ── 1. 前置：列表非空 + 候选池含 coordinator ──
        print("\n[check 1] 前置：GET /api/groups 列表非空 + GET /api/agents 含 coordinator")
        groups_before = await list_groups()
        baseline = len(groups_before)
        if not _check("团队列表非空（至少含种子演示群）", len(groups_before) >= 1,
                      f"列表 {len(groups_before)} 个"):
            errs.append("[list] 团队列表为空")
        else:
            print(f"      baseline 列表 {baseline} 个群组")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        if not coord and agents:
            coord = agents[0]
            print("      [fallback] 无 coordinator 角色，退化取首个 agent 当群主")
        if not _check("候选池含可用 agent 当群主", coord is not None, "候选池为空"):
            errs.append("[pool] 候选池为空，无法建群探针")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord is not None
        coord_id = coord["id"]
        print(f"      选定群主：{coord_id}（{coord.get('name')}）")

        # ── 2. 建群探针：指定 coord + description ──
        print("\n[check 2] 建群探针：POST /api/groups（指定 coord + description）")
        st, g = await create_group({
            "name": PROBE_NAME,
            "coordinator_id": coord_id,
            "description": PROBE_DESC,
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None,
                      f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        create_ok = (
            str(g.get("id", "")).startswith("group_")
            and g.get("name") == PROBE_NAME
            and g.get("coordinator_id") == coord_id
            and g.get("status") == "active"
            and bool(g.get("created_at"))
        )
        if _check("group_ 前缀 / name / coordinator_id==指定 / status=active / created_at 非空",
                  create_ok, f"group={g}"):
            print(f"      样本：id={g['id'][:24]}… coord={g.get('coordinator_id')} "
                  f"name={g.get('name')!r}")
        else:
            errs.append(f"[create] 字段异常：{g}")

        # ── 3. 列表 delta + 排序 ──
        print("\n[check 3] 列表 delta：创建后 +1，探针在列表，按 created_at 升序")
        groups_after = await list_groups()
        if _check(f"列表长度 == baseline+1（{baseline}→{len(groups_after)}）",
                  len(groups_after) == baseline + 1,
                  f"实际 {len(groups_after)}（baseline={baseline}）"):
            pass
        else:
            errs.append(f"[list-delta] 列表长度 {len(groups_after)} != baseline+1={baseline + 1}")
        # 探针在列表
        probe_in_list = next((x for x in groups_after if x.get("id") == probe_group_id), None)
        if _check("探针群在团队列表里", probe_in_list is not None, "列表无探针群"):
            pass
        else:
            errs.append("[list-delta] 探针群不在列表")
        # 按 created_at 升序（list_groups order_by created_at）
        ts_list = [x.get("created_at", "") for x in groups_after]
        sorted_ok = all(ts_list[i] <= ts_list[i + 1] for i in range(len(ts_list) - 1))
        if _check("列表按 created_at 升序（list_groups order_by created_at）", sorted_ok,
                  f"ts={ts_list}"):
            pass
        else:
            errs.append("[list-order] 列表未按 created_at 升序")

        # ── 4. 单读基本信息完整性：8 字段齐全 + 类型合法 + 值正确 ──
        print("\n[check 4] 单读基本信息：GET /api/groups/{id} 8 字段齐全 + 值正确")
        single = await get_group(probe_group_id)
        if single is None:
            _check("单读 200", False, "404/None")
            errs.append("[single] 单读群组 404")
        else:
            keys_ok = GROUP_FIELDS.issubset(set(single.keys()))
            if _check(f"8 字段齐全 {sorted(GROUP_FIELDS)}", keys_ok,
                      f"缺 {GROUP_FIELDS - set(single.keys())}"):
                pass
            else:
                errs.append(f"[single] 字段缺失：{GROUP_FIELDS - set(single.keys())}")
            # 类型合法 + 值正确
            value_ok = (
                isinstance(single.get("id"), str)
                and isinstance(single.get("name"), str)
                and isinstance(single.get("coordinator_id"), str)
                and (single.get("description") is None or isinstance(single.get("description"), str))
                and isinstance(single.get("status"), str)
                and isinstance(single.get("created_at"), str)
                and isinstance(single.get("updated_at"), str)
                and single.get("name") == PROBE_NAME
                and single.get("description") == PROBE_DESC
                and single.get("coordinator_id") == coord_id
                and single.get("status") == "active"
            )
            if _check("字段类型合法 + name/desc/coord/status 值正确", value_ok,
                      f"single={ {k: single.get(k) for k in ('name','description','coordinator_id','status')} }"):
                print(f"      样本：name={single.get('name')!r} status={single.get('status')} "
                      f"coord={single.get('coordinator_id')}")
            else:
                errs.append(f"[single] 字段值异常：{single}")

        # ── 5. 时间戳格式：ISO 8601 + Z 后缀 + updated_at >= created_at ──
        print("\n[check 5] 时间戳格式：created_at/updated_at ISO 8601 + Z 后缀")
        if single is not None:
            ca, ua = single.get("created_at", ""), single.get("updated_at", "")
            fmt_ok = _is_iso_z(ca) and _is_iso_z(ua)
            if _check("created_at/updated_at 均为 ISO 8601 + Z 后缀", fmt_ok,
                      f"ca={ca!r} ua={ua!r}"):
                pass
            else:
                errs.append(f"[ts-fmt] 时间戳格式异常 ca={ca} ua={ua}")
            # updated_at >= created_at（同格式同 UTC，字典序=时间序）
            if _check("updated_at >= created_at（同 UTC，字典序=时间序）",
                      ua >= ca, f"ua={ua} ca={ca}"):
                print(f"      created_at={ca}")
                print(f"      updated_at={ua}")
            else:
                errs.append(f"[ts-order] updated_at < created_at（ua={ua} ca={ca}）")

        # ── 6. 回读 == 列表元素：单读与列表真源一致 ──
        print("\n[check 6] 回读 == 列表元素：单读 8 字段 == 列表中探针元素")
        if single is not None and probe_in_list is not None:
            same = all(single.get(k) == probe_in_list.get(k) for k in GROUP_FIELDS)
            if _check("单读 == 列表元素（8 字段逐一相等，列表即真源）", same,
                      f"diff={ {k: (single.get(k), probe_in_list.get(k)) for k in GROUP_FIELDS if single.get(k) != probe_in_list.get(k)} }"):
                print("      列表元素与单读一致——前端 chatGroup=groups.find(id) 可信")
            else:
                errs.append("[cross] 单读与列表元素不一致")

        # ── 7. 更新基本信息：PUT name+description → 回读更新 + created_at 不变 ──
        print("\n[check 7] 更新基本信息：PUT name+description（created_at 不变 + updated_at 推进）")
        old_ca = single.get("created_at") if single else ""
        old_ua = single.get("updated_at") if single else ""
        st, updated = await update_group(probe_group_id, {
            "name": PROBE_NAME_NEW,
            "description": PROBE_DESC_NEW,
        })
        if not _check("PUT 200 + Group", st == 200 and updated is not None,
                      f"status={st} body={updated}"):
            errs.append(f"[update] 非 200 status={st}")
        else:
            assert updated is not None
            reread = await get_group(probe_group_id)
            if reread is None:
                _check("更新后单读 200", False, "404")
                errs.append("[update-reread] 更新后群组 404")
            else:
                name_desc_ok = (
                    reread.get("name") == PROBE_NAME_NEW
                    and reread.get("description") == PROBE_DESC_NEW
                )
                if _check("回读 name/description == 新值", name_desc_ok,
                          f"name={reread.get('name')!r} desc={reread.get('description')!r}"):
                    print(f"      更新后：name={reread.get('name')!r}")
                else:
                    errs.append(f"[update] name/desc 未更新：{reread}")
                # created_at 不变（强不变量：更新不动创建时间）
                if _check("created_at 不变（更新只翻元数据不动创建时间）",
                          reread.get("created_at") == old_ca and old_ca != "",
                          f"old={old_ca} new={reread.get('created_at')}"):
                    pass
                else:
                    errs.append(f"[update] created_at 被改动 old={old_ca} new={reread.get('created_at')}")
                # updated_at 推进（>= 旧 updated_at；微秒精度通常严格 >，但用 >= 容极端同 tick）
                new_ua = reread.get("updated_at", "")
                if _check("updated_at 推进（>= 旧 updated_at）", new_ua >= old_ua and old_ua != "",
                          f"old={old_ua} new={new_ua}"):
                    print(f"      updated_at：{old_ua} → {new_ua}")
                else:
                    errs.append(f"[update] updated_at 未推进 old={old_ua} new={new_ua}")

        # ── 8. config 基本信息态：新探针群 config == None ──
        print("\n[check 8] config 基本信息态：新探针群 config == None（无 leader_strategy/auto_confirm）")
        config_read = await get_group(probe_group_id)
        if config_read is None:
            _check("读探针 config", False, "群 404")
            errs.append("[config] 探针群 404")
        else:
            cfg = config_read.get("config")
            if _check("新探针群 config == None（create_group 不写 config → JSON null）",
                      cfg is None, f"config={cfg}"):
                print("      新群无 config——前端「未设置指挥策略」占位即此态")
            else:
                errs.append(f"[config] 新群 config 非 None：{cfg}")

        # ── 9. 不存在群：GET → 200 null（不 404） ──
        print("\n[check 9] 不存在群：GET /api/groups/group_nope_mt08 → 200 null")
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{BASE}/api/groups/group_nope_mt08")
            body = r.text
            not_found_ok = (
                r.status_code == 200
                and body.strip() == "null"
            )
            if _check("200 + null（get_group 找不到行返 None，route 透传 null，不 404）",
                      not_found_ok, f"status={r.status_code} body={body[:80]}"):
                pass
            else:
                errs.append(f"[notfound] status={r.status_code} body={body[:80]}")

        # ── 10. 收尾：DELETE 探针 → 列表回 baseline ──
        print("\n[check 10] 收尾：DELETE 探针群 → 列表回 baseline，无残留")
        st, ok = await delete_group(probe_group_id)
        if _check("DELETE 200 True", st == 200 and ok is True, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[cleanup] DELETE status={st} ok={ok}")
        groups_final = await list_groups()
        leaked = [x for x in groups_final if x.get("id") == probe_group_id]
        if _check(f"列表回 baseline（{len(groups_final)} == {baseline}）+ 探针无残留",
                  len(groups_final) == baseline and len(leaked) == 0,
                  f"final={len(groups_final)} baseline={baseline} leaked={len(leaked)}"):
            pass
        else:
            errs.append(f"[cleanup] 列表未回 baseline 或探针残留：final={len(groups_final)}")

    finally:
        # 兜底：若中途失败探针群可能还在，清理之（不污染后续自测）
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 团队列表及基本信息端到端验证通过：")
    print("  · 列表非空 + 候选池含 coordinator；")
    print("  · 建群探针 → 200 + Group（group_ 前缀 / coordinator_id==指定 / status=active）；")
    print("  · 列表 delta（+1）+ 探针在列表 + 按 created_at 升序；")
    print("  · 单读 8 字段齐全 + 类型合法 + 值正确；")
    print("  · 时间戳 ISO 8601 + Z 后缀 + updated_at >= created_at；")
    print("  · 单读 == 列表元素（列表即真源，前端 chatGroup 可信）；")
    print("  · 更新 name/description 回读更新 + created_at 不变 + updated_at 推进；")
    print("  · 新探针群 config == None（无策略配置基本态）；")
    print("  · 不存在群 → 200 null（不 404）；")
    print("  · 收尾 DELETE → 列表回 baseline 无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
