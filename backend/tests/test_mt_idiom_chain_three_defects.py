"""成语接龙三缺陷回归（去中心化群图端到端·task19）.

用户实测「成语接龙」场景暴露三个群聊缺陷（设计真源见 memory
``decentralized-scheduling-stop-plan-2026-07-13``）：

  1. **顺序乱** — 后端工程师连发两条（先「等前端先来」再「发扬光大」），且后端抢在
     协调者 @前端 之前开口。同一 agent 一回合被驱动两次 + 抢序。
  2. **协调者每轮插话** — 用户每发一条新消息都从协调者先开口，agent 之间不能直接对话。
  3. **喊「停停停」停不下来** — 用户多次发「停停停」想中止接龙，turn/loop 继续运行
     （「停」被当普通消息推给协调者 → 反而驱动新一轮循环，火上浇油）。

去中心化群图迁移（task1-22，commit 623d974）已通电，三缺陷根因（去中心化路径只造了
消息流转、没造回合控制+可中止性）由四件套修复：

  · **route_user_message → GroupRuntime.invoke_turn**（task19④）— 一条用户消息 = 一次
    ainvoke = 一个有边界的回合（不再是裸 push_notify 链）。
  · **agent 节点 handoff goto + recent_speakers 图内防连发守卫**（make_agent_node 入口）—
    handoff 天然串行只一节点在跑（顺序乱根因①「两个节点同时跑的抢序」消失）；同一 agent
    一回合被驱动两次时守卫命中 END（顺序乱根因②「单线程内 LLM 把话筒 @回刚发过言的人
    形成 A→B→A→A 连发」堵死）。锁见 vh40。
  · **route_entry 闲聊分叉**（_looks_central / _is_report_back）— 闲聊 / @人 goto agent
    节点，协调者不被触达（问题2修复核心）。锁见 vh39。
  · **request_stop 软停 + cancel_turn 硬停双层**（task17/task23）— ``route_user_message``
    识别「停/stop/中断」关键词 → ``rt.request_stop()``（**短路在 invoke_turn 之前**，不
    路由给任何发言者、不开新回合——这正是问题3「停」反驱动新循环的修复）；UI 停止按钮
    → ``POST /groups/{id}/stop-turn`` → ``rt.cancel_turn()``（先 set 再 task.cancel 断流）。
    锁见 vh44/vh45。前置 BUG「group 路径派工事件断层 emit_task_dispatched」已修（vh54）。

本测试是 **live e2e**（起真后端 + 真 LLM，建多 agent 群发接龙，抓 WS 事件流）。
沿用 MT-09~MT-14 自测模式（httpx HTTP 真源 + WS 抓事件流 + reload 触发引擎启动 +
探针群隔离）。

**为何三缺陷的断言这样设计（避免 flaky + 测真机制非测 LLM 接龙质量）**：

  · **顺序不乱（HARD）**：缺陷是「同一 agent **一回合内**被驱动两次」（A→B→A→A）。
    recent_speakers 守卫在 **每个 invoke_turn 开头 reset 为 []**（fresh checkpointer
    thread），故守卫只在一回合内生效。跨回合的重复（回合1 以 B 结束、回合2 以 B 开局）
    是合理正常行为，**不是**缺陷。故断言落在「**单回合内**每个 agent 最多发言一次」——
    发一条接龙起手消息，抓该回合的 agent_reply 序列，断言无重复 agent（守卫把 A→B→A
    的第三个 A 堵死，回合内 agent 唯一）。这直接验守卫生效，不依赖 LLM 是否接出合法成语。
  · **协调者不插话（HARD）**：接龙回合（去中心化路径）期间无 coordinator_think 事件
    （协调者大脑未被触达）+ 协调者不在 agent_reply 序列（不抢话）。route_entry 闲聊分叉
    goto agent 节点，协调者根本不被触达——这是 route_entry 分叉生效的直接证据。
  · **喊停能停（HARD）**：核心缺陷是「停」被当普通消息推给协调者 → **驱动新一轮循环**
    （火上浇油）。修复是 ``_is_stop_phrase`` 在 ``invoke_turn`` **之前**短路 →
    ``request_stop()``（只 set event，不开新回合）。故断言：发「停」后短窗口内 **无新
    agent_reply / coordinator_think 事件**（「停」没驱动新循环）。再验 stop-turn 端点
    返回 ``{ok, cancelled, message}`` 结构（无活跃回合 cancelled=False 是幂等 no-op，
    不算失败——与自然完成不竞态）。

为何不强制 LLM 接出合法成语 / 多跳 handoff：LLM 输出不确定，可能接不上或改规则。
本测试验「三缺陷修复机制」，非「接龙接得对」。HARD 落在结构证据，SOFT 落在接龙质量。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
import websockets

BASE = "http://localhost:8000"
BACKEND_MAIN = "/home/wyq/work/project/multi-Agent/backend/main.py"

TIMEOUT = 20.0
RELOAD_WAIT = 45.0
# 接龙起手回合窗口（用户消息 → agent 间接龙 → 回合 END）。chat 路径无 task_complete
# 收尾信号，用静默期收尾。给足窗口让 handoff 跑几跳。
TURN_WINDOW = 75.0
# 发「停」后等多久判定「停」没驱动新循环（窗口内无新 agent_reply/coordinator_think）。
STOP_QUIET = 10.0
# 单回合静默期（收满即认为回合 END）。
TURN_QUIET = 12.0

PROBE_GROUP_NAME = "[IDIOM] 成语接龙三缺陷回归探针组"

# 起手消息：@前端工程师 开局，讲清接龙规则（让 LLM 走 chat + @handoff 路径，不拆 dispatch）。
STARTER = (
    "【成语接龙】@前端工程师 我们来玩成语接龙，你先说一个成语开头（四个字），"
    "然后 @后端工程师 接最后一个字开头的新成语，两人轮流接，规则：接得上就接，"
    "接不上就说接不上。开始吧。"
)

# 停止关键词（route_user_message 的 _is_stop_phrase 命中集：停/停止/中断/stop）。
STOP_WORD = "停"


async def health_ok() -> bool:
    async with httpx.AsyncClient(timeout=10.0) as c:
        try:
            r = await c.get(f"{BASE}/health")
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False


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


async def set_config(group_id: str, config: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.put(f"{BASE}/api/groups/{group_id}", json={"config": config})
        return r.json().get("config") if r.status_code == 200 else None


async def send_user_message(group_id: str, content: str) -> dict:
    # group-chat turns run the whole group-graph ainvoke inside the POST handler
    # (route_user_message → GroupRuntime.invoke_turn blocks until the turn ends).
    # httpx default 5s read timeout raises ReadTimeout — give the full turn window
    # (mirrors mt14/mt15/va1 posture). A「停」message short-circuits before
    # invoke_turn so it returns fast (no turn run), but the timeout is harmless.
    async with httpx.AsyncClient(timeout=300.0) as c:
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


async def stop_turn(group_id: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(f"{BASE}/api/groups/{group_id}/stop-turn")
        return r.json() if r.status_code == 200 else {}


async def list_messages(group_id: str, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(
            f"{BASE}/api/messages", params={"groupId": group_id, "limit": str(limit)}
        )
        return r.json() if r.status_code == 200 else []


async def reset_session(group_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            await c.post(f"{BASE}/api/groups/{group_id}/reset-session")
    except Exception:
        pass


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


async def collect_until_quiet(
    ws_url: str, quiet: float, hard_deadline: float, send_action=None
) -> list[dict]:
    """连 WS，send_action 发消息后，收到上一条事件 ``quiet`` 秒静默即收尾（或 hard_deadline 到）."""
    events: list[dict] = []
    last_ev_time = time.time()
    deadline = time.time() + hard_deadline
    async with websockets.connect(ws_url) as ws:
        if send_action is not None:
            await send_action()
            last_ev_time = time.time()
        while time.time() < deadline:
            remaining = min(quiet, deadline - time.time())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.3, remaining))
            except asyncio.TimeoutError:
                if time.time() - last_ev_time >= quiet:
                    break
                continue
            events.append(json.loads(raw))
            last_ev_time = time.time()
    return events


async def collect_window(ws_url: str, duration: float, send_action=None) -> list[dict]:
    """连 WS 收事件 ``duration`` 秒。"""
    events: list[dict] = []
    deadline = time.time() + duration
    async with websockets.connect(ws_url) as ws:
        if send_action is not None:
            await send_action()
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.3, remaining))
            except asyncio.TimeoutError:
                break
            events.append(json.loads(raw))
    return events


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def _info(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "·"
    tag = "INFO" if cond else "SOFT-MISS"
    print(f"  {mark} [{tag}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("=== 成语接龙三缺陷回归（去中心化群图 task19 live e2e） ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_group_id: str | None = None
    coord_id: str | None = None
    frontend_id: str | None = None
    backend_id: str | None = None

    try:
        # ── 0. 前置：候选池含 coordinator + 前端 + 后端 ──
        print("\n[check 0] 前置：候选池含 coordinator + 前端 + 后端")
        agents = await list_agents()
        coord = next((a for a in agents if a.get("role") == "coordinator"), None)
        frontend = next((a for a in agents if a.get("role") == "frontend_engineer"), None)
        backend = next((a for a in agents if a.get("role") == "backend_engineer"), None)
        if not (coord and frontend and backend) and len(agents) >= 3:
            if not coord:
                coord = agents[0]
            if not frontend:
                frontend = next((a for a in agents if a["id"] != coord["id"]), None) or agents[1]
            if not backend:
                backend = next(
                    (a for a in agents if a["id"] not in (coord["id"], frontend["id"])), None
                ) or agents[2]
            print("      [fallback] 种子角色缺失，退化取前 3 个 agent 组队")
        if not _check("候选池含 coordinator + 前端 + 后端", coord and frontend and backend,
                      f"coord={bool(coord)} fe={bool(frontend)} be={bool(backend)}"):
            errs.append("[pool] 候选不足 3 个，无法建探针群")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert coord and frontend and backend
        coord_id, frontend_id, backend_id = coord["id"], frontend["id"], backend["id"]
        print(f"      群主={coord_id}({coord['name']}) 前端={frontend_id}({frontend['name']}) "
              f"后端={backend_id}({backend['name']})")

        # ── 1. 建探针群：coord + [frontend, backend] ──
        print("\n[check 1] 建探针群：POST /api/groups（coord + [frontend, backend]）")
        st, g = await create_group({
            "name": PROBE_GROUP_NAME,
            "coordinator_id": coord_id,
            "description": "成语接龙三缺陷回归探针（顺序/插话/喊停）",
        })
        if not _check("HTTP 200 + Group", st == 200 and g is not None, f"status={st} body={g}"):
            errs.append(f"[create] 非 200 status={st}")
            print("\n=== 结果: FAIL ===")
            for e in errs:
                print(f"  - {e}")
            return 1
        assert g is not None
        probe_group_id = g["id"]
        for aid in (frontend_id, backend_id):
            await add_member(probe_group_id, aid, None)
        if _check("group_ 前缀 + coordinator_id==群主", str(g["id"]).startswith("group_")
                  and g.get("coordinator_id") == coord_id):
            print(f"      样本：id={g['id'][:24]}… coord={coord_id}")
        else:
            errs.append("[create] 群结构异常")

        # ── 2. 引擎启动：reload → 3 引擎 idle ──
        print("\n[check 2] 引擎启动：reload 触发 load_from_store → 3 引擎 idle")
        ready = await wait_for_engines(probe_group_id, expected=3)
        if not _check("reload 后探针群 3 引擎 idle", ready, "reload 后引擎未到位"):
            final = await group_status(probe_group_id)
            print(f"      [diag] 最终引擎数：{len(final)} -> {[(e['id'], e['status']) for e in final]}")
            errs.append("[engines] reload 后引擎未启动到位")
        else:
            engines = await group_status(probe_group_id)
            all_idle = all(e.get("status") == "idle" for e in engines)
            if _check("3 引擎 idle（含 coord + 前端 + 后端）", all_idle,
                      f"statuses={[e['status'] for e in engines]}"):
                print(f"      引擎：{[(e['id'], e['status']) for e in engines]}")
            else:
                errs.append("[engines] 引擎非全 idle")

        # auto_confirm=False（接龙走 chat，不该 fan-out 派工；默认即 False，显式确认）
        cur = await get_group(probe_group_id)
        cfg = dict((cur or {}).get("config") or {})
        cfg["auto_confirm"] = False
        await set_config(probe_group_id, cfg)
        # 清干净上下文（避免残留 plan/memory 污染接龙判定）
        await reset_session(probe_group_id)
        print("      [reset] session cleared（干净接龙上下文）")

        ws_url = f"ws://localhost:8000/ws/bus/{probe_group_id}"

        # ── 3. 缺陷①+②：发接龙起手 + 抓单回合事件流 ──
        # 一条用户消息 = 一次 invoke_turn = 一个回合（recent_speakers 该回合 reset []）。
        # 抓这一回合的 agent_reply 序列验「顺序不乱」+「协调者不插话」。
        print("\n[check 3] 发接龙起手 + 抓单回合事件流（验顺序不乱 + 协调者不插话）")

        async def _send_starter():
            await asyncio.sleep(0.3)
            msg = await send_user_message(probe_group_id, STARTER)
            print(f"      [send] 接龙起手 id={(msg.get('id') or '')[:16]}…")

        events = await collect_until_quiet(
            ws_url, quiet=TURN_QUIET, hard_deadline=TURN_WINDOW, send_action=_send_starter
        )
        type_counts: dict[str, int] = {}
        for e in events:
            t = e.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        print(f"      [events] 收到 {len(events)} 条; 类型分布={type_counts}")

        # ── 4. 缺陷①：顺序不乱（HARD）—— 单回合内每个 agent 最多发言一次 ──
        print("\n[check 4] 缺陷① 顺序不乱：单回合内每个 agent 最多发言一次（防连发守卫）")
        # 本回合 agent_reply 序列（按时序）。recent_speakers 守卫在该回合 reset []，
        # 若 LLM 把话筒 @回已发言者（A→B→A），第三个 A 的节点入口查 recent_speakers 命中
        # → Command(goto=END) 不发言。故一回合内每个 agent 至多出现一次。
        senders = [
            e.get("sender_id", "")
            for e in events
            if e.get("type") == "agent_reply" and e.get("sender_id") != "user"
        ]
        worker_senders = [s for s in senders if s in (frontend_id, backend_id)]
        print(f"      [senders] 本回合接龙发言序列（worker）={worker_senders}")
        if not _check("本回合有 >=1 条 agent_reply（接龙跑起来了）",
                      len(worker_senders) >= 1, f"worker_senders={worker_senders}"):
            errs.append("[chain] 接龙未跑起来（本回合无 worker agent_reply）")
        else:
            # HARD 核心：单回合内每个 agent 最多发言一次（无重复）—— 防连发守卫把
            # A→B→A 的第三个 A 堵死，回合内 agent 唯一。这是「同一 agent 一回合不被
            # 驱动两次」修复的直接证据（顺序乱根因②）。
            seen: set[str] = set()
            dup_agent: str | None = None
            for s in worker_senders:
                if s in seen:
                    dup_agent = s
                    break
                seen.add(s)
            if _check("单回合内无重复 agent（防连发守卫生效，顺序乱根因②已修）",
                      dup_agent is None,
                      f"重复 agent={dup_agent} seq={worker_senders}"):
                print(f"      ✓ recent_speakers 守卫：本回合发言 {worker_senders} 无 agent 连发")
            else:
                errs.append(f"[order] 单回合内 agent 重复={dup_agent}（防连发守卫未生效）："
                            f"seq={worker_senders}")
            # SOFT：接龙跨 >=2 个不同 agent（真多跳 handoff，非单 agent 自说自话）
            _info("接龙跨 >=2 个不同 agent（真多跳 handoff）",
                  len(set(worker_senders)) >= 2, f"unique={set(worker_senders)}")

        # ── 5. 缺陷②：协调者不插话（HARD）──
        print("\n[check 5] 缺陷② 协调者不插话：接龙回合无 coordinator_think / 协调者不抢话")
        coord_think_events = [e for e in events if e.get("type") == "coordinator_think"]
        coord_plan_events = [e for e in events if e.get("type") == "coordinator_plan"]
        coord_replies = [
            e for e in events
            if e.get("type") == "agent_reply" and e.get("sender_id") == coord_id
        ]
        print(f"      [coord] coordinator_think={len(coord_think_events)} "
              f"coordinator_plan={len(coord_plan_events)} coord_agent_reply={len(coord_replies)}")
        # HARD 核心：去中心化路径协调者大脑未被触达（route_entry 闲聊分叉不 goto classify）
        if _check("无 coordinator_think（协调者大脑未被触达，route_entry 闲聊分叉生效）",
                  len(coord_think_events) == 0,
                  f"coordinator_think={len(coord_think_events)}"):
            print(f"      ✓ route_entry 闲聊/@mention 分叉 goto agent 节点，协调者不被触达")
        else:
            errs.append(f"[chat] 接龙回合出现 coordinator_think={len(coord_think_events)}"
                        f"（协调者被触达=插话）")
        # HARD：协调者不在接龙发言序列（不抢话）。起手消息 @前端 → route_entry 直接 goto
        # 前端节点，协调者根本不触达，故 coord_replies 应为 0。
        if _check("协调者不在接龙 agent_reply 序列（不抢话）",
                  len(coord_replies) == 0, f"coord_replies={len(coord_replies)}"):
            pass
        else:
            errs.append(f"[chat] 协调者在接龙序列发言 {len(coord_replies)} 条（抢话=插话缺陷）")
        # SOFT：无 coordinator_plan（接龙未被误判 dispatch 拆计划）
        _info("无 coordinator_plan（接龙未被误判 dispatch 拆计划）",
              len(coord_plan_events) == 0, f"plan={len(coord_plan_events)}")

        # ── 6. 缺陷③：喊停能停（HARD）—— 「停」不驱动新循环 ──
        print("\n[check 6] 缺陷③ 喊停能停：发「停」→ 不驱动新循环（_is_stop_phrase 短路）")
        # 等上一回合彻底静默（确保无在途回合）
        await asyncio.sleep(3.0)
        msgs_before_stop = await list_messages(probe_group_id, limit=100)
        reply_before = len([m for m in msgs_before_stop if m.get("type") == "agent_reply"])
        think_before = 0  # think 事件不入库（仅 WS），用窗口内事件计数

        # 发「停」+ 抓 STOP_QUIET 窗口事件流。核心断言：「停」不驱动新循环——
        # route_user_message 的 _is_stop_phrase 命中 → request_stop → 短路在 invoke_turn
        # 之前，不路由给任何发言者、不开新回合。故窗口内无新 agent_reply / coordinator_think。
        async def _send_stop():
            await asyncio.sleep(0.3)
            msg = await send_user_message(probe_group_id, STOP_WORD)
            print(f"      [send] 「停」 id={(msg.get('id') or '')[:16]}…")

        stop_events = await collect_window(ws_url, STOP_QUIET, _send_stop)
        stop_type_counts: dict[str, int] = {}
        for e in stop_events:
            t = e.get("type", "?")
            stop_type_counts[t] = stop_type_counts.get(t, 0) + 1
        print(f"      [stop-events] 收到 {len(stop_events)} 条; 分布={stop_type_counts}")

        # 「停」自身会落一条 user_input（send_message 在 route_user_message 前落库 +
        # emit_message_added），这是正常的（「停」被系统接收）。但「停」不该驱动任何
        # agent_reply / coordinator_think（短路在 invoke_turn 前）。
        new_agent_replies = [
            e for e in stop_events
            if e.get("type") == "agent_reply" and e.get("sender_id") != "user"
        ]
        new_coord_thinks = [e for e in stop_events if e.get("type") == "coordinator_think"]
        print(f"      [stop] 窗口内新 agent_reply={len(new_agent_replies)} "
              f"coordinator_think={len(new_coord_thinks)}")

        # HARD 核心：「停」不驱动新循环——窗口内无新 agent_reply（「停」没让任何 agent 发言）
        if _check("「停」未驱动新 agent_reply（_is_stop_phrase 短路，不开新回合）",
                  len(new_agent_replies) == 0,
                  f"new_agent_replies={len(new_agent_replies)}"):
            print(f"      ✓ route_user_message 识别停关键词 → request_stop（短路在 invoke_turn 前）")
        else:
            errs.append(f"[stop] 「停」驱动了 {len(new_agent_replies)} 条新 agent_reply"
                        f"（停关键词未短路，火上浇油缺陷未修）")
        # HARD：「停」不触达协调者大脑（不驱动 coordinator_think）
        if _check("「停」未触达 coordinator_think（不驱动协调者新循环）",
                  len(new_coord_thinks) == 0,
                  f"new_coord_thinks={len(new_coord_thinks)}"):
            pass
        else:
            errs.append(f"[stop] 「停」触达 coordinator_think={len(new_coord_thinks)}（驱动新循环）")
        # SOFT：「停」自身 user_input 落库（系统接收了停止指令，非丢弃）
        stop_user_inputs = [e for e in stop_events if e.get("type") == "user_input"]
        _info("「停」自身 user_input 落库（系统接收停止指令，非丢弃）",
              len(stop_user_inputs) >= 1, f"user_input={len(stop_user_inputs)}")

        # ── 7. stop-turn 端点契约（HARD）—— 硬停端点结构 + 幂等 ──
        print("\n[check 7] stop-turn 端点契约：{ok, cancelled, message} 结构 + 幂等 no-op")
        # 无活跃回合时调 stop-turn → cancelled=False（幂等 no-op，与自然完成不竞态），
        # 不算失败。有活跃回合 → cancelled=True。本场景上一回合已 END，「停」也没开新
        # 回合，故预期 cancelled=False（无活跃回合可停）。核心验端点结构 + 不报错。
        resp = await stop_turn(probe_group_id)
        print(f"      [stop-turn] resp={resp}")
        if _check("stop-turn 响应 {ok, cancelled, message} 结构",
                  resp.get("ok") is True and "cancelled" in resp and "message" in resp,
                  f"resp={resp}"):
            pass
        else:
            errs.append(f"[stop-turn] 响应结构异常：{resp}")
        # cancelled=False 是合法（无活跃回合幂等 no-op）；cancelled=True 也合法（有活跃
        # 回合硬切）。两者都不算失败——核心是端点不报错 + 结构正确。
        _info("cancelled 字段合法（False=无活跃幂等 no-op / True=有活跃硬切）",
              resp.get("cancelled") in (True, False), f"cancelled={resp.get('cancelled')}")

        # ── 8. 收尾：DELETE 探针群 ──
        print("\n[check 8] 收尾：DELETE 探针群 → 全局无残留")
        st, ok = await delete_group(probe_group_id)
        if _check("DELETE 200 True", st == 200 and ok is True, f"status={st} ok={ok}"):
            pass
        else:
            errs.append(f"[cleanup] DELETE status={st} ok={ok}")
        groups_final = await list_groups()
        leaked = [x for x in groups_final if x.get("id") == probe_group_id]
        if _check("全局列表无探针群残留", len(leaked) == 0, f"{len(leaked)} 个残留"):
            pass
        else:
            errs.append("[cleanup] 探针群在全局列表残留")

    finally:
        if probe_group_id:
            g = await get_group(probe_group_id)
            if g is not None:
                await delete_group(probe_group_id)
                print(f"[cleanup] 兜底删除残留探针群 {probe_group_id[:24]}…")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项硬断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 成语接龙三缺陷回归端到端验证通过：")
    print("  · 建探针群 coord+[前端,后端] → reload 起 3 引擎 → auto_confirm=False（接龙走 chat）；")
    print("  · [缺陷①·顺序不乱] recent_speakers 图内防连发守卫：单回合内每个 agent 最多发言")
    print("    一次（A→B→A 的第三个 A 被守卫堵死，回合内 agent 唯一，无连发）；")
    print("  · [缺陷②·协调者不插话] route_entry 闲聊/@mention 分叉 goto agent 节点：")
    print("    接龙回合无 coordinator_think / 协调者不在发言序列（不被触达/不抢话）；")
    print("  · [缺陷③·喊停能停] _is_stop_phrase 在 invoke_turn 前短路 → request_stop：")
    print("    发「停」后窗口内无新 agent_reply / coordinator_think（不驱动新循环，火上浇油修复）；")
    print("    stop-turn 端点 {ok, cancelled, message} 结构正确 + 幂等 no-op。")
    print("  · 收尾 DELETE 探针群 → 全局无残留。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
