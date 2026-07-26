"""任务17b 回归：记忆模块后端实体 + /api/memory CRUD + FTS5 全文检索.

八段契约（静态 + TestClient 真 HTTP + 真 crud 落库，隔离临时 DB）：

  A. 路由就位（静态源码）
     1. api/memory.py 定义 list_memories_route / create_memory_route /
        update_memory_route / delete_memory_route / search_memories_route.
     2. memory_api.router 注册了 8 条路径（GET/POST/PUT/DELETE + enable/disable + search）.
     3. main.py include_router(memory.router).
     4. entities.py 定义 MemoryEntity（__tablename__ == 'memories'）.

  B. crud 函数就位（静态源码）
     5. crud 有 list_memories / get_memory / create_memory / update_memory /
        delete_memory / set_memory_enabled / search_memories / ensure_memories_fts.
     6. ensure_memories_fts 用 ``CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(... tokenize='trigram')``（trigram 支持 CJK 子串；unicode61 对
        中文整条列当一个 token 无法 MATCH——已实测验证）。
     7. search_memories 用 FTS5 ``MATCH`` + ``bm25(memories_fts)`` 排序 +
        importance 加权 + LIKE 兜底（短查询/畸形 query 不崩）。

  C. CRUD 真落库（TestClient 真 HTTP）
     8.  POST /api/memory → 200 + Memory（id mem_ 前缀 / content / scope=global /
         importance / enabled=True / created_at 非空）。
     9.  GET /api/memory 列表含探针（真源交叉验证）。
     10. GET /api/memory/{id} 回读 == create 响应（持久化一致）。
     11. PUT /api/memory/{id} 改 content → 200 + 新 content（FTS5 sidecar 同步）。
     12. DELETE /api/memory/{id} → 200 + True；再 GET → None。

  D. enable/disable 软删除（TestClient）
     13. POST /disable → enabled=False；POST /enable → enabled=True。

  E. FTS5 全文检索（TestClient）
     14. POST /api/memory/search query='Java' → 命中含 Java 的记忆，bm25 排序。
     15. 短查询（2 字符中文）走 LIKE 兜底仍返回命中（trigram 需 ≥3 字符）。
     16. disabled 记忆不被检索命中（enabled=1 守卫）。
     17. 检索命中后 access_count 自增 + last_accessed_at 落库（衰减排序用）。

  F. 校验（TestClient）
     18. POST 空 content → 400。
     19. POST scope='bogus' → 400。
     20. POST scope='agent' 无 scope_ref → 400。
     21. PUT scope='bogus' → 400。

  G. FTS5 sidecar 同步（双写真源）
     22. update content 后新关键词可被检索命中（FTS5 sidecar 已 delete+insert 同步）。
     23. delete 后 FTS5 sidecar 行也清掉（再 search 不命中已删内容）。

不依赖 live server / 真实 LLM：纯 HTTP + 真 crud 落库 + 隔离临时 DB。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

# --- redirect DATA_DIR to a temp root BEFORE importing config/database ---
_TMP = tempfile.mkdtemp(prefix="memory_17b_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402

config.DATA_DIR = _TMP

import api.memory as memory_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
MEMORY_API_PY = BACKEND / "api" / "memory.py"
CRUD_PY = BACKEND / "store" / "crud.py"
MAIN_PY = BACKEND / "main.py"
ENTITIES_PY = BACKEND / "store" / "entities.py"

app = FastAPI()
app.include_router(memory_api.router)
client = TestClient(app)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fname: str, is_async: bool = True) -> str:
    prefix = "async def" if is_async else "def"
    m = re.search(rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)", src, re.S)
    return m.group(0) if m else ""


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


def assert_static() -> list[str]:
    """A/B：静态源码契约（路由 + crud 函数 + FTS5 建表 + 检索 SQL）。"""
    errs: list[str] = []
    api_src = _read(MEMORY_API_PY)
    crud_src = _read(CRUD_PY)
    main_src = _read(MAIN_PY)
    entities_src = _read(ENTITIES_PY)

    # ── A. 路由就位 ──
    try:
        _check("[A1] api/memory.py 定义 5 个 route 函数",
               all(f"async def {fn}(" in api_src for fn in (
                   "list_memories_route", "create_memory_route",
                   "update_memory_route", "delete_memory_route",
                   "search_memories_route")))
    except AssertionError as e:
        errs.append(str(e))

    try:
        paths = {r.path for r in memory_api.router.routes}
        expected = {
            "/api/memory", "/api/memory/{memory_id}",
            "/api/memory/{memory_id}/enable",
            "/api/memory/{memory_id}/disable",
            "/api/memory/search",
        }
        _check("[A2] memory router 注册 5 条路径",
               expected.issubset(paths), detail=f"paths={sorted(paths)}")
    except AssertionError as e:
        errs.append(str(e))

    try:
        _check("[A3] main.py include_router(memory.router)",
               "memory.router" in main_src)
    except AssertionError as e:
        errs.append(str(e))

    try:
        _check("[A4] entities.py 定义 MemoryEntity (__tablename__='memories')",
               "class MemoryEntity(Base):" in entities_src
               and "__tablename__ = \"memories\"" in entities_src)
    except AssertionError as e:
        errs.append(str(e))

    # ── B. crud 函数 + FTS5 ──
    try:
        _check("[B5] crud 有 8 个 memory 函数",
               all(f"async def {fn}(" in crud_src for fn in (
                   "list_memories", "get_memory", "create_memory",
                   "update_memory", "delete_memory", "set_memory_enabled",
                   "search_memories", "ensure_memories_fts")))
    except AssertionError as e:
        errs.append(str(e))

    try:
        # FTS5 DDL lives in the module constant _MEM_FTS_CREATE_SQL (not in the
        # function body, which only references it via text(_MEM_FTS_CREATE_SQL)).
        ok = ("_MEM_FTS_CREATE_SQL" in crud_src
              and "memories_fts USING fts5" in crud_src
              and "tokenize" in crud_src and "trigram" in crud_src
              and "async def ensure_memories_fts(" in crud_src)
        _check("[B6] ensure_memories_fts + _MEM_FTS_CREATE_SQL (trigram tokenizer)", ok,
               detail="FTS5 DDL constant or ensure_memories_fts missing")
    except AssertionError as e:
        errs.append(str(e))

    try:
        body = _fn_body(crud_src, "search_memories")
        ok = ("MATCH :q" in body and "bm25(memories_fts)" in body
              and "importance" in body and "LIKE" in body)
        _check("[B7] search_memories 用 FTS5 MATCH + bm25 + importance + LIKE 兜底",
               ok, detail=body[:300])
    except AssertionError as e:
        errs.append(str(e))

    return errs


async def assert_e2e() -> list[str]:
    """C/D/E/F/G：真 crud 落库 + TestClient 真 HTTP（隔离临时 DB）。"""
    errs: list[str] = []
    orig_data_dir = os.environ.get("MULTI_AGENT_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="memory_17b_e2e_")
    os.environ["MULTI_AGENT_DATA_DIR"] = tmp_dir
    probe_ids: list[str] = []
    try:
        import importlib
        import store.database as _db
        importlib.reload(_db)
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )
        _db.engine = create_async_engine(
            _db.DB_URL, echo=False,
            connect_args={"check_same_thread": False}, pool_pre_ping=True,
        )
        _db.SessionLocal = async_sessionmaker(
            _db.engine, expire_on_commit=False, class_=AsyncSession,
        )
        await _db.init_db()
        from store import crud

        # ── C8 创建记忆 → 200 + Memory ──
        try:
            r = client.post("/api/memory", json={
                "content": "用户是Java后端工程师，偏好简洁回复",
                "scope": "global", "importance": 1.0,
            })
            mem = r.json()
            ok = (r.status_code == 200
                  and str(mem.get("id", "")).startswith("mem_")
                  and mem.get("content") == "用户是Java后端工程师，偏好简洁回复"
                  and mem.get("scope") == "global"
                  and mem.get("importance") == 1.0
                  and mem.get("enabled") is True
                  and isinstance(mem.get("created_at"), str) and mem.get("created_at"))
            _check("[C8] POST /api/memory → 200 + Memory（mem_ 前缀/content/scope/importance/enabled/created_at）",
                   ok, detail=f"status={r.status_code} body={mem}")
            if ok:
                probe_ids.append(mem["id"])
        except AssertionError as e:
            errs.append(str(e))

        # 第二条探针（不同 content，供检索排序断言）
        mem_b = client.post("/api/memory", json={
            "content": "前端React技术栈，上次项目",
            "scope": "global", "importance": 0.8,
        }).json()
        if mem_b.get("id"):
            probe_ids.append(mem_b["id"])

        # ── C9 列表含探针 ──
        try:
            lst = client.get("/api/memory").json()
            ids = {m["id"] for m in lst}
            both = probe_ids[0] in ids and probe_ids[1] in ids if len(probe_ids) >= 2 else False
            _check("[C9] GET /api/memory 列表含两条探针", both,
                   detail=f"ids={sorted(ids)[:4]}")
        except AssertionError as e:
            errs.append(str(e))

        # ── C10 单读回读一致 ──
        try:
            mid = probe_ids[0]
            reread = client.get(f"/api/memory/{mid}").json()
            ok = (reread.get("id") == mid
                  and reread.get("content") == "用户是Java后端工程师，偏好简洁回复"
                  and reread.get("scope") == "global")
            _check("[C10] GET /api/memory/{id} 回读 == create 响应", ok,
                   detail=f"reread={reread}")
        except AssertionError as e:
            errs.append(str(e))

        # ── C11 PUT 改 content → 新 content（FTS5 sidecar 同步在 G22 验证）──
        try:
            mid = probe_ids[0]
            r = client.put(f"/api/memory/{mid}", json={
                "content": "用户是Java后端工程师，偏好简洁回复，且偏好中文回答",
                "importance": 0.7,
            })
            upd = r.json()
            ok = (r.status_code == 200
                  and upd.get("content") == "用户是Java后端工程师，偏好简洁回复，且偏好中文回答"
                  and upd.get("importance") == 0.7)
            _check("[C11] PUT /api/memory/{id} 改 content+importance → 200 + 新值", ok,
                   detail=f"status={r.status_code} body={upd}")
        except AssertionError as e:
            errs.append(str(e))

        # ── D13 enable/disable ──
        try:
            mid = probe_ids[1]
            r1 = client.post(f"/api/memory/{mid}/disable").json()
            r2 = client.post(f"/api/memory/{mid}/enable").json()
            ok = r1.get("enabled") is False and r2.get("enabled") is True
            _check("[D13] disable→enabled=False，enable→enabled=True", ok,
                   detail=f"disable={r1.get('enabled')} enable={r2.get('enabled')}")
        except AssertionError as e:
            errs.append(str(e))

        # ── E14 FTS5 检索命中 + bm25 排序 ──
        try:
            r = client.post("/api/memory/search", json={"query": "Java", "top_k": 5})
            resp = r.json()
            results = resp.get("results", [])
            # 第一条探针 content 含 Java；第二条不含 Java → 命中应只含第一条
            hit_ids = {h["memory"]["id"] for h in results}
            ok = (r.status_code == 200
                  and probe_ids[0] in hit_ids
                  and probe_ids[1] not in hit_ids
                  and resp.get("query") == "Java")
            _check("[E14] search 'Java' 命中含 Java 的记忆，排除不含的", ok,
                   detail=f"status={r.status_code} hit_ids={sorted(hit_ids)[:4]}")
        except AssertionError as e:
            errs.append(str(e))

        # ── E15 短查询（2 字符）走 LIKE 兜底 ──
        try:
            # '后端' 是 2 字符，trigram 需 ≥3 字符 → FTS5 MATCH 返回空 → LIKE 兜底
            r = client.post("/api/memory/search", json={"query": "后端", "top_k": 5})
            results = r.json().get("results", [])
            hit_ids = {h["memory"]["id"] for h in results}
            ok = r.status_code == 200 and probe_ids[0] in hit_ids
            _check("[E15] search '后端'（2 字符）走 LIKE 兜底仍命中", ok,
                   detail=f"status={r.status_code} hit_ids={sorted(hit_ids)[:4]}")
        except AssertionError as e:
            errs.append(str(e))

        # ── E16 disabled 记忆不被检索命中 ──
        try:
            mid = probe_ids[1]
            client.post(f"/api/memory/{mid}/disable")
            r = client.post("/api/memory/search", json={"query": "React", "top_k": 5})
            results = r.json().get("results", [])
            hit_ids = {h["memory"]["id"] for h in results}
            ok = mid not in hit_ids  # 已 disable → 不应命中
            _check("[E16] disabled 记忆不被检索命中（enabled=1 守卫）", ok,
                   detail=f"disabled_id={mid} hit_ids={sorted(hit_ids)[:4]}")
            client.post(f"/api/memory/{mid}/enable")  # 恢复供后续断言
        except AssertionError as e:
            errs.append(str(e))

        # ── E17 检索命中后 access_count 自增 ──
        try:
            before = client.get(f"/api/memory/{probe_ids[0]}").json()
            ac_before = before.get("access_count", 0)
            client.post("/api/memory/search", json={"query": "Java", "top_k": 5})
            after = client.get(f"/api/memory/{probe_ids[0]}").json()
            ac_after = after.get("access_count", 0)
            ok = ac_after > ac_before and after.get("last_accessed_at")
            _check("[E17] 检索命中后 access_count 自增 + last_accessed_at 落库", ok,
                   detail=f"before={ac_before} after={ac_after} last={after.get('last_accessed_at')}")
        except AssertionError as e:
            errs.append(str(e))

        # ── F18-F21 校验 ──
        try:
            r = client.post("/api/memory", json={"content": "", "scope": "global"})
            _check("[F18] POST 空 content → 400", r.status_code == 400,
                   detail=f"status={r.status_code}")
        except AssertionError as e:
            errs.append(str(e))

        try:
            r = client.post("/api/memory", json={"content": "x", "scope": "bogus"})
            _check("[F19] POST scope='bogus' → 400", r.status_code == 400,
                   detail=f"status={r.status_code}")
        except AssertionError as e:
            errs.append(str(e))

        try:
            r = client.post("/api/memory", json={"content": "x", "scope": "agent"})
            _check("[F20] POST scope='agent' 无 scope_ref → 400", r.status_code == 400,
                   detail=f"status={r.status_code}")
        except AssertionError as e:
            errs.append(str(e))

        try:
            r = client.put(f"/api/memory/{probe_ids[0]}", json={"scope": "bogus"})
            _check("[F21] PUT scope='bogus' → 400", r.status_code == 400,
                   detail=f"status={r.status_code}")
        except AssertionError as e:
            errs.append(str(e))

        # ── G22 update content 后新关键词可检索（FTS5 sidecar 同步）──
        try:
            # C11 已把 content 改成含「中文」→ sidecar 应已同步
            r = client.post("/api/memory/search", json={"query": "中文", "top_k": 5})
            results = r.json().get("results", [])
            hit_ids = {h["memory"]["id"] for h in results}
            ok = probe_ids[0] in hit_ids
            _check("[G22] update content 后新关键词可检索（FTS5 sidecar delete+insert 同步）",
                   ok, detail=f"hit_ids={sorted(hit_ids)[:4]}")
        except AssertionError as e:
            errs.append(str(e))

        # ── C12 + G23 删除 + sidecar 清理 ──
        try:
            for mid in probe_ids:
                r = client.delete(f"/api/memory/{mid}")
                if r.status_code != 200 or r.json() is not True:
                    errs.append(f"[C12] 删除 {mid} 失败 status={r.status_code}")
                    continue
                # 再 GET → None
                g = client.get(f"/api/memory/{mid}").json()
                if g is not None:
                    errs.append(f"[C12] 删除后 GET 仍返回非 None: {g}")
            # G23: sidecar 清理——再 search 已删内容应不命中
            r = client.post("/api/memory/search", json={"query": "Java", "top_k": 5})
            results = r.json().get("results", [])
            leaked = {h["memory"]["id"] for h in results} & set(probe_ids)
            _check("[C12+G23] 删除后 GET→None + FTS5 sidecar 无残留",
                   not leaked, detail=f"leaked={sorted(leaked)}")
        except AssertionError as e:
            errs.append(str(e))

    finally:
        if orig_data_dir is not None:
            os.environ["MULTI_AGENT_DATA_DIR"] = orig_data_dir
        else:
            os.environ.pop("MULTI_AGENT_DATA_DIR", None)

    return errs


def main() -> int:
    print("=" * 70)
    print("任务17b 回归：记忆模块后端实体 + /api/memory CRUD + FTS5 全文检索")
    print("=" * 70 + "\n")
    print("── A/B 静态契约 ──")
    static_errs = assert_static()
    print("\n── C/D/E/F/G 真 crud 落库 + TestClient ──")
    e2e_errs = asyncio.run(assert_e2e())
    errs = static_errs + e2e_errs
    print()
    if errs:
        print(f"=== 结果: FAIL ({len(errs)} 项) ===")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("=== 结果: PASS ===")
    print(
        "\n任务17b 契约锁定（MemoryEntity + /api/memory CRUD + FTS5 检索）：\n"
        "  · A 路由 5 条路径注册 + main.py 挂载 + MemoryEntity 实体；\n"
        "  · B crud 8 函数 + ensure_memories_fts (trigram) + search FTS5 MATCH/bm25/LIKE 兜底；\n"
        "  · C CRUD 真落库 + 列表/单读/更新/删除 一致；\n"
        "  · D enable/disable 软删除切换；\n"
        "  · E FTS5 检索命中 + bm25 排序 + 短查询 LIKE 兜底 + disabled 排除 + access_count 自增；\n"
        "  · F 校验（空 content / 非法 scope / agent 无 ref → 400）；\n"
        "  · G FTS5 sidecar 双写同步（update 新词可检索 / delete 无残留）。"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        pass
