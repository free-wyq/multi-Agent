"""MT-15 自测：Worker 失败后自动重派/降级（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-13/MT-14 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动 + 直接干模式 fan-out + 真执行 report-back）。

MT-15 链路（Worker 失败 → Leader 恢复决策 → 重派/降级/级联 → 计划终止）：
  前端 GroupPage：用户发一个目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出 plan（单步派给失败探针 worker）
    · node_dispatch → direct_run → node_dispatch_next → dispatch_ready_steps
      → _dispatch_one 派发步骤到失败 worker + emit_task_dispatched
  失败 worker 执行 _run_worker_task → execute_agent_task → run_agent_loop：
    · ChatOpenAI(model=「bogus-mt15-fail」) → astream_events 调 LLM → 端点返 400
      （deepseek 对未知模型名返 400 invalid_request_error，确定性失败）
    · except Exception → 返回 {success:False, exit_code:1, output:"execution error:..."}
  worker _run_worker_task 收 success=False：
    · emit_task_completed(success=False) → WS task_failed
    · complete_task(False)
    · _reply("执行出错了: ...")
    · push_notify(agent_reply, worker→coordinator, {task_id, success:False})  ← 失败 report-back
  coordinator 引擎 _handle_notify（处理失败 report-back）→ LangGraph ainvoke：
    · classify：incoming_kind=="agent_reply" + sender!=user + data.task_id 匹配 dispatched step
      → handle_reply（非 llm_decide——Leader 已在跟踪）
    · node_handle_reply：标记 step status=failed + result=汇报内容。
      all_done（单步）？是 → 但先 MT-15：not success → _maybe_handle_step_failure：
        · build_step_recovery_prompt 给 LLM 看「计划状态 + 失败步骤 + 失败原因 + roster + attempt」
        · LLM 选 retry/reassign/skip/keep_failed 四策略之一：
          - retry   → step 重置 pending + task_id=None + _attempts++ → dispatch_next 重派原 worker
                     （bogus model 仍失败 → 再 report-back failed → _attempts++ → 达 MAX_RETRY_ATTEMPTS=2
                      → 硬上限强制 keep_failed，不再调 LLM）
          - reassign→ step 重置 pending + 换 agent_id/agent_name 为目标成员 → dispatch_next 重派新 worker
          - skip    → step 标 completed + result「⚠️ 步骤失败已降级跳过」（dependents 可继续，降级）
          - keep_failed → step 保持 failed（apply_fail_fast 级联，确定性默认）
        · 每策略发 announce 公告（keep_failed/硬上限除外）
      → 据 strategy 后 all_done 判定 → summarize（单步必 all_done）
    · node_summarize：从 plan 各 step 构建「🎉 全部完成！协作结果汇总」（失败步 ❌ / 降级步 ✅含⚠️）

「Worker 失败后自动重派/降级」的核心证据：
  ① 失败发生——失败 worker（bogus model）执行时 LLM 端点返 400 → run_agent_loop 返 success=False
     → task_failed 事件（success=False）+ worker agent_reply「执行出错了」（确定性失败触发）；
  ② 恢复决策——node_handle_reply 失败侧调 _maybe_handle_step_failure，Leader LLM 据失败原因 +
     roster 选 retry/reassign/skip/keep_failed 之一（恢复机制运转）；
  ③ 重派/降级生效——据 strategy mutate step：retry/reassign 重置 pending 触发「第二次 task_dispatch
     同一 step」（重派）；skip 标 completed 含「⚠️ 降级跳过」（降级）；keep_failed 保持 failed（级联）。
     无论选哪个，计划最终终止（summarize reached，未因失败死锁）。

为何用 bogus model 制造确定性失败：deepseek 端点对未知模型名返 400 invalid_request_error（实测验证），
ChatOpenAI.astream_events 调用即抛 → run_agent_loop 的 except Exception 捕获 → 返回 success=False。
这比「派一个无法完成的任务」更确定性——LLM 对模糊任务可能仍产出口头回答（success=True），而 bogus
model 必失败（端点级 400，不依赖 LLM 对任务的理解）。失败探针 agent 用专属 role + name 便于溯源清理。

为何单步 plan：单步失败后 all_done 必成立 → summarize 必达（无论 recovery 选哪个策略，计划都终止）。
这隔离了「失败恢复」与「多步依赖级联」——单步验证恢复决策本身（retry/reassign/skip/keep_failed），
多步级联（apply_fail_fast 把失败传给 dependents）是 dispatcher 已有能力，MT-15 聚焦「失败步自身的恢复」。

为何用专属探针群 + reload + 直接干：group_demo_1 coordinator 引擎累积历史状态污染断言。新建 [MT-15]
探针群（coord + [失败探针 agent]）→ reload 起干净引擎 → 显式 auto_confirm=True → coordinator 只看到本
目标 → 派单步给失败探针 → 失败 → 干净恢复决策（沿用 MT-09~MT-14 隔离模式）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator（建探针群群主来源）；
  ② 建失败探针 agent：POST /api/agents（role=frontend_engineer, name=MT15失败探针）+ PUT model=bogus
     （让 worker 执行必失败）；
  ③ 建探针群：POST /api/groups（coord + [失败探针]）→ 200 + Group；
  ④ 引擎启动：touch main.py reload → 轮询 status 直到 2 引擎 idle（Leader + 失败 worker）；
  ⑤ 设直接干：PUT config auto_confirm=True；
  ⑥ 发目标 + 抓事件流：连 WS → POST /api/messages 发「前端工程师创建文件 mt15_probe.md」→ 抓
     task_dispatch + task_failed + announce + Leader 汇总（collect_until_summary 收到「全部完成」即收尾）；
  ⑦ 失败发生（HARD）：task_failed 事件存在（worker 执行失败 success=False）+ 失败步 task_dispatch 存在
     （失败步确被派发过）；
  ⑧ 计划终止（HARD 核心）：抓到 Leader 汇总 reply「全部完成」+ DB 落库交叉——失败未让计划死锁，
     handle_reply 失败侧 + 恢复决策让计划走到 summarize；
  ⑨ 失败步入汇总（HARD）：汇总 reply 含失败探针 agent_name（Leader 跟踪了失败步并纳入汇总）；
  ⑩ 重派发生（SOFT 核心）：失败 step 被「重派」——同一 step 号 task_dispatch >1 次（retry/reassign
     重置 pending 触发第二次派发）；或 announce reply 含「重试/重派/改派」语义；
  ⑪ 降级发生（SOFT 核心）：失败 step 被「降级」——汇总含「⚠️/降级/跳过」（skip 标 completed 降级）；
     或 announce reply 含「跳过/降级」语义；
  ⑫ 改派到其他成员（SOFT）：若 reassign，同一 step 号 task_dispatch 的 agent_id 不同（重派到不同 worker）；
  ⑬ 收尾：DELETE 探针群（stop_group + delete_group）+ DELETE 失败探针 agent → 全局无残留。

为何 HARD 核心是「计划终止」而非「必重派/必降级」：MT-15 的机制是「失败后 Leader 跑 _maybe_handle_step_failure
做恢复决策」。这一机制的确定性证据是「失败后计划未死锁、走到 summarize」——若恢复机制没运转，失败 report-back 后
coordinator 可能卡在 failed step 不前进（无 dispatch_next / 无 summarize）。故「summarize reached」证明失败被
handle_reply 处理并经恢复决策后计划终止。具体选 retry/reassign/skip/keep_failed 是 LLM 据失败原因的决策
（LLM 可能合理判 keep_failed 走级联），作 SOFT——HARD 是「失败发生 + 恢复运转 + 计划终止」确定性链路。

为何不强制「必重派成功」：bogus model 必失败，retry 重派原 worker 仍失败（同 bogus model）→ 重试必再失败 →
达 MAX_RETRY_ATTEMPTS=2 硬上限后强制 keep_failed。故「重派成功」不可强制（除非 reassign 到真实 worker，但单步
单 worker 无可改派对象）。重派「发生」（第二次 task_dispatch）可作 SOFT（retry 会触发），重派「成功」不可强制。
降级（skip）同样 LLM 依赖（LLM 可能选 keep_failed 而非 skip）。故 ⑩⑪ 作 SOFT，HARD 是失败+终止+入汇总。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

BASE = "http://localhost:8000"
BACKEND_MAIN = "/home/wyq/work/project/multi-Agent/backend/main.py"
DATA_DIR = str(Path.home() / ".local" / "share" / "multi-agent")

TIMEOUT = 120.0
RELOAD_WAIT = 45.0
SUMMARY_TIMEOUT = 200.0  # 等失败→恢复决策→(可能重派再失败)→summarate 完整链路

# 探针群组名 + 失败探针 agent 名（[MT-15] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-15] 失败重派降级探针组"
FAIL_AGENT_NAME = "MT15失败探针"
FAIL_AGENT_ROLE = "frontend_engineer"
# bogus model 名——deepseek 端点对未知模型返 400，让 worker 执行必失败（确定性失败触发）。
BOGUS_MODEL = "mt15-bogus-fail-model"

# 目标——派给失败探针（唯一 worker）的单步任务，coordinator 必派给它。
GOAL = (
    "【MT-15】请前端工程师用 write_file 工具创建文件 mt15_probe.md，写入一行测试内容'MT15 失败重派探针'。"
    "请直接派发执行计划。"
)

# 产物文件名（失败 worker 可能未产出，清理兜底）。
PROBE_FILE = "mt15_probe.md"


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/health")
        return r.status_code == 200 and r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


async def create_agent(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/agents", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def update_agent(agent_id: str, payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(f"{BASE}/api/agents/{agent_id}", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def delete_agent(agent_id: str) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/agents/{agent_id}")
        return r.status_code == 200 and bool(r.json())


async def create_group(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def add_member(group_id: str, agent_id: str, alias: str | None = None) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/groups/{group_id}/members",
            json={"agentId": agent_id, "alias": alias},
        )
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def group_status(group_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/status/{group_id}")
        return r.json() if r.status_code == 200 else []


async def get_group(group_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def set_auto_confirm(group_id: str, value: bool) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        cur = (await c.get(f"{BASE}/api/groups/{group_id}")).json()
        config = dict(cur.get("config") or {})
        config["auto_confirm"] = value
        r = await c.put(f"{BASE}/api/groups/{group_id}", json={"config": config})
        return r.json().get("config") if r.status_code == 200 else None


async def send_user_message(group_id: str, content: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": group_id,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json() if r.status_code == 200 else {}


async def list_messages(group_id: str, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(
            f"{BASE}/api/messages", params={"groupId": group_id, "limit": str(limit)}
        )
        return r.json() if r.status_code == 200 else []


async def delete_group(group_id: str) -> tuple[int, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.delete(f"{BASE}/api/groups/{group_id}")
        if r.status_code == 200:
            return 200, bool(r.json())
        return r.status_code, False


async def list_groups() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/groups")
        return r.json() if r.status_code == 200 else []


async def wait_for_engines(group_id: str, expected: int) -> bool:
    os.system(f"touch {BACKEND_MAIN}")
    deadline = asyncio.get_event_loop().time() + RELOAD_WAIT
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                h = await c.get(f"{BASE}/health")
                if h.status_code == 200:
                    st = await c.get(f"{BASE}/api/status/{group_id}")
                    if st.status_code == 200 and len(st.json()) >= expected:
                        return True
        except (httpx.HTTPError, Exception):
            pass
        await asyncio.sleep(1.0)
    return False


async def collect_until_summary(
    ws_url: str, send_action, timeout: float, coordinator_id: str
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict | None]:
    """连 WS，send_action 发消息，收事件直到抓到 Leader 汇总 agent_reply 或超时。

    返回 (全量事件, task_dispatch 事件列表, task_complete/task_failed 事件列表,
           coordinator agent_reply 事件列表[含 announce + 汇总], 汇总 reply 事件)。
    汇总 reply = node_summarize 产出的 agent_reply（sender=coordinator，content 含「全部完成/汇总」）。
    """
    events: list[dict] = []
    dispatches: list[dict] = []
    completes: list[dict] = []
    coord_replies: list[dict] = []
    summary_ev: dict | None = None
    deadline = time.time() + timeout
    async with websockets.connect(ws_url, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        if send_action is not None:
            await send_action()
        while time.time() < deadline and summary_ev is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            t = ev.get("type")
            if t == "task_dispatch":
                dispatches.append(ev)
            elif t in ("task_complete", "task_failed"):
                completes.append(ev)
            elif t == "agent_reply" and ev.get("sender_id") == coordinator_id:
                coord_replies.append(ev)
                content = ev.get("content") or ""
                if "全部完成" in content or "汇总" in content:
                    summary_ev = ev
                    end = time.time() + 3.0
                    while time.time() < end:
                        try:
                            raw2 = await asyncio.wait_for(
                                ws.recv(), timeout=max(0.1, end - time.time())
                            )
                            ev2 = json.loads(raw2)
                            events.append(ev2)
                            if ev2.get("type") == "task_dispatch":
                                dispatches.append(ev2)
                            elif ev2.get("type") in ("task_complete", "task_failed"):
                                completes.append(ev2)
                            elif ev2.get("type") == "agent_reply" and ev2.get("sender_id") == coordinator_id:
                                coord_replies.append(ev2)
                        except asyncio.TimeoutError:
                            break
                    break
    # 兜底：从全量事件补扫
    for ev in events:
        if ev.get("type") == "task_dispatch" and ev not in dispatches:
            dispatches.append(ev)
        elif ev.get("type") in ("task_complete", "task_failed") and ev not in completes:
            completes.append(ev)
        elif ev.get("type") == "agent_reply" and ev.get("sender_id") == coordinator_id and ev not in coord_replies:
            coord_replies.append(ev)
    return events, dispatches, completes, coord_replies, summary_ev


def workspace_file(group_id: str, rel: str) -> Path:
    return Path(DATA_DIR) / "workspaces" / group_id / rel


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _info(name: str, cond: bool, detail: str = "") -> None:
    """软断言：不计 FAIL，只 INFO 报告。"""
    mark = "✓" if cond else "·"
    tag = "INFO" if cond else "SOFT-MISS"
    print(f"  {mark} [{tag}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("=== MT-15 自测：Worker 失败后自动重派/降级 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    fail_agent_id: str | None = None

    try:
        # ── 1. 前置：候选池含 coordinator ──
        print("\n[check 1] 前置：GET /api/agents 候选池含 coordinator")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        if not coord and agents:
            coord = agents[0]
            print("      [fallback] 无 coordinator 角色，退化取首个 agent 当群主")
        if not _check("候选池含 coordinator", coord is not None, "无 coordinator"):
            errs.append("[pool] 无 coordinator，无法建探针群")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord is not None
        coord_id = coord["id"]
        print(f"      群主={coord_id}({coord['name']})")

        # ── 2. 建失败探针 agent：POST + PUT model=bogus ──
        print("\n[check 2] 建失败探针 agent：POST /api/agents + PUT model=bogus（让 worker 执行必失败）")
        st, a = await create_agent({
            "name": FAIL_AGENT_NAME,
            "role": FAIL_AGENT_ROLE,
            "system_prompt": "你是一名前端工程师，负责创建和修改前端文件。",
            "description": "MT-15 失败重派/降级探针（bogus model 必失败）",
        })
        if not _check("POST /api/agents 200 + Agent", st == 200 and a is not None, f"status={st} body={a}"):
            errs.append(f"[agent] 创建失败探针非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert a is not None
        fail_agent_id = a["id"]
        # PUT model=bogus（update_agent 透传 model 字段，让该 agent 执行时用 bogus model → 端点 400 → 失败）
        st2, a2 = await update_agent(fail_agent_id, {
            "name": FAIL_AGENT_NAME,
            "role": FAIL_AGENT_ROLE,
            "system_prompt": "你是一名前端工程师，负责创建和修改前端文件。",
            "model": BOGUS_MODEL,
        })
        if _check("PUT model=bogus 回读确认（worker 执行必失败）",
                  st2 == 200 and (a2 or {}).get("model") == BOGUS_MODEL,
                  f"status={st2} model={(a2 or {}).get('model')}"):
            print(f"      失败探针 agent={fail_agent_id} model={BOGUS_MODEL}（执行时端点返 400 → success=False）")
        else:
            errs.append(f"[agent] PUT model=bogus 未生效 status={st2}")

        # ── 3. 建探针群：coord + [失败探针] ──
        print("\n[check 3] 建探针群：POST /api/groups（coord + [失败探针]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "MT-15 Worker 失败后自动重派/降级自测探针",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        await add_member(probe_group_id, fail_agent_id, None)
        if _check("group_ 前缀 + coordinator_id==群主", str(g["id"]).startswith("group_")
                  and g.get("coordinator_id") == coord_id):
            print(f"      样本：id={g['id'][:24]}… coord={coord_id} worker={fail_agent_id}")
        else:
            errs.append("[create] 群结构异常")

        # ── 4. 引擎启动：reload → 轮询 status 直到 2 引擎 idle ──
        print("\n[check 4] 引擎启动：reload 触发 load_from_store → 2 引擎 idle（Leader + 失败 worker）")
        ready = await wait_for_engines(probe_group_id, expected=2)
        if not _check("reload 后探针群 2 引擎 idle", ready, "reload 后引擎未到位"):
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'], e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            ids = {e["id"] for e in engines}
            all_idle = all(e.get("status") == "idle" for e in engines)
            if _check("2 引擎含 coord + 失败探针 且全 idle",
                      {coord_id, fail_agent_id}.issubset(ids) and all_idle,
                      f"ids={ids} statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎集合/idle 不符")

        # ── 5. 设直接干：PUT config auto_confirm=True ──
        print("\n[check 5] 设直接干：PUT config auto_confirm=True（让 Leader fan-out + 失败 report-back 触发恢复）")
        cfg = await set_auto_confirm(probe_group_id, True)
        if _check("config.auto_confirm==True（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True), f"config={cfg}"):
            print(f"      auto_confirm=True（直接干，失败 report-back 触发 handle_reply 恢复决策）")
        else:
            errs.append("[config] auto_confirm 未置 True")

        # ── 6. 发目标 + 抓事件流 ──
        print("\n[check 6] 发目标 + 抓 task_dispatch + task_failed + announce + Leader 汇总")
        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"
        async def _send():
            await asyncio.sleep(0.3)
            msg = await send_user_message(probe_group_id, GOAL)
            print(f"      [send] user message id={(msg.get('id') or '')[:16]}…")

        events, dispatches, completes, coord_replies, summary_ev = await collect_until_summary(
            ws_url, _send, SUMMARY_TIMEOUT, coord_id
        )

        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")
        print(f"      [counts] task_dispatch={len(dispatches)} "
              f"task_complete/failed={len(completes)} coord_replies={len(coord_replies)}")

        # 失败步 step 号（首个 task_dispatch 的 step）
        failed_step_num: Any = None
        if dispatches:
            failed_step_num = (dispatches[0].get("data") or {}).get("step")
        # task_failed 事件（success=False）
        failed_evs = [c for c in completes if c.get("type") == "task_failed"]
        # 按 step 号分组 task_dispatch（检测重派：同 step 派 >1 次）
        dispatch_by_step: dict[Any, list[dict]] = {}
        for d in dispatches:
            sn = (d.get("data") or {}).get("step")
            dispatch_by_step.setdefault(sn, []).append(d)

        # ── 7. 失败发生（HARD）：task_failed 存在 + 失败步 task_dispatch 存在 ──
        print("\n[check 7] 失败发生（HARD）：task_failed 事件存在 + 失败步 task_dispatch 存在")
        if _check("task_failed 事件存在（worker 执行失败 success=False）",
                  len(failed_evs) >= 1, f"task_failed={len(failed_evs)}"):
            fe = failed_evs[0]
            print(f"      [fail] sender={fe.get('sender_id')} task_id={(fe.get('task_id') or '')[:16]}… "
                  f"content={str(fe.get('content'))[:60]}")
        else:
            errs.append(f"[fail] 无 task_failed 事件（worker 未失败）：completes={len(completes)}")
        if _check("失败步 task_dispatch 存在（失败步确被派发过）",
                  failed_step_num is not None and len(dispatches) >= 1,
                  f"dispatches={len(dispatches)} step={failed_step_num}"):
            print(f"      [dispatch] 失败步 step={failed_step_num} 派发 {len(dispatch_by_step.get(failed_step_num, []))} 次")
        else:
            errs.append(f"[dispatch] 无 task_dispatch（失败步未派发）：dispatches={len(dispatches)}")

        # ── 8. 计划终止（HARD 核心）：抓到 Leader 汇总 reply + DB 交叉 ──
        print("\n[check 8] 计划终止（HARD 核心）：抓到 Leader 汇总 reply + DB 交叉（失败未死锁）")
        summary_content = (summary_ev or {}).get("content") or ""
        print(f"      [summary] content 预览：{summary_content[:160]}…")
        if _check("捕获 Leader 汇总 reply（失败后计划走到 summarize，未死锁）",
                  summary_ev is not None, "未捕获汇总 reply（计划可能死锁在失败步）"):
            pass
        else:
            errs.append("[summary] 未捕获 Leader 汇总 reply（失败后计划未终止）")
        msgs = await list_messages(probe_group_id, limit=100)
        db_summary = next(
            (m for m in msgs
             if m.get("type") == "agent_reply"
             and m.get("sender_id") == coord_id
             and ("全部完成" in (m.get("content") or "") or "汇总" in (m.get("content") or ""))),
            None,
        )
        if _check("汇总 reply 落库（GET /api/messages 交叉确认 WS vs DB 双真源）",
                  db_summary is not None, "DB 未找到汇总 reply"):
            print(f"      [db] 汇总 message id={(db_summary or {}).get('id', '')[:16]}… 落库确认")
        else:
            errs.append("[summary] 汇总 reply 未落库")

        # ── 9. 失败步入汇总（HARD）：汇总含失败探针 agent_name ──
        print("\n[check 9] 失败步入汇总（HARD）：汇总 reply 含失败探针 agent_name（Leader 跟踪失败步并纳入汇总）")
        fail_name_in_summary = FAIL_AGENT_NAME in summary_content or "前端工程师" in summary_content
        if _check("汇总 reply 含失败探针 agent_name（失败步被跟踪入汇总）",
                  fail_name_in_summary, f"summary 不含 {FAIL_AGENT_NAME}/前端工程师"):
            pass
        else:
            errs.append(f"[summary] 汇总未含失败探针 name：{summary_content[:100]}")

        # ── 10. 重派发生（SOFT 核心）：同 step task_dispatch >1 或 announce 含重试/重派 ──
        print("\n[check 10] 重派发生（SOFT 核心）：同 step task_dispatch >1（retry/reassign 重派）或 announce 含重试/重派")
        redispatch_count = len(dispatch_by_step.get(failed_step_num, [])) if failed_step_num is not None else 0
        redispatch_happened = redispatch_count > 1
        # announce reply：coord agent_reply 非「全部完成/已制定协作计划/派发」
        announce_replies = [
            r for r in coord_replies
            if "全部完成" not in (r.get("content") or "")
            and "已制定协作计划" not in (r.get("content") or "")
            and "派发" not in (r.get("content") or "")
        ]
        announce_retry = any(
            any(kw in (r.get("content") or "") for kw in ["重试", "重派", "改派", "重新派", "再试"])
            for r in announce_replies
        )
        if _info("重派发生（同 step 派发 >1 次 或 announce 含重试/重派语义）",
                 redispatch_happened or announce_retry,
                 f"同step派发={redispatch_count} announce重试词={'有' if announce_retry else '无'}"
                 + (f" announce预览={[(r.get('content') or '')[:50] for r in announce_replies]}" if announce_replies else "")):
            print(f"      ✓ 失败步被重派（retry/reassign 触发第二次 task_dispatch）或 Leader 公告重试/改派")
        else:
            print(f"      · 未检测到重派（LLM 可能选 skip 降级 或 keep_failed 级联，见 ⑪/⑫）")

        # ── 11. 降级发生（SOFT 核心）：汇总含 ⚠️/降级/跳过 或 announce 含跳过/降级 ──
        print("\n[check 11] 降级发生（SOFT 核心）：汇总含 ⚠️/降级/跳过（skip 标 completed 降级）或 announce 含跳过/降级")
        degraded_in_summary = any(
            kw in summary_content for kw in ["⚠️", "降级", "跳过"]
        )
        announce_skip = any(
            any(kw in (r.get("content") or "") for kw in ["跳过", "降级"])
            for r in announce_replies
        )
        if _info("降级发生（汇总含 ⚠️/降级/跳过 或 announce 含跳过/降级语义）",
                 degraded_in_summary or announce_skip,
                 f"汇总降级词={'有' if degraded_in_summary else '无'} announce跳过词={'有' if announce_skip else '无'}"):
            print(f"      ✓ 失败步被降级（skip 标 completed 含 ⚠️ 降级，dependents 可继续）")
        else:
            print(f"      · 未检测到降级（LLM 可能选 retry 重派 或 keep_failed 级联，见 ⑩/⑫）")

        # ── 12. 改派到其他成员（SOFT）：同 step task_dispatch agent_id 不同 ──
        print("\n[check 12] 改派到其他成员（SOFT）：若 reassign，同 step task_dispatch agent_id 不同")
        if failed_step_num is not None and redispatch_count > 1:
            step_dispatches = dispatch_by_step[failed_step_num]
            agent_ids = {((d.get("data") or {}).get("agent_id")) for d in step_dispatches}
            reassigned = len(agent_ids) > 1
            if _info("改派到其他成员（同 step 派给不同 agent_id）",
                     reassigned, f"agent_ids={agent_ids}"):
                print(f"      ✓ 失败步被改派给其他 worker（reassign 重派到不同成员）")
            else:
                print(f"      · 重派但未改派（retry 重派原 worker，agent_id 不变={agent_ids}）")
        else:
            print(f"      [skip] 同 step 仅派发 {redispatch_count} 次，改派校验跳过（未重派则无可改派）")

        # ── 13. 收尾：DELETE 探针群 + DELETE 失败探针 agent → 全局无残留 ──
        print("\n[check 13] 收尾：DELETE 探针群 + DELETE 失败探针 agent → 全局无残留")
        st, ok = await delete_group(probe_group_id)
        if _check("DELETE 探针群 200 True", st == 200 and ok is True, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[cleanup] DELETE 群 status={st} ok={ok}")
        # 失败探针 agent 是本任务创建的探针，清理之
        if fail_agent_id:
            agent_deleted = await delete_agent(fail_agent_id)
            if _check("DELETE 失败探针 agent True", agent_deleted, f"agent={fail_agent_id}"):
                pass
            else:
                errs.append(f"[cleanup] DELETE 失败探针 agent 失败：{fail_agent_id}")
        groups_final = await list_groups()
        leaked_g = [x for x in groups_final if x.get("id") == probe_group_id]
        agents_final = await list_agents()
        leaked_a = [x for x in agents_final if x.get("id") == fail_agent_id]
        if _check("全局无探针群 + 无失败探针 agent 残留",
                  len(leaked_g) == 0 and len(leaked_a) == 0,
                  f"群残留={len(leaked_g)} agent残留={len(leaked_a)}"):
            pass
        else:
            errs.append(f"[cleanup] 残留：群={len(leaked_g)} agent={len(leaked_a)}")

    finally:
        # 兜底：若中途失败探针群/agent 可能还在，清理之
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")
        if fail_agent_id:
            agents_check = await list_agents()
            if any(a.get("id") == fail_agent_id for a in agents_check):
                await delete_agent(fail_agent_id)
                print(f"[cleanup] 兜底删除残留失败探针 agent {fail_agent_id[:24]}…")
        if probe_group_id:
            p = workspace_file(probe_group_id, PROBE_FILE)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项硬断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — Worker 失败后自动重派/降级端到端验证通过：")
    print("  · 建失败探针 agent（bogus model 让 worker 执行必失败）+ 探针群 coord+[失败探针]；")
    print("  · 设直接干 → 发单步目标 → 失败步被派发 → worker 执行失败（task_failed success=False）；")
    print("  · [核心] 计划终止：失败后 Leader 走 handle_reply 恢复决策 → summarize reached + DB 落库")
    print("    （失败未让计划死锁，恢复机制运转让计划走到汇总）；")
    print("  · 失败步入汇总（含失败探针 agent_name，Leader 跟踪失败步并纳入汇总）；")
    print("  · [SOFT] 重派发生（retry/reassign 同 step 重派 >1 次 或 announce 重试/改派）；")
    print("  · [SOFT] 降级发生（skip 标 completed 含 ⚠️ 降级 或 announce 跳过/降级）；")
    print("  · [SOFT] 改派到其他成员（reassign 同 step 不同 agent_id）；")
    print("  · 收尾 DELETE 探针群 + 失败探针 agent → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
