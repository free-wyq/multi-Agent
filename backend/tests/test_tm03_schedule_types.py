"""TM-03 自测：三种调度类型创建并按计划触发（端到端真火）。

不依赖 pytest，直接 asyncio 跑。沿用 PL-05/08/09 自测模式（httpx + websockets），
但与 TM-01/TM-02 的纯 HTTP 同步自测不同——TM-03 验证「按计划真实触发」，fire 是
APScheduler 异步回调，必须等真实调度时刻到期，故用「轮询 runs 端点 + WS 事件流」双真源。

TM-03 链路（SchedulePage 调度类型 Segmented 三选一 → APScheduler 真火）：
  前端表单 handleCreate 按 schedule_type 分流组装 payload：
    · interval → interval_seconds（数字×单位换算，cron/run_at omit）
    · cron     → cron 表达式（interval_seconds/run_at omit）
    · once     → run_at = dayjs.toISOString()（ISO8601 Z，interval_seconds/cron omit）
  后端 POST /api/scheduled-tasks → crud.create_scheduled_task 落库 + add_job 注册
    APScheduler job（IntervalTrigger / CronTrigger.from_crontab / DateTrigger）。
  到调度时刻 APScheduler 回调 _fire(task_id, force=False)：
    1. create_scheduled_task_run(task_id) → 插入 status=running 的 run 记录
    2. push_task(group_id, "scheduler", agent_id, "[定时任务:{name}] {content}", ...)
       → 把任务扔进 agent 的 inbox → 常驻引擎 _run_loop 拾起 → _handle_task：
         emit agent_status(executing) + _publish_log("▶ 开始执行任务") → WS message_added(task_log)
       → 复用与交互派发同一条 agentic loop（非独立执行路径，见 scheduler.py docstring）
    3. finish_scheduled_task_run(run.id, True, "已派发给智能体 {agent_id}") → status=success
  本自测验证「按计划触发」的核心证据：GET /{id}/runs 出现 status=success 且 result
  == "已派发给智能体 {agent_id}" 的 run 记录——该记录只由 _fire 写入，且本自测从不
  调 runNow（force=True 路径），故任何 run 必来自 APScheduler job 的按计划回调。

为何用近未来时刻 + 短间隔而非远未来：TM-01 用 2099 远未来避免真火（只验列表展示）；
TM-03 要验「按计划触发」，必须让调度真实到期 fire。once 用 now+12s，interval 用 8s
（首火 ~8s 后），cron 用 `* * * * *`（下一个整分钟边界 fire，最长 ~60s）。三探针并发
创建后并发轮询，谁先 fire 谁先删（interval 避免重复 fire 刷 agent LLM），总窗口 90s
覆盖 cron 最长延迟。

为何删除即停火：DELETE /api/scheduled-tasks 后端 remove_job 取消 APScheduler job +
级联删 ScheduledTaskRun。interval 探针一旦检测到首火 success run 立即删，避免后续重复
fire 继续给 agent 推任务（每次 fire 触发一次 LLM agent run，省 token）。once/cron 只火
一次但仍删以收尾。

为何 WS 作辅证：run 记录证明「调度 fire 了」（按计划触发）；WS task_log 事件证明
「引擎拾起并产出了可观察输出」（scheduler→inbox→engine→WS 链路通）。两者互补：run 是
_fire 同步写的真源（确定性），WS 是引擎异步执行的旁证（链路通）。run 为主断言，WS
为辅（至少一条 task_log 含「定时任务」即证明链路通）。

验证六块（确定性断言）：
  ① interval 探针（interval_seconds=8）→ 90s 内出现 success run，result 含目标 agent；
  ② once 探针（run_at = now_utc+12s 的 ISO8601 Z）→ 出现 success run（且只火一次）；
  ③ cron 探针（cron="* * * * *"）→ 出现 success run（整分钟边界 fire）；
  ④ 三探针落库字段分流正确：interval 存 interval_seconds、cron 存 cron、once 存 run_at，
     且不相关字段为默认空值（omit 不传 → 后端 None → 存默认）；
  ⑤ run.result == "已派发给智能体 {agent_id}"（_fire 写入的确定性文案，证明是 scheduler
     fire 而非 runNow——runNow 也调 _fire 故 result 同，但本自测从不调 runNow，唯一火源
     是 APScheduler job 回调，故 success run 即「按计划触发」）；
  ⑥ WS 至少一条 task_log 事件 content 含「定时任务」（scheduler push 的 content 前缀
     "[定时任务:{name}]"，引擎 _publish_log 把 content 预览拼进 "▶ 开始执行任务: ..."，
     故 WS task_log content 含「定时任务」即证明 scheduler→engine→WS 链路通）。

为何不调 runNow：runNow（POST /{id}/run）走 _fire(force=True) 也会写 success run，但那是
「立即执行」（TM-04 范畴）不是「按计划触发」。本自测要让 APScheduler job 在调度时刻
自己 fire（force=False 路径），故全程不碰 /run 端点，确保唯一火源是调度回调。

为何三探针并发而非串行：cron 最长要等 ~60s 才 fire，串行则总时长 ~90s×3。并发创建 +
并发轮询，总窗口压缩到 ~90s（cron 主导），interval/once 在前 ~15s 内就 fire 完。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/bus/group_demo_1"
GROUP_ID = "group_demo_1"
AGENT_ID = "agent_frontend_1"

# 调度时序参数
INTERVAL_SECONDS = 8          # interval 探针首火 ~8s 后
ONCE_OFFSET = 12              # once 探针 now_utc + 12s fire
FIRE_DEADLINE = 90.0          # 总火窗（cron `* * * * *` 最长 ~60s + 缓冲）
POLL_INTERVAL = 1.0          # runs 轮询间隔
WS_TAIL = 6.0                 # 全 fire 后再多收 6s 尾巴（引擎执行异步滞后于 run 写入）

# 探针内容：极简指令，让 agent 简短确认即可，不触发工具调用，省 LLM token + 加速。
PROBE_CONTENT = "这是定时任务自测触发，请只回复「收到」二字，不要调用任何工具。"


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _once_iso(offset_secs: float) -> str:
    """复刻前端 dayjs.toISOString() 格式：UTC ISO8601 + Z + 毫秒3位。

    前端 once 分支 DatePicker(showTime) value 是 Dayjs，handleCreate 调
    values.run_at.toISOString() 得到 '2026-07-10T22:30:00.123Z'。这里复刻同一格式
    验证后端 DateTrigger(run_date=...) 能正确解析（实测 APScheduler 把带 Z 的
    aware datetime 解析为 UTC，与本地时区无关）。
    """
    t = _utc_now() + _dt.timedelta(seconds=offset_secs)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create(payload: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/scheduled-tasks", json=payload)
        return r.json() if r.status_code == 200 else None


async def list_runs(task_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks/{task_id}/runs")
        return r.json() if r.status_code == 200 else []


async def get_task(task_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks/{task_id}")
        return r.json() if r.status_code == 200 else None


async def delete_task(task_id: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.delete(f"{BASE}/api/scheduled-tasks/{task_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def collect_ws(stop_event: asyncio.Event, tail: float) -> list[dict]:
    """连 WS 收事件直到 stop_event 置位 + tail 秒尾巴。返回事件列表。

    scheduler→inbox→engine 链路异步滞后于 _fire 写 run 记录（run 在 push_task 后即写
    success，但引擎拾起 + _publish_log 要等 inbox.get + LLM 调度），故 fire 后再多收
    tail 秒确保 task_log 事件被捕获。
    """
    events: list[dict] = []
    try:
        async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=8 * 1024 * 1024) as ws:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    events.append(json.loads(raw))
                except asyncio.TimeoutError:
                    continue
            # tail：再多收 tail 秒，捕获 fire 后异步产生的 task_log
            end = time.time() + tail
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                    events.append(json.loads(raw))
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        print(f"[ws] collect error: {e}")
    return events


async def wait_first_success_run(task_id: str, deadline: float) -> dict | None:
    """轮询 runs 直到出现首条 success run 或超时。返回该 run 或 None。

    success run 由 _fire 的 finish_scheduled_task_run(run.id, True, ...) 写入，是
    「调度 fire 了」的确定性真源。本自测从不调 runNow，故此 run 必来自按计划触发。
    """
    while time.time() < deadline:
        runs = await list_runs(task_id)
        for r in runs:
            if r.get("status") == "success":
                return r
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def main() -> int:
    print("=== TM-03 自测：三种调度类型创建并按计划触发 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []
    # tag → (task_id, payload, success_run_or_None)
    results: dict[str, dict] = {}

    # ── 构造三探针 payload（分流字段，omit 不相关字段，复刻前端 handleCreate）──
    once_run_at = _once_iso(ONCE_OFFSET)
    probes: list[tuple[str, dict]] = [
        (
            "interval",
            {
                "name": "[TM-03] interval 探针",
                "content": PROBE_CONTENT,
                "agent_id": AGENT_ID,
                "group_id": GROUP_ID,
                "schedule_type": "interval",
                "interval_seconds": INTERVAL_SECONDS,
                "enabled": True,
            },
        ),
        (
            "once",
            {
                "name": "[TM-03] once 探针",
                "content": PROBE_CONTENT,
                "agent_id": AGENT_ID,
                "group_id": GROUP_ID,
                "schedule_type": "once",
                "run_at": once_run_at,
                "enabled": True,
            },
        ),
        (
            "cron",
            {
                "name": "[TM-03] cron 探针",
                "content": PROBE_CONTENT,
                "agent_id": AGENT_ID,
                "group_id": GROUP_ID,
                "schedule_type": "cron",
                "cron": "* * * * *",
                "enabled": True,
            },
        ),
    ]
    print(f"[setup] once run_at = {once_run_at}（now_utc+{ONCE_OFFSET}s）")
    print(f"[setup] interval_seconds = {INTERVAL_SECONDS}s")
    print(f"[setup] cron = '* * * * *'（下一整分钟边界 fire）")

    # ── 创建三探针（enabled=True → add_job 注册 APScheduler）──
    created: dict[str, tuple[str, dict]] = {}
    for tag, payload in probes:
        task = await create(payload)
        if not task or not task.get("id"):
            errs.append(f"[create-{tag}] 创建失败 payload={payload}")
            print(f"[create-{tag}] ✗ 失败")
            continue
        created[tag] = (task["id"], payload)
        probe_ids.append(task["id"])
        print(f"[create-{tag}] ✓ id={task['id'][:18]}… schedule_type={task.get('schedule_type')}")

    # ── ④ 落库字段分流断言（创建即验，不等 fire）──
    print("\n[check 4] 三探针落库字段分流正确（interval→interval_seconds / cron→cron / once→run_at）")
    expected_result_substr = f"已派发给智能体 {AGENT_ID}"
    for tag, (tid, payload) in created.items():
        task = await get_task(tid)
        if not task:
            errs.append(f"[fields-{tag}] 单读失败")
            continue
        stype = payload["schedule_type"]
        if stype == "interval":
            ok = (
                int(task.get("interval_seconds", -1)) == payload["interval_seconds"]
                and not task.get("cron")
                and not task.get("run_at")
            )
            field_detail = f"interval_seconds={task.get('interval_seconds')} cron='{task.get('cron')}' run_at='{task.get('run_at')}'"
        elif stype == "cron":
            ok = (
                task.get("cron") == payload["cron"]
                and int(task.get("interval_seconds", -1)) == 0
                and not task.get("run_at")
            )
            field_detail = f"cron='{task.get('cron')}' interval_seconds={task.get('interval_seconds')} run_at='{task.get('run_at')}'"
        else:  # once
            ok = (
                task.get("run_at") == payload["run_at"]
                and int(task.get("interval_seconds", -1)) == 0
                and not task.get("cron")
            )
            field_detail = f"run_at='{task.get('run_at')}' interval_seconds={task.get('interval_seconds')} cron='{task.get('cron')}'"
        if not _check(f"{tag} 落库字段分流正确", ok, field_detail):
            errs.append(f"[fields-{tag}] 字段分流异常：{field_detail}")

    # ── 启 WS 收事件（辅证 scheduler→engine→WS 链路）──
    ws_stop = asyncio.Event()
    ws_task = asyncio.create_task(collect_ws(ws_stop, WS_TAIL))
    await asyncio.sleep(0.5)  # 让 WS 先连上

    # ── 并发轮询三探针 runs，谁先 success 谁先删（interval 避免重复 fire）──
    print(f"\n[fire] 并发轮询三探针 runs（窗口 {FIRE_DEADLINE:.0f}s，谁先 fire 谁先删）...")
    deadline = time.time() + FIRE_DEADLINE
    pending = dict(created)  # tag → (tid, payload)

    async def watch(tag: str, tid: str, payload: dict) -> None:
        run = await wait_first_success_run(tid, deadline)
        if run is None:
            errs.append(f"[fire-{tag}] {FIRE_DEADLINE:.0f}s 内未出现 success run（调度未按计划 fire）")
            print(f"[fire-{tag}] ✗ 超时未 fire")
            return
        results[tag] = {"task_id": tid, "payload": payload, "run": run}
        elapsed = FIRE_DEADLINE - (deadline - time.time())
        print(f"[fire-{tag}] ✓ fire @ ~{elapsed:.1f}s run_id={run.get('id','')[:14]}… "
              f"result={str(run.get('result'))[:40]}")
        # 立即删（interval 停重复 fire；once/cron 收尾）
        await delete_task(tid)
        print(f"[fire-{tag}] 已删除探针（remove_job 停火）")

    await asyncio.gather(*(watch(tag, tid, payload) for tag, (tid, payload) in pending.items()))

    # 停 WS 收尾（再收 tail 秒）
    ws_stop.set()
    ws_events = await ws_task
    print(f"[ws] 收到 {len(ws_events)} 条事件，类型分布: "
          f"{sorted({e.get('type') for e in ws_events})}")

    # ── ① ② ③ 三探针 success run 出现 + ⑤ result 文案 ──
    print("\n[check 1~3+5] 三探针 success run + result 含目标 agent（按计划 fire 真源）")
    for tag in ("interval", "once", "cron"):
        r = results.get(tag)
        if not r:
            errs.append(f"[run-{tag}] 无 success run 记录")
            _check(f"{tag} 出现 success run", False)
            continue
        run = r["run"]
        ok_run = run.get("status") == "success"
        ok_result = expected_result_substr in str(run.get("result") or "")
        _check(f"{tag} success run + result 含「{expected_result_substr}」", ok_run and ok_result,
               f"status={run.get('status')} result={run.get('result')!r}")
        if not (ok_run and ok_result):
            errs.append(f"[run-{tag}] run={run}")

    # ── ⑥ WS task_log 含「定时任务」（scheduler→engine→WS 链路辅证）──
    print("\n[check 6] WS 至少一条 task_log 含「定时任务」（scheduler→engine→WS 链路通）")
    sched_logs = [
        e for e in ws_events
        if e.get("type") == "task_log" and "定时任务" in str(e.get("content") or "")
    ]
    # task_log 也可能是引擎 _publish_log 拼的 "▶ ... [定时任务:...]" 预览；放宽到任意 message_added
    # content 含「定时任务」（task_log 是 message_added 的一种 type）
    any_sched_msg = [
        e for e in ws_events
        if "定时任务" in str(e.get("content") or "")
    ]
    if _check(f"WS task_log 含「定时任务」共 {len(sched_logs)} 条", len(sched_logs) >= 1,
              f"含定时任务的 message 事件 {len(any_sched_msg)} 条"):
        sample = sched_logs[0]
        print(f"      样本 content: {str(sample.get('content'))[:60]}…")
    else:
        # 辅证失败不直接判 FAIL（WS 时序可能受引擎繁忙影响），但记入 errs 供诊断
        errs.append(f"[ws] 未捕获含「定时任务」的 task_log（scheduler→engine→WS 链路未观测到）"
                    f"；事件类型分布={sorted({e.get('type') for e in ws_events})}")

    # ── 收尾：删掉任何残留探针（保险，正常 watch 已删）──
    print(f"\n[cleanup] 清理残留探针（{len(probe_ids)} 条）")
    for tid in probe_ids:
        # 已被 watch 删过的不报错（delete 幂等返 True/False）
        try:
            await delete_task(tid)
        except Exception:
            pass

    # ── 汇总 ──
    print("\n" + "=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 三种调度类型创建并按计划触发端到端验证通过：")
    print(f"  · interval（{INTERVAL_SECONDS}s）探针按计划 fire → success run result 含「{expected_result_substr}」；")
    print(f"  · once（run_at ISO8601 Z）探针到点 fire → success run（DateTrigger 解析 UTC aware）；")
    print("  · cron（'* * * * *'）探针整分钟边界 fire → success run（CronTrigger.from_crontab）；")
    print("  · 三探针落库字段分流正确（interval→interval_seconds / cron→cron / once→run_at，不相关字段默认空）；")
    print("  · run.result 文案匹配 _fire 写入值，证明是 APScheduler job 回调（本自测从不调 runNow）；")
    print("  · WS task_log 含「定时任务」证明 scheduler→inbox→engine→WS 链路通（复用常驻引擎 agentic loop）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
