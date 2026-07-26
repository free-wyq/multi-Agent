"""任务18b — 定时任务全链路 e2e：建 interval=2s 任务 → 等触发 → 断言
ScheduledTaskRun running→success + push_task 到 agent + run_now/pause/resume。

确定性 e2e（不依赖 live server / 真实 LLM）。process-internal：隔离 DB +
AsyncIOScheduler 跑在本测试的 asyncio loop 上 + 直接调 api/scheduled_tasks 路由
函数（与 POST /api/scheduled-tasks 同路径）+ patch crud.finish_scheduled_task_run
卡住 run 终态翻转，确定性观察 running→success 过渡 + push_task 落 agent inbox。

为何 patch finish_scheduled_task_run 而非 push_task：_fire 顺序是
create_scheduled_task_run(running) → await push_task → await finish(success)。
finish 是最后一步，卡住它 = run 停在 running（push_task 已跑完，_task_queues 已有
item），test 从容观察 running + push_task 到 agent，再放行 finish → success。卡
push_task 会延迟 item 入队，无法在 running 期断言 _task_queues。

为何不跑真 agent LLM loop：本测聚焦 scheduler→push_task→agent inbox 链路
（TM-04/05/07），不验 agent 执行（worker LLM 范畴，任务16b 已覆）。push_task 落
_task_queues[group_id] 是「到 agent」的确定性真源（engine _run_loop 也从此 queue
拾取）。不建 engine = 不需要 fake LLM，链路更纯、更快、更稳。

patch 落点：_fire 内 ``from store import crud`` + ``crud.finish_scheduled_task_run``
——crud 是模块对象，crud.finish_scheduled_task_run 在调用时读模块属性，故 patch
``store.crud.finish_scheduled_task_run``（模块属性）即被 _fire 拾取（与 16b patch
``engine.worker.chat_completion_stream`` 同型：worker.py 顶层 ``from llm.client
import chat_completion_stream``，16b 改 ``engine.worker.chat_completion_stream``）。

四段契约（一个 task 走完全链路）：
  A. interval=2s 按计划触发：建 task(enabled) → APScheduler IntervalTrigger(2s)
     注册 → 等首火 → finish 卡住 → 断言 run running + _task_queues 有 item
     （receiver=agent / content 含「[定时任务:{name}]」/ data.run_id /
     data.scheduled_task_id）→ 放行 → run success + result 含
     「已派发给智能体 {agent_id}」→ pause 停后续火。
  B. run_now（TM-04 force 立即执行）：task 已 pause，run_now 走 _fire(force=True)
     绕过 enabled 检查 fire → running→success，新 run_id ≠ A 的。
  C. pause/resume（TM-05）：A 末 pause 后 enabled=False + scheduler 无 job
     （get_job None）→ resume 重注册 job（get_job 非 None）→ 等按计划火 →
     running→success，新 run_id ≠ A/B → 再 pause 收尾。
  D. 收尾：delete task + agent + group + shutdown_scheduler，断言 task/agent/group
     已删 + scheduler 单例已清。

pytest 收集：test_a..test_d + main()。每段末 assert not errs（pytest 真门），
main() 顺序调（直接跑也门）。无 conftest / 无 pytest-asyncio：异步段用 asyncio.run
包，每段独立 event loop（scheduler 单例跨段重建——_fire 的 crud 读隔离 DB 在每段
独立 _init_isolated_db 后重建，scheduler 仍活但 job 已在段内 pause/delete 清掉）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="tm_e2e_18b_")
os.environ["MULTI_AGENT_DATA_DIR"] = _TMP

import config  # noqa: E402

config.DATA_DIR = _TMP

import store.database as _db  # noqa: E402
import importlib  # noqa: E402
importlib.reload(_db)
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

PROBE_CONTENT = "定时任务 e2e 探针触发，请确认收到。"
INTERVAL_SECONDS = 2  # 首火 ~2s 后，足够调度又不太慢
FIRE_DEADLINE = 12.0  # 等火窗（2s 间隔 + APScheduler 精度 + 缓冲）


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


# ── DB + scheduler setup helper（每段独立 loop 内调） ─────────────────────────
async def _init_isolated_db() -> None:
    _db.engine = create_async_engine(
        _db.DB_URL, echo=False,
        connect_args={"check_same_thread": False}, pool_pre_ping=True,
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession,
    )
    await _db.init_db()
    # 活跃 provider cache：给 get_config() 一个不连真 LLM 的兜底配置（本测不调 LLM，
    # 但 init_db 的 load_active_provider_into_cache 路径 + crud 内部若有 get_config
    # 读取，兜底防意外打网络）。
    config.set_active_cache({
        "api_key": "sk-e2e-fake",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake-e2e-model",
        "temperature": 0.0,
        "max_tokens": 0,
    })


async def _create_probe_group_agent() -> tuple[str, str]:
    """建一个 e2e 探针 group + agent，返 (group_id, agent_id)。

    scheduler._fire 用 task.group_id / task.agent_id 调 push_task；group_id 不必
    真有引擎在跑——push_task 落 _task_queues[group_id] 是「到 agent」的确定性真源，
    无 engine 消费也不影响断言（item 入队即证明链路通）。建真实 group/agent 行让
    task 落库 + 收尾 delete 干净（不污染隔离 DB）。
    """
    from store import crud
    from models import AgentCreatePayload, GroupCreatePayload
    agent = await crud.create_agent(AgentCreatePayload(
        name=f"[e2e-18b] agent {uuid.uuid4().hex[:6]}",
        role="backend_engineer",
        system_prompt="你是定时任务 e2e 探针目标 agent。",
        description="任务18b e2e 探针",
    ))
    group = await crud.create_group(GroupCreatePayload(
        name=f"[e2e-18b] group {uuid.uuid4().hex[:6]}",
        coordinator_id=agent.id,
        member_ids=[agent.id],
        description="任务18b e2e 探针群",
    ))
    return group.id, agent.id


async def _wait_first_run(
    task_id: str, expect_status: str, deadline: float
) -> Any:
    """轮询 task 的 runs 直到出现 expect_status run 或超时。返回该 run 或 None。"""
    from store import crud
    import time
    while time.time() < deadline:
        runs = await crud.list_scheduled_task_runs(task_id)
        for r in runs:
            if r.status == expect_status:
                return r
        await asyncio.sleep(0.1)
    return None


# ── A. interval=2s 按计划触发（running→success + push_task 到 agent + pause）──
async def _async_a_interval_fire() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import ScheduledTaskCreatePayload
    from engine import scheduler as sch

    group_id, agent_id = await _create_probe_group_agent()
    task_name = f"[e2e-18b] interval 探针 {uuid.uuid4().hex[:6]}"
    task = await crud.create_scheduled_task(ScheduledTaskCreatePayload(
        name=task_name,
        content=PROBE_CONTENT,
        agent_id=agent_id,
        group_id=group_id,
        schedule_type="interval",
        interval_seconds=INTERVAL_SECONDS,
        enabled=True,
    ))
    if not _check("A1 建探针 task（id sched_ 前缀 / enabled=True）",
                 isinstance(task.id, str) and task.id.startswith("sched_") and task.enabled,
                 f"id={task.id} enabled={task.enabled}"):
        errs.append("[A1] 建探针 task 失败")
        return errs

    # 注册调度 job（复刻 create 路由 enabled 时 add_job）
    sch.add_job(task.model_dump())
    _check("A2 add_job 注册 IntervalTrigger(2s) job",
           sch.get_scheduler().get_job(sch._job_id(task.id)) is not None)

    # ── 卡住 finish_scheduled_task_run：_fire 跑到 push_task 后停在 finish 前，
    #    run 停在 running，test 从容观察 push_task 落 _task_queues + run running。
    import time as _time
    finish_gate = asyncio.Event()
    captured_runs: list[str] = []  # finish 收到的 run_id
    orig_finish = crud.finish_scheduled_task_run

    async def _hold_finish(run_id: str, success: bool, result: str):
        captured_runs.append(run_id)
        await finish_gate.wait()  # 卡住直到 test 放行
        return await orig_finish(run_id, success, result)

    crud.finish_scheduled_task_run = _hold_finish

    import time
    try:
        # 等首火：APScheduler IntervalTrigger(2s) 在 ~2s 后回调 _fire。
        # _fire: create_scheduled_task_run(running) → push_task → finish(被卡)。
        deadline = time.time() + FIRE_DEADLINE
        run = await _wait_first_run(task.id, "running", deadline)
        if not _check("A3 首火出现 running run（_fire create_scheduled_task_run 已跑）",
                      run is not None, f"deadline={FIRE_DEADLINE}s"):
            errs.append("[A3] 首火未出现 running run（APScheduler 未按计划 fire）")
            return errs

        # 断言 push_task 到 agent：_task_queues[group_id] 有 item，receiver=agent_id，
        # content 含「[定时任务:{name}]」，data.run_id / data.scheduled_task_id 对得上。
        from engine.inbox import _task_queues
        items = _task_queues.get(group_id, [])
        sched_items = [it for it in items
                       if it.get("receiver_id") == agent_id
                       and (it.get("data") or {}).get("scheduled_task_id") == task.id]
        if not _check("A4 push_task 落 agent inbox（_task_queues 有该 task 的 item）",
                      bool(sched_items), f"items={len(items)} sched={len(sched_items)}"):
            errs.append("[A4] push_task 未落 agent inbox（scheduler→agent 链路断）")
        else:
            it = sched_items[-1]
            ok_receiver = it.get("receiver_id") == agent_id
            ok_sender = it.get("sender_id") == "scheduler"
            ok_content = f"[定时任务:{task_name}]" in (it.get("content") or "")
            ok_run_id = (it.get("data") or {}).get("run_id") == run.id
            ok_sched_id = (it.get("data") or {}).get("scheduled_task_id") == task.id
            if not _check("A5 push_task item 字段：receiver=agent / sender=scheduler / "
                          "content 含「[定时任务:{name}]」/ data.run_id+scheduled_task_id 对齐",
                          all([ok_receiver, ok_sender, ok_content, ok_run_id, ok_sched_id]),
                          f"receiver={it.get('receiver_id')} sender={it.get('sender_id')} "
                          f"content_ok={ok_content} run_id_ok={ok_run_id} sched_id_ok={ok_sched_id}"):
                errs.append("[A5] push_task item 字段不对齐")

        # 放行 finish → run 翻 success
        finish_gate.set()
        # 等 finish 完成（_hold_finish 放行后调 orig_finish 写 success）
        deadline2 = time.time() + 5.0
        success_run = await _wait_first_run(task.id, "success", deadline2)
        if not _check("A6 放行 finish 后 run 翻 success", success_run is not None):
            errs.append("[A6] run 未翻 success（finish 异常）")
        else:
            ok_result = f"已派发给智能体 {agent_id}" in (success_run.result or "")
            if not _check(f"A7 success run result 含「已派发给智能体 {agent_id}」",
                          ok_result, f"result={success_run.result!r}"):
                errs.append("[A7] success run result 文案不对（_fire 写入值不匹配）")
            # captured_runs 应含该 run_id（证明 finish 被卡住后再放行）
            if not _check("A8 finish_scheduled_task_run 被卡住后放行（captured_runs 含 run_id）",
                          run.id in captured_runs):
                errs.append("[A8] finish patch 未生效（_hold_finish 未被调）")

        # pause 停后续火（TM-05）——A 段末必须 pause，否则 interval 持续火干扰 B/C
        from api.scheduled_tasks import pause
        paused = await pause(task.id)
        if not _check("A9 pause 后 enabled=False + scheduler 无 job",
                      paused is not None and paused.enabled is False
                      and sch.get_scheduler().get_job(sch._job_id(task.id)) is None,
                      f"enabled={paused.enabled if paused else None}"):
            errs.append("[A9] pause 未生效（job 仍在或 enabled 未翻）")
    finally:
        crud.finish_scheduled_task_run = orig_finish
        # 每段独立 loop：shutdown scheduler 单例，防下段 add_job 调 call_soon_threadsafe
        # 到已关闭的 loop（AsyncIOScheduler 绑定首个 loop，跨 asyncio.run 不迁移）。
        await sch.shutdown_scheduler()

    # 留 task_id/group_id/agent_id 给 D 段收尾（同一隔离 DB，跨段不重建则可直接用；
    # 但每段独立 loop + 独立 _init_isolated_db 重建 engine，DB 文件同路径故数据仍在——
    # 用模块级 _PROBE 记录 id 供 D 段删除）
    global _PROBE
    _PROBE = {"task_id": task.id, "group_id": group_id, "agent_id": agent_id}
    return errs


def test_a_interval_fire() -> None:
    print("\n=== A. interval=2s 按计划触发（running→success + push_task + pause）===")
    try:
        errs = asyncio.run(_async_a_interval_fire())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ A 段异常: {type(e).__name__}: {e}")
        errs = [f"[A] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── B. run_now（TM-04 force 立即执行，绕过 enabled） ──────────────────────────
async def _async_b_run_now() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from api.scheduled_tasks import run_now

    probe = _PROBE
    task = await crud.get_scheduled_task(probe["task_id"])
    if not _check("B1 task 仍存在且 enabled=False（A 段已 pause）",
                  task is not None and task.enabled is False):
        errs.append("[B1] task 不存在或未 pause")
        return errs

    prior_runs = {r.id for r in await crud.list_scheduled_task_runs(task.id)}

    # run_now 走 _fire(force=True) 绕过 enabled 检查 fire
    await run_now(task.id)
    # 给 _fire 跑完（push_task + finish——本段不卡 finish，_fire 内 await 链已跑完才返回）
    import time
    deadline = time.time() + 5.0
    new_run = None
    while time.time() < deadline:
        runs = await crud.list_scheduled_task_runs(task.id)
        for r in runs:
            if r.id not in prior_runs and r.status == "success":
                new_run = r
                break
        if new_run:
            break
        await asyncio.sleep(0.1)

    if not _check("B2 run_now 产生新 success run（force=True 绕过 enabled）",
                  new_run is not None):
        errs.append("[B2] run_now 未产生新 success run（force 路径未 fire）")
    else:
        if not _check("B3 新 run id ≠ A 段的 run", new_run.id not in prior_runs):
            errs.append("[B3] run_now 未产生新 run_id（复用了旧 run？）")
        # run_now 也会 push_task 到 agent（_fire 链路同）
        from engine.inbox import _task_queues
        items = _task_queues.get(probe["group_id"], [])
        force_items = [it for it in items
                       if (it.get("data") or {}).get("scheduled_task_id") == task.id]
        if not _check("B4 run_now 也 push_task 到 agent（_task_queues 有该 task item）",
                      len(force_items) >= 2,  # A 段 1 + B 段 1
                      f"force_items={len(force_items)}"):
            errs.append("[B4] run_now 未 push_task 到 agent")
    # 每段独立 loop：shutdown scheduler 单例，防下段 add_job 调 call_soon_threadsafe
    # 到已关闭的 loop（AsyncIOScheduler 绑定首个 loop，跨 asyncio.run 不迁移）。
    from engine import scheduler as sch
    await sch.shutdown_scheduler()
    return errs


def test_b_run_now() -> None:
    print("\n=== B. run_now（TM-04 force 立即执行，绕过 enabled）===")
    try:
        errs = asyncio.run(_async_b_run_now())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ B 段异常: {type(e).__name__}: {e}")
        errs = [f"[B] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── C. pause/resume（TM-05：job 摘除/重注册 + 按计划再火） ─────────────────────
async def _async_c_pause_resume() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from api.scheduled_tasks import resume, pause
    from engine import scheduler as sch
    import time

    probe = _PROBE
    task = await crud.get_scheduled_task(probe["task_id"])
    if not _check("C1 task enabled=False + scheduler 无 job（A 段 pause 后状态）",
                  task is not None and task.enabled is False
                  and sch.get_scheduler().get_job(sch._job_id(task.id)) is None):
        errs.append("[C1] 起点状态不对（task 未 pause 或 job 仍在）")
        return errs

    prior_runs = {r.id for r in await crud.list_scheduled_task_runs(task.id)}

    # resume：set_enabled(True) + add_job 重注册
    resumed = await resume(task.id)
    if not _check("C2 resume 后 enabled=True + scheduler 重注册 job",
                 resumed is not None and resumed.enabled is True
                 and sch.get_scheduler().get_job(sch._job_id(task.id)) is not None):
        errs.append("[C2] resume 未重注册 job")
        return errs

    # 等按计划火（IntervalTrigger(2s) ~2s 后首火）
    deadline = time.time() + FIRE_DEADLINE
    new_run = None
    while time.time() < deadline:
        runs = await crud.list_scheduled_task_runs(task.id)
        for r in runs:
            if r.id not in prior_runs and r.status == "success":
                new_run = r
                break
        if new_run:
            break
        await asyncio.sleep(0.1)
    if not _check("C3 resume 后按计划火 → 新 success run", new_run is not None):
        errs.append("[C3] resume 后未按计划火（scheduler 未 fire）")
    else:
        if not _check("C4 新 run id ≠ A/B 段的 run", new_run.id not in prior_runs):
            errs.append("[C4] resume 后未产生新 run_id")

    # 再 pause 收尾（停后续火）
    await pause(task.id)
    _check("C5 再 pause 停后续火（job 摘除）",
           sch.get_scheduler().get_job(sch._job_id(task.id)) is None)
    # 每段独立 loop：shutdown scheduler 单例，防下段 add_job 调 call_soon_threadsafe
    # 到已关闭的 loop（AsyncIOScheduler 绑定首个 loop，跨 asyncio.run 不迁移）。
    await sch.shutdown_scheduler()
    return errs


def test_c_pause_resume() -> None:
    print("\n=== C. pause/resume（TM-05：job 摘除/重注册 + 按计划再火）===")
    try:
        errs = asyncio.run(_async_c_pause_resume())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ C 段异常: {type(e).__name__}: {e}")
        errs = [f"[C] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── D. 收尾：删 task/agent/group + shutdown_scheduler ─────────────────────────
async def _async_d_cleanup() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from engine import scheduler as sch

    probe = _PROBE
    # delete task（路由级 remove_job + 级联删 runs）
    from api.scheduled_tasks import delete_scheduled_task
    ok = await delete_scheduled_task(probe["task_id"])
    if not _check("D1 delete task（remove_job + 级联删 runs）", ok):
        errs.append("[D1] delete task 失败")

    # 删 group（级联删 members/tasks/messages）+ agent
    ok_g = await crud.delete_group(probe["group_id"])
    ok_a = await crud.delete_agent(probe["agent_id"])
    if not _check("D2 delete group + agent", ok_g and ok_a,
                  f"group={ok_g} agent={ok_a}"):
        errs.append("[D2] delete group/agent 失败")

    # 断言 task/agent/group 已删
    t = await crud.get_scheduled_task(probe["task_id"])
    g = await crud.get_group(probe["group_id"])
    if not _check("D3 task/group 已删（get 返 None）", t is None and g is None):
        errs.append("[D3] 删除后仍能 get 到 task/group")

    # shutdown_scheduler：清进程级单例（_scheduler=None），不泄漏给后续测试
    await sch.shutdown_scheduler()
    if not _check("D4 scheduler 单例已清（_scheduler=None + 无残留 job）",
                  sch._scheduler is None):
        errs.append("[D4] scheduler 单例未清")
    return errs


def test_d_cleanup() -> None:
    print("\n=== D. 收尾：删 task/agent/group + shutdown_scheduler ===")
    try:
        errs = asyncio.run(_async_d_cleanup())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ D 段异常: {type(e).__name__}: {e}")
        errs = [f"[D] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── 模块级探针 id（跨段共享 task_id/group_id/agent_id） ──────────────────────
_PROBE: dict[str, str] = {}


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 70)
    print("任务18b 定时任务全链路 e2e：interval 触发 + run_now + pause/resume")
    print("=" * 70)
    for fn in (test_a_interval_fire, test_b_run_now,
               test_c_pause_resume, test_d_cleanup):
        fn()  # assert 内置失败即 raise
    print("\n" + "=" * 70)
    print("PASS — 定时任务全链路 e2e 验证通过：")
    print("  · A interval=2s 按计划 fire → run running→success + push_task 落 agent inbox")
    print("    （receiver=agent / sender=scheduler / content 含「[定时任务:{name}]」/ data 对齐）")
    print("    + pause 停后续火；")
    print("  · B run_now(force=True) 绕过 enabled 立即 fire → 新 success run（≠A）；")
    print("  · C resume 重注册 job → 按计划再火 → 新 success run（≠A/B）+ pause 收尾；")
    print("  · D task/agent/group 已删 + scheduler 单例已清。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
