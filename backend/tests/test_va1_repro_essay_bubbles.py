"""A1 复现「写 200 字作文」3 气泡：起后端 + 前端，单聊/群聊各发一次.

任务定位根因（不修，只复现 + 定位）：
  作文请求返回 3 条消息：
    ① worker.py:307  node_execute   `收到，我来 {preview}...`   （worker brain 误判 execute）
    ② registry.py:179 _execute_body `▶ [{name}] 开始执行任务: ...`  （task_log）
    ③ registry.py:400 `_run_worker_task` `任务完成 🎉{snippet}`        （agent_reply）
  ——或——协调者把作文误判 dispatch（拆计划），那是另一条根因。
  本脚本两条路径都发一次，抓每条气泡的 sender_id / type / data，定位每条出自哪行，
  确认是「worker brain 误判 execute」还是「coordinator 误判 dispatch」。

两条场景各跑一次：

  场景 1 — 单聊（worker brain 路径）：
    单聊群「后端工程师」group_e53545c...（single_chat=true，coordinator_id=agent_backend_1）。
    agent_backend_1 走 worker 图（is_coordinator 但 single_chat→graph_kind=worker），
    裸消息（无 @mention）经 route_user_message → coordinator_id=agent_backend_1 →
    push_notify(coordinator_reply) → worker brain。
    若 brain 误判 execute → node_execute 推「收到，我来...」+ push_task →
    _handle_task → _run_worker_task（task_log「▶ 开始执行任务」+ create_react_agent
    真跑工具）→ task_complete + _reply「任务完成 🎉」。
    期望（健康）：brain 判 chat → node_chat 一条 agent_reply 带正文 + stats。

  场景 2 — 群聊（coordinator dispatch 路径）：
    群聊群 group_bee1d426...（coordinator=agent_coord_1，3 成员，auto_confirm=true）。
    裸消息经 route_user_message → coordinator_id=agent_coord_1 → push_notify(coordinator_reply)
    → coordinator 图 node_llm_decide。
    若 coordinator 误判 dispatch → node_dispatch 推「📋 已制定协作计划...」+ interrupt
    （auto_confirm 直接 fan-out）→ dispatch_next → _dispatch_one push_task 给某 worker →
    worker execute → task_log「▶ 开始执行任务」+ ... + task_complete + _reply「任务完成 🎉」。
    期望（健康）：coordinator 判 chat → node_chat 一条 agent_reply 带正文 + stats。

每条 agent_reply/task_log/task_dispatch/task_complete 事件抓 sender_id/type/data，
用「正文前缀/模板特征」定位它出自哪行代码：
  - worker.py:307  `收到，我来`         → node_execute 的 _unified_reply
  - registry.py:179 `▶ ... 开始执行任务`  → _execute_body 的 _publish_log
  - registry.py:400 `任务完成 🎉`          → _run_worker_task 的 _reply
  - coordinator.py node_dispatch `📋 已制定协作计划`  → coordinator 误判 dispatch

不修代码（A4/A5 才修 prompts.py）。本脚本只复现 + 定位 + 给结论。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8000"

# 场景 1：单聊群「后端工程师」（worker brain 路径）
SINGLE_GROUP = "group_e53545c71a8c4cf8ae5e69d06ef77952"
SINGLE_WORKER = "agent_backend_1"

# 场景 2：群聊群「[MT-14] 据结果调整后续计划探针组」（coordinator dispatch 路径）
# auto_confirm=true（直接干模式，dispatch 不停在确认卡，直接 fan-out——能完整跑到
# task_log/task_complete，便于抓 3 气泡）。3 成员：coord+前端+后端。
GROUP_GROUP = "group_bee1d4261ae2464f8cf14188fdeaa775"
GROUP_COORD = "agent_coord_1"

# 作文请求（用户报告的原始症状：「写 200 字作文」）
ESSAY_PROMPT = "请帮我写一篇 200 字的作文，题目是《晨光里的公园》"

WS_TIMEOUT = 240.0
# 静默期：收到上一条事件后，QUIET 秒内无新事件即认为本轮结束。
# chat 路径无 task_complete 收尾信号（node_chat 落 agent_reply 后即静默），
# execute/dispatch 路径有 task_complete 但也在其后陆续落 agent_reply/notify。
# 用静默期统一收尾，避免 chat 路径死等 task_complete 挂满 WS_TIMEOUT。
QUIET = 15.0


# ── 气泡特征定位表（content 前缀 → 出自哪行）──
def classify_bubble(ev: dict) -> str:
    """据 type + content 特征定位气泡出处。返回定位标签。"""
    t = ev.get("type", "")
    c = str(ev.get("content") or "")
    if t == "task_log":
        if "开始执行任务" in c:
            return "registry.py:179 _execute_body _publish_log「▶ 开始执行任务」"
        if "产物已记录" in c:
            return "registry.py:~388 _run_worker_task _publish_log「📦 产物已记录」"
        return f"registry.py:_publish_log task_log（{c[:24]}...）"
    if t == "task_dispatch":
        return "events/bus.py emit_task_dispatched「步骤 N 派发」"
    if t == "task_complete":
        return "registry.py:~381 emit_task_completed「task_complete」"
    if t == "agent_reply":
        if c.startswith("收到，我来"):
            return "worker.py:307 node_execute「收到，我来 {preview}...」 ← worker brain 误判 execute"
        if c.startswith("任务完成 🎉"):
            return "registry.py:400 _run_worker_task _reply「任务完成 🎉{snippet}」 ← execute 收尾"
        if c.startswith("📋 已制定协作计划"):
            return "coordinator.py node_dispatch「📋 已制定协作计划」 ← coordinator 误判 dispatch"
        if "执行出错了" in c:
            return "registry.py:~404 _run_worker_task _reply「执行出错了」 ← execute 失败"
        # 普通 chat 正文：带 stats 才算健康
        data = ev.get("data") or {}
        has_stats = isinstance(data, dict) and bool(data.get("elapsed_ms"))
        tag = " ✅带stats" if has_stats else " ⚠️无stats"
        return f"agent_reply 正文（chat 路径）{tag} ← 期望的健康路径"
    if t == "task_token":
        return "task_token（逐字流式，非气泡）"
    if t == "task_think":
        return "task_think（折叠块，非独立气泡）"
    return f"其他 {t}"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def all_idle(group_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{group_id}")
        for a in r.json():
            if a.get("status") != "idle":
                return False
    return True


async def send_message(group_id: str, content: str) -> dict:
    # group-chat coordinator turns run the whole group-graph ainvoke inside the
    # POST handler (route_user_message → GroupRuntime.invoke_turn blocks until
    # the turn ends, 13–42s for a coordinator plan+dispatch+summary chain).
    # httpx's default 5s read timeout raises ReadTimeout on the group-chat
    # scenario while single-chat (worker brain, ~2s) is unaffected. The
    # coordinator ainvoke is the real blocking work — give it the full turn
    # window (mirrors mt14/mt15's SUMMARY_TIMEOUT posture).
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
        return r.json()


async def reset_session(group_id: str) -> None:
    """reset-session：清持久化消息 + 清引擎内存态（memory/dispatch_plan/recent_routes）。

    让作文请求从干净上下文开始——避免上一轮残留 plan 把作文当 continue，或
    上一轮 memory 干扰 brain 判定。reset-session 既清 DB 消息也清引擎内存态。
    """
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(f"{BASE}/api/groups/{group_id}/reset-session")
    except Exception:
        pass


async def collect(timeout: float, stop_types: tuple[str, ...]) -> list[dict]:
    """连 WS 收事件直到命中任一 stop_types 且后续 QUIET 秒静默，或超时。

    收尾策略（双轨）：
      - 若命中 stop_types（execute/dispatch 的 task_complete/task_failed）：
        再多收 3s 尾巴（agent_reply/notify 紧随其后），结束。
      - 否则用静默期收尾：上一条事件后 QUIET 秒无新事件即结束。
        （chat 路径无 task_complete，靠静默期判定结束，不死等超时。）
    """
    events: list[dict] = []
    deadline = time.time() + timeout
    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
        while time.time() < deadline:
            remaining = deadline - time.time()
            wait = max(0.1, min(QUIET, remaining))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                # QUIET 秒静默 → 结束（chat 路径收尾）；或整体超时
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") in stop_types:
                # 命中收尾信号，再多收 3s 尾巴
                end = time.time() + 3.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                break
    return events


def extract_bubbles(events: list[dict]) -> list[dict]:
    """抽会渲染成聊天气泡的事件（agent_reply + task_log，前端 CHAT_MESSAGE_TYPES 白名单）。

    task_dispatch/task_complete/task_think/task_token 不在白名单（不桥接成独立气泡），
    但为复现/定位也一并抓出来标记「非独立气泡」，方便看全事件链。
    """
    # ChatPanel CHAT_MESSAGE_TYPES = {agent_reply, user_input, task_log, slash_card}
    BUBBLE_TYPES = {"agent_reply", "task_log"}
    bubbles = []
    for e in events:
        if e.get("type") in BUBBLE_TYPES:
            bubbles.append(e)
    return bubbles


async def run_scenario(name: str, group_id: str, prompt: str, stop_types: tuple[str, ...]) -> int:
    """跑一个场景：reset → 等空闲 → 连 WS 发作文 → 抓事件 → 定位气泡出处。"""
    global WS_URL
    WS_URL = f"ws://localhost:8000/ws/bus/{group_id}"

    print(f"\n{'='*72}")
    print(f"场景 {name}：group={group_id[:20]}... prompt={prompt!r}")
    print(f"{'='*72}")

    # reset 干净上下文
    await reset_session(group_id)
    print(f"[reset] {group_id[:20]}... session cleared")

    # 等全员 idle
    for _ in range(30):
        if await all_idle(group_id):
            break
        await asyncio.sleep(2)
    else:
        print(f"[fatal] {name}: agents 一直 busy，放弃"); return 2
    print(f"[idle] all agents idle")

    # 连 WS + 发作文
    ws_task = asyncio.create_task(collect(WS_TIMEOUT, stop_types))
    await asyncio.sleep(0.5)
    sent = await send_message(group_id, prompt)
    print(f"[send] user msg id={sent.get('id','')[:16]}...")
    events = await ws_task

    # 事件类型统计
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] {len(events)} 条; 分布={type_counts}")

    # 抓会成气泡的事件（agent_reply + task_log）+ 关键非气泡事件（dispatch/complete）
    bubbles = extract_bubbles(events)
    non_bubble_keys = [
        e for e in events
        if e.get("type") in ("task_dispatch", "task_complete", "task_failed")
    ]

    print(f"\n── 气泡（CHAT_MESSAGE_TYPES 白名单：agent_reply + task_log）共 {len(bubbles)} 条 ──")
    for i, b in enumerate(bubbles, 1):
        sid = b.get("sender_id")
        t = b.get("type")
        data = b.get("data")
        c = str(b.get("content") or "")
        loc = classify_bubble(b)
        data_summary = (
            "None" if data is None
            else f"keys={list(data.keys())}" if isinstance(data, dict)
            else f"{type(data).__name__}"
        )
        if isinstance(data, dict) and "elapsed_ms" in data:
            data_summary += (
                f" elapsed_ms={data.get('elapsed_ms')} tokens={data.get('tokens')} "
                f"model={data.get('model')!r} reasoning_tokens={data.get('reasoning_tokens')}"
            )
        print(f"  [{i}] sender_id={sid!r} type={t!r}")
        print(f"      data={data_summary}")
        print(f"      content={c[:80]!r}{'...' if len(c)>80 else ''}")
        print(f"      → 出处: {loc}")

    if non_bubble_keys:
        print(f"\n── 关键非气泡事件（dispatch/complete，看链路走向）共 {len(non_bubble_keys)} 条 ──")
        for e in non_bubble_keys:
            print(f"  type={e.get('type')!r} sender={e.get('sender_id')!r} "
                  f"task_id={str(e.get('task_id'))[:16]}... "
                  f"content={str(e.get('content') or '')[:50]!r}")

    # ── 根因判定 ──
    agent_replies = [b for b in bubbles if b.get("type") == "agent_reply"]
    has_execute_ack = any(str(b.get("content") or "").startswith("收到，我来") for b in agent_replies)
    has_task_log_start = any(
        b.get("type") == "task_log" and "开始执行任务" in str(b.get("content") or "")
        for b in bubbles
    )
    has_complete_reply = any(
        str(b.get("content") or "").startswith("任务完成 🎉") for b in agent_replies
    )
    has_dispatch_announce = any(
        str(b.get("content") or "").startswith("📋 已制定协作计划") for b in agent_replies
    )
    # 健康路径：只有正文 agent_reply（无 ack / 无 task_log开始 / 无完成），且带 stats
    chat_replies = [
        b for b in agent_replies
        if not str(b.get("content") or "").startswith(("收到，我来", "任务完成 🎉", "📋 已制定协作计划", "执行出错了"))
    ]
    healthy = (
        not has_execute_ack and not has_task_log_start and not has_complete_reply
        and not has_dispatch_announce
        and len(chat_replies) >= 1
        and all(
            isinstance(b.get("data"), dict) and bool(b.get("data", {}).get("elapsed_ms"))
            for b in chat_replies
        )
    )

    print(f"\n── 根因判定 ──")
    print(f"  worker brain 误判 execute?    {has_execute_ack}（worker.py:307「收到，我来」）")
    print(f"  registry task_log「开始执行」? {has_task_log_start}（registry.py:179）")
    print(f"  registry _reply「任务完成🎉」? {has_complete_reply}（registry.py:400）")
    print(f"  coordinator 误判 dispatch?     {has_dispatch_announce}（node_dispatch「📋」）")
    print(f"  健康 chat 正文气泡数           {len(chat_replies)}（带 stats）")
    print(f"  → 健康(1条chat+stats)?        {healthy}")

    if has_execute_ack or has_task_log_start or has_complete_reply:
        print(f"\n  结论[{name}]：⚠️ WORKER BRAIN 误判 EXECUTE——作文被当成动手任务，"
              f"走了 node_execute→push_task→_run_worker_task 全链路，产生 3 气泡"
              f"（ack/开始执行/任务完成）。根因在 build_brain_prompt 把「写文章」归 execute。")
    elif has_dispatch_announce:
        print(f"\n  结论[{name}]：⚠️ COORDINATOR 误判 DISPATCH——作文被当成工程任务拆计划，"
              f"node_dispatch 推「📋 已制定协作计划」。根因在 COORDINATOR_SYSTEM / "
              f"build_coordinator_prompt 把「写文章」归 dispatch。")
    elif healthy:
        print(f"\n  结论[{name}]：✅ 健康——作文走 chat 一条回复带 stats，无 3 气泡问题。")
    else:
        print(f"\n  结论[{name}]：❓ 异常但非上述任一根因——agent_reply 数={len(agent_replies)}，"
              f"需人工看上方事件链定位。")

    # 判定逻辑：复现到 3 气泡（任一根因）即 PASS（任务目的是复现+定位，不是修）
    reproduced = has_execute_ack or has_task_log_start or has_complete_reply or has_dispatch_announce
    if reproduced:
        print(f"  [复现] {name}: 已复现作文异常气泡（任务目的达成）")
    else:
        print(f"  [复现] {name}: 未复现异常气泡（可能 prompts 已修或 LLM 本次判 chat）")
    return 0


async def main() -> int:
    print("=== A1 复现「写 200 字作文」3 气泡：单聊 + 群聊各一次 ===")
    print("目的：抓 3 条气泡各自 sender_id/type/data，定位每条出自哪行，")
    print("     确认是 worker brain 误判 execute 还是 coordinator 误判 dispatch。")
    print("（不修代码，A4/A5 才修 prompts.py）\n")

    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 场景 1：单聊（worker brain 路径）
    # 单聊 chat 无 task_complete；execute 路径才有。stop 用 agent_reply + task_complete 兜底。
    await run_scenario(
        "1-单聊(worker brain)",
        SINGLE_GROUP, ESSAY_PROMPT,
        stop_types=("task_complete", "task_failed"),
    )

    # 场景 2：群聊（coordinator dispatch 路径）
    # coordinator chat → agent_reply；dispatch → task_complete（auto_confirm 直接 fan-out）。
    await run_scenario(
        "2-群聊(coordinator)",
        GROUP_GROUP, ESSAY_PROMPT,
        stop_types=("task_complete", "task_failed"),
    )

    print(f"\n{'='*72}")
    print("复现完成。结论见上方各场景「根因判定」段。")
    print(f"{'='*72}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
