"""MT-17 自测：超时自动触发降级（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MT-13~MT-16 自测模式（httpx HTTP 真源 +
WS 抓事件流 + reload 触发引擎启动 + 直接干模式 fan-out + 真执行 report-back）。

MT-17 链路（Worker 执行 hang 超时 → 看门狗 cancel → 合成失败 report-back → Leader MT-15 恢复 → 计划终止）：
  前端 GroupPage：用户发目标 → messageApi.send → POST /api/messages
  后端 send_message：route_user_message → coordinator 引擎 inbox notify
  coordinator 引擎 _handle_notify → LangGraph ainvoke（auto_confirm=True 直接干）：
    · node_llm_decide：拆出 plan（单步派给 worker）
    · node_dispatch → direct_run → node_dispatch_next → dispatch_ready_steps
      → _dispatch_one 派发步骤到 worker + emit_task_dispatched
  worker 引擎 _handle_task：创建 _worker_task（_execute_body → _run_worker_task → execute_agent_task
    → run_agent_loop → create_react_agent.astream_events 真调 LLM）。
    **MT-17 核心**：_handle_task 在创建 _worker_task 后挂 asyncio 看门狗（仅 worker，!is_coordinator）：
      watchdog = await _arm_timeout_watchdog(task, timeout)
      timeout = per-group config.worker_timeout 覆盖 > WORKER_TASK_TIMEOUT（默认 300s）
      本探针群设 worker_timeout=2（极短，强制看门狗 fire——真 LLM 调用 deepseek 首 token
      延迟 ~1-3s，2s 时 LLM 调用必在途，worker「长时间无响应」）。
    watchdog _watch 协程：asyncio.sleep(timeout) 后若 _worker_task 仍 running →
      置 _timeout_fired=True + _publish_log「⏱ 任务超时…自动降级」+ cancel(self._worker_task)。
    cancel 传播进 astream_events（async for 的 await 点抛 CancelledError，BaseException
    不被 run_agent_loop 的 except Exception 捕获）→ _execute_body → _worker_task 以 CancelledError 结束。
    _handle_task 的 `await self._worker_task` 抛 CancelledError → except 分支：
      _timeout_fired=True → _on_task_timed_out（区别于 _cancel_requested 的 PL-11 用户停）：
        · complete_task(task_id, False, "任务超时（worker 长时间无响应）"）
        · emit_task_completed(success=False) → WS task_failed（content 含「超时」）
        · _publish_log + _reply
        · **核心**：push_notify(agent_reply, worker→coordinator, {task_id, success:False})
          ——合成失败 report-back（与 _run_worker_task 真失败同通道），唤醒 Leader。
  coordinator 引擎 _handle_notify（处理超时 report-back）→ LangGraph ainvoke：
    · classify → handle_reply（data.task_id 匹配 dispatched step）
    · node_handle_reply：step.status=failed + result=汇报内容（含「超时」）。
      not success → _maybe_handle_step_failure（MT-15）调 LLM 选 retry/skip/reassign/keep_failed。
      单步 plan → all_done → summarize（无论恢复选哪个策略，计划都终止）。
    · node_summarize：从 plan 各 step 构建「🎉 全部完成！协作结果汇总」（超时步 ❌/降级步 ✅含⚠️）

「超时自动触发降级」的核心证据：
  ① 超时发生——worker 执行超过 worker_timeout（2s）无结果，看门狗 fire（cancel + task_failed），
     task_failed 的 content 含「超时/长时间无响应」语义（区别于 MT-15 bogus model 的「execution error: 400」）；
  ② 超时降级 report-back——_on_task_timed_out 合成 push_notify(success:False, task_id) 到 coordinator，
     唤醒 node_handle_reply 的 MT-15 恢复决策（retry/skip/reassign/keep_failed）；
  ③ 计划终止——超时未让计划死锁（step 不会永远停在 dispatched），handle_reply 恢复决策让计划走到 summarize。

为何用「极短 worker_timeout」而非「真 hang 端点」制造超时：MT-17 的机制是「worker 在 timeout 窗口内
无结果则降级」。确定性触发=让 timeout 远小于真实执行时间。真 LLM 调用（deepseek 首 token ~1-3s +
工具 + 多轮）必 >2s，故 worker_timeout=2 时看门狗必 fire（worker 必「长时间无响应」）。比「配一个
hang 端点」（需起 TCP black-hole server，且 agent 无 per-agent base_url 字段无法定向）更简单更确定性。
per-group config.worker_timeout 只影响本探针群（其他群用默认 300s），reload 起干净引擎 + 直接干让链路
纯净触发（沿用 MT-09~MT-16 隔离模式）。

为何单步 plan：单步超时后 all_done 必成立 → summarize 必达（无论 recovery 选哪个策略，计划都终止）。
隔离「超时降级」与「多步依赖级联」——单步验证超时检测+合成 report-back+恢复唤醒本身，多步级联是
dispatcher 已有能力（apply_fail_fast）。MT-17 聚焦「无 report-back 的超时感知降级」（区别 MT-15「有
失败 report-back 的恢复」——MT-15 worker 自己发 task_failed，MT-17 worker hang 不发自发 report-back，
看门狗代为合成）。

验证块（HARD 硬断言 + SOFT 软断言分层）：
  ① 前置：候选池含 coordinator + 一个 worker（建探针群 roster）；
  ② 建探针群：POST /api/groups（coord + [frontend]）→ 200 + Group；
  ③ 引擎启动：touch main.py reload → 轮询 status 直到 2 引擎 idle（Leader + worker）；
  ④ 设超时 + 直接干：PUT config {auto_confirm=True, worker_timeout=2}（极短超时强制看门狗 fire）；
  ⑤ 发目标 + 抓事件流：连 WS → POST /api/messages 发「前端创建 mt17_probe.md」→ 抓 task_dispatch +
     task_failed + announce + Leader 汇总（collect_until_summary 收到「全部完成」即收尾）；
  ⑥ 超时发生（HARD 核心）：task_failed 事件存在 + 其 content 含「超时/长时间无响应」语义
     （证明是看门狗超时降级路径 _on_task_timed_out，非正常失败/非 bogus 400）；
  ⑦ 超时步 task_dispatch 存在（HARD）：失败步确被派发过（超时发生在派发后的执行中）；
  ⑧ 计划终止（HARD 核心）：抓到 Leader 汇总 reply + DB 落库交叉——超时未让计划死锁，
     _on_task_timed_out 合成 report-back 唤醒 handle_reply 恢复决策让计划走到 summarize；
  ⑨ 超时步入汇总（HARD）：汇总 reply 含 worker agent_name（Leader 跟踪超时步并纳入汇总）；
  ⑩ 重派发生（SOFT）：同 step task_dispatch >1（retry/reassign 重派，可能触发二次超时）或
     announce 含重试/重派；
  ⑪ 降级发生（SOFT）：汇总含 ⚠️/降级/跳过（skip 标 completed 降级）或 announce 含跳过/降级；
  ⑫ 收尾：DELETE 探针群 → 全局无残留。

为何 HARD 核心是「超时发生 + 计划终止」而非「必重派/必降级」：MT-17 的机制是「worker hang 超时 →
看门狗 cancel + 合成失败 report-back → Leader 恢复决策」。确定性证据是「超时看门狗 fire（task_failed
含超时语义）+ 计划未死锁（summarize reached）」——若看门狗没 fire，worker 会永远 hang 在 LLM 调用，
step 永远停在 dispatched，计划死锁无 summarize。故「task_failed 含超时 + summarize reached」证明超时
检测+合成 report-back+恢复唤醒链路完整运转。具体 retry/skip/reassign/keep_failed 是 LLM 据超时原因的
决策（超时可能判 retry 重试——但同 worker 同 timeout 会再超时，达 MAX_RETRY_ATTEMPTS=2 后 keep_failed；
或判 skip 降级——非关键步骤容忍超时），作 SOFT——HARD 是「超时发生+恢复运转+计划终止」确定性链路。
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

TIMEOUT = 20.0
RELOAD_WAIT = 45.0
SUMMARY_TIMEOUT = 180.0  # 等超时→恢复决策→(可能重派再超时达 cap)→summarize 完整链路

# 探针群组名（[MT-17] 前缀便于溯源 + 清理识别）。
PROBE_GROUP_NAME = "[MT-17] 超时降级探针组"
WORKER_ROLE = "frontend_engineer"

# 极短 worker_timeout（秒）——强制看门狗 fire。真 LLM 调用（deepseek 首 token ~1-3s + 工具 + 多轮）
# 必 >WORKER_TIMEOUT_SEC，故 worker 必在窗口内「长时间无响应」触发超时降级。
WORKER_TIMEOUT_SEC = 2

# 目标——派给 worker 的单步任务（真执行真调 LLM，耗时 >WORKER_TIMEOUT_SEC 触发超时）。
GOAL = (
    "【MT-17】请前端工程师用 write_file 工具创建文件 mt17_probe.md，写入一行测试内容'MT17 超时降级探针'。"
    "请直接派发执行计划。"
)

# 产物文件名（超时 worker 可能未产出，清理兜底）。
PROBE_FILE = "mt17_probe.md"

# 超时语义关键词（HARD：task_failed content 含其一=看门狗超时路径，非 bogus 400）。
TIMEOUT_KEYWORDS = ["超时", "长时间无响应", "无响应", "timed out", "timeout"]


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}/health")
        return r.status_code == 200 and r.json().get("status") == "ok"


async def list_agents() -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{BASE}/api/agents")
        return r.json() if r.status_code == 200 else []


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


async def set_group_config(group_id: str, config: dict) -> dict | None:
    """PUT /api/groups/{id} 改 config（返回 config dict，增量合并）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        cur = (await c.get(f"{BASE}/api/groups/{group_id}")).json()
        merged = dict(cur.get("config") or {})
        merged.update(config)
        r = await c.put(f"{BASE}/api/groups/{group_id}", json={"config": merged})
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
    """touch main.py 触发 reload，轮询健康 + status 直到探针群引擎数 == expected。"""
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
    汇总 reply = node_summarize 产出的 agent_reply（sender=coordinator，content 含「全部完成」或「汇总」）。
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
    print("=== MT-17 自测：超时自动触发降级 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    worker_id: str | None = None
    worker_name: str | None = None

    try:
        # ── 1. 前置：候选池含 coordinator + 一个 worker ──
        print("\n[check 1] 前置：GET /api/agents 候选池含 coordinator + 一个 worker")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        worker = next((a for a in agents if a.get("role") == WORKER_ROLE), None)
        if not coord and agents:
            coord = agents[0]
            print("      [fallback] 无 coordinator 角色，退化取首个 agent 当群主")
        if not worker and len(agents) >= 2:
            worker = next((a for a in agents if a["id"] != (coord or {}).get("id")), None)
            print("      [fallback] 无 frontend_engineer 角色，退化取次个 agent 当 worker")
        if not _check("候选池含 coordinator + 一个 worker", coord is not None and worker is not None,
                      f"coord={bool(coord)} worker={bool(worker)}"):
            errs.append("[pool] 候选不足，无法建探针群")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord is not None and worker is not None
        coord_id, worker_id = coord["id"], worker["id"]
        worker_name = worker["name"]
        print(f"      群主={coord_id}({coord['name']}) worker={worker_id}({worker_name})")

        # ── 2. 建探针群：coord + [worker] ──
        print("\n[check 2] 建探针群：POST /api/groups（coord + [worker]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "MT-17 超时自动触发降级自测探针",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        await add_member(probe_group_id, worker_id, None)
        if _check("group_ 前缀 + coordinator_id==群主", str(g["id"]).startswith("group_")
                  and g.get("coordinator_id") == coord_id):
            print(f"      样本：id={g['id'][:24]}… coord={coord_id} worker={worker_id}")
        else:
            errs.append("[create] 群结构异常")

        # ── 3. 引擎启动：reload → 轮询 status 直到 2 引擎 idle ──
        print("\n[check 3] 引擎启动：reload 触发 load_from_store → 2 引擎 idle（Leader + worker）")
        ready = await wait_for_engines(probe_group_id, expected=2)
        if not _check("reload 后探针群 2 引擎 idle", ready, "reload 后引擎未到位"):
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'], e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            ids = {e["id"] for e in engines}
            all_idle = all(e.get("status") == "idle" for e in engines)
            if _check("2 引擎含 coord + worker 且全 idle",
                      {coord_id, worker_id}.issubset(ids) and all_idle,
                      f"ids={ids} statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎集合/idle 不符")

        # ── 4. 设超时 + 直接干：PUT config {auto_confirm=True, worker_timeout=2} ──
        print(f"\n[check 4] 设超时 + 直接干：PUT config {{auto_confirm=True, worker_timeout={WORKER_TIMEOUT_SEC}}}（极短超时强制看门狗 fire）")
        cfg = await set_group_config(probe_group_id, {
            "auto_confirm": True,
            "worker_timeout": WORKER_TIMEOUT_SEC,
        })
        if _check("config.auto_confirm==True + worker_timeout==2（回读确认）",
                  bool(cfg and cfg.get("auto_confirm") is True and cfg.get("worker_timeout") == WORKER_TIMEOUT_SEC),
                  f"config={cfg}"):
            print(f"      auto_confirm=True + worker_timeout={WORKER_TIMEOUT_SEC}s（worker 真执行 >2s 必超时，看门狗 fire 降级）")
        else:
            errs.append(f"[config] 未生效 config={cfg}")

        # ── 5. 发目标 + 抓事件流 ──
        print("\n[check 5] 发目标 + 抓 task_dispatch + task_failed + announce + Leader 汇总")
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

        # ── 6. 超时发生（HARD 核心）：task_failed 存在 + content 含超时语义 ──
        print("\n[check 6] 超时发生（HARD 核心）：task_failed 事件存在 + content 含「超时/长时间无响应」语义")
        if _check("task_failed 事件存在（worker 超时降级，success=False）",
                  len(failed_evs) >= 1, f"task_failed={len(failed_evs)} completes={len(completes)}"):
            fe = failed_evs[0]
            print(f"      [fail] sender={fe.get('sender_id')} task_id={(fe.get('task_id') or '')[:16]}… "
                  f"content={str(fe.get('content'))[:80]}")
        else:
            errs.append(f"[fail] 无 task_failed 事件（worker 未超时）：completes={len(completes)}")
        # 超时语义检测：任一 task_failed 的 content 含超时关键词 = 看门狗超时路径（非 bogus 400）
        timeout_failed = [
            f for f in failed_evs
            if any(kw in str(f.get("content") or "") for kw in TIMEOUT_KEYWORDS)
        ]
        if _check("task_failed content 含「超时/长时间无响应」语义（看门狗 _on_task_timed_out 路径，非 bogus 400）",
                  len(timeout_failed) >= 1,
                  f"超时 task_failed={len(timeout_failed)}/{len(failed_evs)} "
                  f"contents={[str(f.get('content'))[:40] for f in failed_evs[:3]]}"):
            print(f"      ✓ 超时降级路径确认：_on_task_timed_out 合成 task_failed（content 含超时语义）")
        else:
            errs.append(f"[fail] task_failed 无超时语义（非看门狗路径）：{[str(f.get('content'))[:60] for f in failed_evs[:3]]}")

        # ── 7. 超时步 task_dispatch 存在（HARD） ──
        print("\n[check 7] 超时步 task_dispatch 存在（HARD）：失败步确被派发过（超时发生在派发后的执行中）")
        if _check("超时步 task_dispatch 存在（派发后才超时）",
                  failed_step_num is not None and len(dispatches) >= 1,
                  f"dispatches={len(dispatches)} step={failed_step_num}"):
            print(f"      [dispatch] 超时步 step={failed_step_num} 派发 {len(dispatch_by_step.get(failed_step_num, []))} 次")
        else:
            errs.append(f"[dispatch] 无 task_dispatch（超时步未派发）：dispatches={len(dispatches)}")

        # ── 8. 计划终止（HARD 核心）：抓到 Leader 汇总 reply + DB 交叉 ──
        print("\n[check 8] 计划终止（HARD 核心）：抓到 Leader 汇总 reply + DB 交叉（超时未死锁）")
        summary_content = (summary_ev or {}).get("content") or ""
        print(f"      [summary] content 预览：{summary_content[:160]}…")
        if _check("捕获 Leader 汇总 reply（超时后计划走到 summarize，未死锁）",
                  summary_ev is not None, "未捕获汇总 reply（计划可能死锁在超时步）"):
            pass
        else:
            errs.append("[summary] 未捕获 Leader 汇总 reply（超时后计划未终止）")
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

        # ── 9. 超时步入汇总（HARD）：汇总含 worker agent_name ──
        print("\n[check 9] 超时步入汇总（HARD）：汇总 reply 含 worker agent_name（Leader 跟踪超时步并纳入汇总）")
        worker_name_in_summary = bool(worker_name) and worker_name in summary_content
        # LLM 可能用 role 泛称，兜底放宽
        role_in_summary = "前端工程师" in summary_content
        if _check("汇总 reply 含 worker agent_name（超时步被跟踪入汇总）",
                  worker_name_in_summary or role_in_summary,
                  f"summary 不含 {worker_name}/前端工程师"):
            pass
        else:
            errs.append(f"[summary] 汇总未含 worker name：{summary_content[:100]}")

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
            print(f"      ✓ 超时步被重派（retry/reassign 触发第二次 task_dispatch）或 Leader 公告重试/改派")
        else:
            print(f"      · 未检测到重派（LLM 可能选 skip 降级 或 keep_failed 级联，见 ⑪）")

        # ── 11. 降级发生（SOFT 核心）：汇总含 ⚠️/降级/跳过 或 announce 含跳过/降级 ──
        print("\n[check 11] 降级发生（SOFT 核心）：汇总含 ⚠️/降级/跳过 或 announce 含跳过/降级")
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
            print(f"      ✓ 超时步被降级（skip 标 completed 含 ⚠️ 降级，dependents 可继续）")
        else:
            print(f"      · 未检测到降级（LLM 可能选 retry 重派 或 keep_failed 级联，见 ⑩）")

        # ── 12. 收尾：DELETE 探针群 → 全局无残留 ──
        print("\n[check 12] 收尾：DELETE 探针群 → 全局无残留")
        st, ok = await delete_group(probe_group_id)
        if _check("DELETE 探针群 200 True", st == 200 and ok is True, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[cleanup] DELETE 群 status={st} ok={ok}")
        groups_final = await list_groups()
        leaked_g = [x for x in groups_final if x.get("id") == probe_group_id]
        if _check("全局无探针群残留", len(leaked_g) == 0, f"{len(leaked_g)} 个残留"):
            pass
        else:
            errs.append(f"[cleanup] 探针群在全局列表残留：{len(leaked_g)}")

    finally:
        # 兜底：若中途失败探针群可能还在（auto_confirm=True worker 在跑/超时重派中），清理之
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")
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
    print("PASS — 超时自动触发降级端到端验证通过：")
    print("  · 建探针群 coord+[worker] → reload 起 Leader + worker 引擎；")
    print("  · 设直接干 + 极短 worker_timeout=2s（强制看门狗 fire——真 LLM 调用 >2s 必超时）；")
    print("  · [核心] 超时发生：worker 执行超 2s 无响应 → 看门狗 cancel + task_failed 含「超时」语义；")
    print("  · [核心] 计划终止：_on_task_timed_out 合成失败 report-back 唤醒 Leader MT-15 恢复决策")
    print("    → summarize reached + DB 落库（超时未让计划死锁在 dispatched 步骤）；")
    print("  · 超时步入汇总（含 worker agent_name，Leader 跟踪超时步并纳入汇总）；")
    print("  · [SOFT] 重派发生（retry/reassign 同 step 重派 >1 次 或 announce 重试/改派）；")
    print("  · [SOFT] 降级发生（skip 标 completed 含 ⚠️ 降级 或 announce 跳过/降级）；")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
