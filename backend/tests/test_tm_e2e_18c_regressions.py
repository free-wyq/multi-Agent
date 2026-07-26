"""任务18c 回归测试：锁住 18b e2e 暴露并修复的四类问题。

四段契约（确定性，process-internal，不依赖 live server / 真实 LLM）：

  A. **job 注册——空 cron / 空 run_at / interval≤0 / once 过期**（任务18c 核心修）：
     18b e2e 暴露的根因——``_build_trigger`` 在 ``crud.create_scheduled_task``
     **落库后** 才被 ``add_job`` 调用，对「cron 空 / once run_at 空 / once run_at
     已过 / interval_seconds≤0」直接 raise ``ValueError`` → 路由 500 + DB 留下孤儿行。
     任务18c 把校验前移到 persist 前（``validate_schedule`` 抛
     ``ScheduleConfigError`` → 路由捕 400），坏 task 根本不落库。本段直接调
     ``create_scheduled_task`` 路由函数，断言四种坏配置都返 ``HTTPException(400)`` +
     DB 无对应行（``get_scheduled_task`` 返 None）。同时断言 ``validate_schedule``
     对合法配置（cron 5 段式 / once 未来时刻 / interval>0）不 raise。

  B. **agent 未就绪——delete_agent 级联摘 job**（任务18c 修）：
     18b e2e D 段先 delete task 再 delete agent，没暴露「agent 上仍挂着定时任务 job」
     的孤儿场景。任务18c 给 ``DELETE /api/agents/{id}`` 加级联：先
     ``list_scheduled_tasks_for_agent`` 拿到该 agent 的所有 task，逐个 ``remove_job``，
     再删 agent。本段建一个 enabled interval task（add_job 注册了 APScheduler job）
     → 直接调 ``delete_agent`` 路由 → 断言 scheduler 上该 job 已摘（``get_job`` None）
     + task 行仍在（保留配置，只是不再 fire）。**不**走 ``delete_scheduled_task``——
     那会删 task 行，掩盖「agent 删了但 task 留着」的语义。

  C. **状态未更新——delete_group 级联摘 job**（任务18c 修）：
     同 B，但作用在 group 维度。``DELETE /api/groups/{id}`` 原只 ``stop_group`` 停引擎
     + crud 级联删 members/tasks/messages，**没**摘定时任务 job → 解散群后该群的
     interval task 还会按计划 fire，``push_task`` 进一个引擎已被 stop 的死 inbox
     （``_task_queues[group_id]`` 堆积无人消费）。任务18c 给路由加
     ``list_scheduled_tasks_for_group`` + 逐个 ``remove_job``。本段建 group+agent+
     enabled interval task → ``delete_group`` 路由 → 断言 job 已摘 + group 已删。
     用 ``test_c`` 而非 ``test_c_group`` 命名保持四段 A/B/C/D 一致。

  D. **时区——once run_at 的 aware/naive 归一**（任务18c 修）：
     18b e2e 只测 interval，没覆盖 once。前端 ``dayjs.toISOString()`` 发 UTC+Z（aware），
     直接调 API 的 caller 可能发 naive 本地串。APScheduler ``DateTrigger`` 对 aware 按
     其时区解释、对 naive 按 scheduler 本地时区解释——两种串原先可能在不同的墙钟时刻
     fire。任务18c 的 ``_parse_run_at`` 把两者都归一到 aware UTC。本段断言：
     ``_parse_run_at('2026-07-27T02:00:00.000Z')`` 与 ``_parse_run_at('2026-07-27T10:00:00')``
     （本地 +08）解释为**同一 UTC 时刻**（02:00Z == 10:00 +08），且都 aware；
     ``validate_schedule`` 接受未来 once、拒绝过去 once（过去 once 原先静默永不火）。

pytest 收集：``test_a``..``test_d`` + ``main()``。每段末 ``assert not errs``（pytest
真门——失败 raise AssertionError，pytest 判 FAIL；与 18b 一致，区别于 16c 的
``return errs`` 只 warn）。无 conftest / 无 pytest-asyncio：异步段 ``asyncio.run`` 包，
每段独立 event loop（scheduler 单例跨段重建——每段末 ``shutdown_scheduler`` 清单例，
防下段 ``add_job`` 调 ``call_soon_threadsafe`` 到已关闭的 loop，见
``scheduler-e2e-test-pattern-2026-07-27.md`` 的 AsyncIOScheduler 跨 asyncio.run 坑）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

# ── path + 隔离 DB（必须在 import app 模块前设） ──────────────────────────────
REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="tm_e2e_18c_")
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


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


async def _init_isolated_db() -> None:
    _db.engine = create_async_engine(
        _db.DB_URL, echo=False,
        connect_args={"check_same_thread": False}, pool_pre_ping=True,
    )
    _db.SessionLocal = async_sessionmaker(
        _db.engine, expire_on_commit=False, class_=AsyncSession,
    )
    await _db.init_db()
    config.set_active_cache({
        "api_key": "sk-e2e-fake",
        "base_url": "http://127.0.0.1:1/v1",
        "model": "fake-e2e-model",
        "temperature": 0.0,
        "max_tokens": 0,
    })


async def _create_probe_group_agent() -> tuple[str, str]:
    """建一个 e2e 探针 group + agent，返 (group_id, agent_id)（与 18b 同型）。"""
    from store import crud
    from models import AgentCreatePayload, GroupCreatePayload
    agent = await crud.create_agent(AgentCreatePayload(
        name=f"[e2e-18c] agent {uuid.uuid4().hex[:6]}",
        role="backend_engineer",
        system_prompt="你是 18c 回归探针目标 agent。",
        description="任务18c 回归探针",
    ))
    group = await crud.create_group(GroupCreatePayload(
        name=f"[e2e-18c] group {uuid.uuid4().hex[:6]}",
        coordinator_id=agent.id,
        member_ids=[agent.id],
        description="任务18c 回归探针群",
    ))
    return group.id, agent.id


# ── A. 空配置 400 + 无孤儿行（validate_schedule 前移到 persist 前） ──────────
async def _async_a_invalid_configs() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import ScheduledTaskCreatePayload
    from api.scheduled_tasks import create_scheduled_task
    from engine.scheduler import validate_schedule, ScheduleConfigError
    from fastapi import HTTPException

    group_id, agent_id = await _create_probe_group_agent()

    # ── 合法配置不 raise（回归：校验函数不放行坏配置的同时不能误拦好配置）──
    good_cases = [
        ("cron 合法 5 段式", {"schedule_type": "cron", "cron": "0 8 * * *"}),
        ("interval>0", {"schedule_type": "interval", "interval_seconds": 60}),
    ]
    # once 未来时刻：用 ISO+Z（前端 dayjs 格式），now_utc + 1h 保证在未来
    import datetime as _dt
    future_utc = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    good_cases.append(
        ("once 未来 ISO+Z", {"schedule_type": "once",
                             "run_at": future_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")}),
    )
    for label, extra in good_cases:
        payload = ScheduledTaskCreatePayload(
            name=f"[e2e-18c] good {uuid.uuid4().hex[:4]}",
            content="probe", agent_id=agent_id, group_id=group_id,
            enabled=False,  # 不真注册 job，只验校验放行
            **extra,
        )
        try:
            validate_schedule(payload.model_dump())
            _check(f"A0 合法配置放行：{label}", True)
        except ScheduleConfigError as e:
            _check(f"A0 合法配置放行：{label}", False, f"误拦: {e}")
            errs.append(f"[A0] 合法配置 {label} 被误拦: {e}")

    # ── 四种坏配置：create_scheduled_task 路由返 HTTPException(400) + 无孤儿行 ──
    bad_cases = [
        ("cron 空", {"schedule_type": "cron", "cron": ""}),
        ("cron 缺段", {"schedule_type": "cron", "cron": "0 8 *"}),
        ("once run_at 空", {"schedule_type": "once", "run_at": ""}),
        ("once 过去时刻", {"schedule_type": "once",
                          "run_at": "2020-01-01T00:00:00.000Z"}),
        ("interval=0", {"schedule_type": "interval", "interval_seconds": 0}),
        ("interval 负", {"schedule_type": "interval", "interval_seconds": -5}),
    ]
    for label, extra in bad_cases:
        payload = ScheduledTaskCreatePayload(
            name=f"[e2e-18c] bad {uuid.uuid4().hex[:4]}",
            content="probe", agent_id=agent_id, group_id=group_id,
            enabled=True,  # 即使 enabled 也不该落库（校验在前）
            **extra,
        )
        raised_400 = False
        try:
            await create_scheduled_task(payload)
        except HTTPException as e:
            raised_400 = e.status_code == 400
            if not _check(f"A1 {label} → HTTPException(400)", raised_400,
                          f"status={e.status_code}"):
                errs.append(f"[A1] {label} 状态码非 400: {e.status_code}")
        except Exception as e:  # noqa: BLE001
            _check(f"A1 {label} → HTTPException(400)", False,
                   f"非 HTTPException: {type(e).__name__}: {e}")
            errs.append(f"[A1] {label} 抛非 HTTPException: {type(e).__name__}")
        if not raised_400:
            # 路由没拒——查 DB 是否留孤儿行（这是 18c 的核心修复点）
            tasks = await crud.list_scheduled_tasks()
            orphan = [t for t in tasks if t.name == payload.name]
            if orphan:
                errs.append(f"[A1] {label} 路由未拒且留孤儿行 {orphan[0].id}")
                # 清理孤儿
                await crud.delete_scheduled_task(orphan[0].id)

    # ── update 路由同样 400（回归：update 也走 validate_schedule） ──
    # 先建一个合法 task，再 update 成坏配置
    import datetime as _dt2
    future2 = _dt2.datetime.now(_dt2.timezone.utc) + _dt2.timedelta(hours=2)
    good_task = await crud.create_scheduled_task(ScheduledTaskCreatePayload(
        name=f"[e2e-18c] update-target {uuid.uuid4().hex[:4]}",
        content="probe", agent_id=agent_id, group_id=group_id,
        schedule_type="interval", interval_seconds=120, enabled=False,
    ))
    bad_update = ScheduledTaskCreatePayload(
        name=good_task.name, content="probe",
        agent_id=agent_id, group_id=group_id,
        schedule_type="cron", cron="", enabled=False,  # 空 cron
    )
    raised_400_update = False
    try:
        await _update_via_route(good_task.id, bad_update)
    except HTTPException as e:
        raised_400_update = e.status_code == 400
        if not _check("A2 update 坏配置 → HTTPException(400)", raised_400_update,
                      f"status={e.status_code}"):
            errs.append(f"[A2] update 状态码非 400: {e.status_code}")
    except Exception as e:  # noqa: BLE001
        _check("A2 update 坏配置 → HTTPException(400)", False,
               f"非 HTTPException: {type(e).__name__}")
        errs.append(f"[A2] update 抛非 HTTPException: {type(e).__name__}")
    if raised_400_update:
        # 原 task 应仍可读且配置未变（update 被 400 拒，未落库）
        still = await crud.get_scheduled_task(good_task.id)
        if not _check("A3 update 被拒后原 task 配置未变（schedule_type 仍 interval）",
                      still is not None and still.schedule_type == "interval"
                      and still.interval_seconds == 120,
                      f"type={still.schedule_type if still else None}"):
            errs.append("[A3] update 被拒但原 task 配置已变（不应落库）")

    # ── load_from_store 跳过坏行不崩（启动期防御） ──
    # 直接往 DB 塞一行 cron 空的坏 task（绕过路由校验），再 load_from_store
    from store.entities import ScheduledTaskEntity
    from store.database import SessionLocal
    bad_id = f"sched_badlegacy_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as db:
        db.add(ScheduledTaskEntity(
            id=bad_id, name="[e2e-18c] legacy bad cron", content="x",
            agent_id=agent_id, group_id=group_id,
            schedule_type="cron", cron="",  # 空 cron（旧版本写入）
            enabled=1,
        ))
        await db.commit()
    from engine import scheduler as sch
    await sch.load_from_store()  # 不应崩
    # 坏行被跳过（无 job），但其他合法 task 的 job 仍在
    job = sch.get_scheduler().get_job(sch._job_id(bad_id))
    if not _check("A4 load_from_store 跳过坏 cron 行不崩（无 job 注册）",
                  job is None):
        errs.append("[A4] load_from_store 未跳过坏行（注册了 job）")
    await sch.shutdown_scheduler()  # 段末清单例（跨 asyncio.run 坑）

    # 收尾：删 group/agent（用路由级 delete_group 走 stop_group + 级联）
    # 这里直接 crud 删，因为本段已验完路由行为，收尾走 crud 更快
    await crud.delete_group(group_id)
    await crud.delete_agent(agent_id)
    return errs


async def _update_via_route(task_id: str, payload):
    """直接调 update_scheduled_task 路由函数（与 PUT /api/scheduled-tasks/{id} 同路径）。"""
    from api.scheduled_tasks import update_scheduled_task
    return await update_scheduled_task(task_id, payload)


def test_a_invalid_configs() -> None:
    print("\n=== A. 空配置 400 + 无孤儿行（validate_schedule 前移）===")
    try:
        errs = asyncio.run(_async_a_invalid_configs())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ A 段异常: {type(e).__name__}: {e}")
        errs = [f"[A] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── B. delete_agent 级联摘 job（agent 未就绪 → 孤儿 job 防护） ──────────────────
async def _async_b_delete_agent_cascade() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import ScheduledTaskCreatePayload
    from engine import scheduler as sch
    from api.scheduled_tasks import create_scheduled_task
    from api.agents import delete_agent

    group_id, agent_id = await _create_probe_group_agent()

    # 建一个 enabled interval task（add_job 注册 APScheduler job）
    task = await create_scheduled_task(ScheduledTaskCreatePayload(
        name=f"[e2e-18c] cascade-agent {uuid.uuid4().hex[:6]}",
        content="probe", agent_id=agent_id, group_id=group_id,
        schedule_type="interval", interval_seconds=3600, enabled=True,
    ))
    job_id = sch._job_id(task.id)
    if not _check("B1 task 建后 APScheduler 有 job",
                  sch.get_scheduler().get_job(job_id) is not None):
        errs.append("[B1] task 建后无 job")
        return errs

    # 直接调 delete_agent 路由（不删 task 行）——应级联摘 job
    ok = await delete_agent(agent_id)
    if not _check("B2 delete_agent 路由返 True", ok):
        errs.append("[B2] delete_agent 返 False")
    sch_after = sch.get_scheduler()
    job_gone = sch_after.get_job(job_id) is None
    if not _check("B3 delete_agent 后 task 的 APScheduler job 已摘（防孤儿 fire）",
                  job_gone):
        errs.append("[B3] delete_agent 后 job 仍在（级联摘 job 未生效）")

    # task 行仍在（保留配置，只是不再 fire）——用户可后续改 agent_id 重指
    task_still = await crud.get_scheduled_task(task.id)
    if not _check("B4 task 行仍在（delete_agent 保留调度配置，只摘 job）",
                  task_still is not None):
        errs.append("[B4] delete_agent 误删了 task 行（应保留配置）")

    # 收尾：删 task + group（agent 已删）
    await crud.delete_scheduled_task(task.id)
    await crud.delete_group(group_id)
    await sch.shutdown_scheduler()  # 段末清单例
    return errs


def test_b_delete_agent_cascade() -> None:
    print("\n=== B. delete_agent 级联摘 job（防孤儿 fire）===")
    try:
        errs = asyncio.run(_async_b_delete_agent_cascade())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ B 段异常: {type(e).__name__}: {e}")
        errs = [f"[B] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── C. delete_group 级联摘 job（状态未更新 → 死 inbox 堆积防护） ────────────────
async def _async_c_delete_group_cascade() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from store import crud
    from models import ScheduledTaskCreatePayload
    from engine import scheduler as sch
    from api.scheduled_tasks import create_scheduled_task
    from api.groups import delete_group

    group_id, agent_id = await _create_probe_group_agent()

    # 建一个 enabled interval task 挂在该 group
    task = await create_scheduled_task(ScheduledTaskCreatePayload(
        name=f"[e2e-18c] cascade-group {uuid.uuid4().hex[:6]}",
        content="probe", agent_id=agent_id, group_id=group_id,
        schedule_type="interval", interval_seconds=3600, enabled=True,
    ))
    job_id = sch._job_id(task.id)
    if not _check("C1 task 建后 APScheduler 有 job",
                  sch.get_scheduler().get_job(job_id) is not None):
        errs.append("[C1] task 建后无 job")
        return errs

    # 直接调 delete_group 路由——应 stop_group + 级联摘 scheduled job
    ok = await delete_group(group_id)
    if not _check("C2 delete_group 路由返 True", ok):
        errs.append("[C2] delete_group 返 False")
    job_gone = sch.get_scheduler().get_job(job_id) is None
    if not _check("C3 delete_group 后 task 的 job 已摘（防死 inbox 堆积）",
                  job_gone):
        errs.append("[C3] delete_group 后 job 仍在（级联摘 job 未生效）")

    # group 已删（crud 级联）；task 行仍在（delete_group 不级联删 scheduled_tasks，
    # 只摘 job——与 delete_agent 一致的「保留配置」语义）
    group_gone = await crud.get_group(group_id) is None
    if not _check("C4 group 已删（crud 级联）", group_gone):
        errs.append("[C4] delete_group 后 group 仍在")
    task_still = await crud.get_scheduled_task(task.id)
    if not _check("C5 task 行仍在（delete_group 保留调度配置，只摘 job）",
                  task_still is not None):
        errs.append("[C5] delete_group 误删了 task 行")

    # 收尾：删 task + agent（group 已删，但 task 行还在需手清；agent 还在）
    await crud.delete_scheduled_task(task.id)
    await crud.delete_agent(agent_id)
    await sch.shutdown_scheduler()  # 段末清单例
    return errs


def test_c_delete_group_cascade() -> None:
    print("\n=== C. delete_group 级联摘 job（防死 inbox 堆积）===")
    try:
        errs = asyncio.run(_async_c_delete_group_cascade())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ C 段异常: {type(e).__name__}: {e}")
        errs = [f"[C] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── D. once run_at 时区归一（aware UTC vs naive local 同一时刻） ────────────────
async def _async_d_timezone_normalize() -> list[str]:
    errs: list[str] = []
    await _init_isolated_db()
    from engine.scheduler import _parse_run_at, validate_schedule, ScheduleConfigError

    # ── aware UTC（前端 dayjs.toISOString 格式，带 Z） ──
    aware = _parse_run_at("2026-07-27T02:00:00.000Z")
    # ── naive local（直接 API caller，无 Z） ——本地是 +08（Asia/Shanghai），
    #    10:00 naive 本地 == 02:00 UTC，应与 aware 解释为同一 UTC 时刻 ──
    naive = _parse_run_at("2026-07-27T10:00:00")
    if not _check("D1 _parse_run_at 返 aware datetime（两种输入都 aware）",
                  aware.tzinfo is not None and naive.tzinfo is not None):
        errs.append("[D1] _parse_run_at 返 naive datetime（未归一时区）")
    # 两者应代表同一 UTC 时刻（02:00Z == 10:00 +08）
    same_moment = aware == naive
    if not _check("D2 aware 02:00Z 与 naive 10:00(+08) 归一到同一 UTC 时刻",
                  same_moment,
                  f"aware={aware} naive={naive}"):
        errs.append(f"[D2] 时区归一失败: aware={aware} naive={naive}（应相等）")

    # ── validate_schedule：未来 once 接受，过去 once 拒绝 ──
    import datetime as _dt
    future_iso = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    past_iso = "2020-01-01T00:00:00.000Z"
    try:
        validate_schedule({"schedule_type": "once", "run_at": future_iso})
        _check("D3 validate_schedule 接受未来 once", True)
    except ScheduleConfigError as e:
        _check("D3 validate_schedule 接受未来 once", False, f"误拒: {e}")
        errs.append(f"[D3] 未来 once 被误拒: {e}")
    try:
        validate_schedule({"schedule_type": "once", "run_at": past_iso})
        _check("D4 validate_schedule 拒绝过去 once", False, "未拒（会静默永不火）")
        errs.append("[D4] 过去 once 未被拒（原 18b 前的静默永不火 bug 未修）")
    except ScheduleConfigError:
        _check("D4 validate_schedule 拒绝过去 once（原静默永不火 bug 已修）", True)

    # ── _build_trigger 用归一后的 aware UTC 构 DateTrigger（不崩） ──
    from engine.scheduler import _build_trigger
    from apscheduler.triggers.date import DateTrigger
    trig = _build_trigger({"schedule_type": "once", "run_at": future_iso})
    if not _check("D5 once _build_trigger 返 DateTrigger（aware UTC）",
                  isinstance(trig, DateTrigger)):
        errs.append(f"[D5] once _build_trigger 非 DateTrigger: {type(trig)}")
    # next_fire_time 应在未来（aware）
    nxt = trig.get_next_fire_time(None, None)
    if not _check("D6 once trigger next_fire_time 在未来（aware）",
                  nxt is not None and nxt.tzinfo is not None
                  and nxt > _dt.datetime.now(_dt.timezone.utc)):
        errs.append(f"[D6] once next_fire_time 异常: {nxt}")

    from engine import scheduler as sch
    await sch.shutdown_scheduler()  # 段末清单例（防跨 loop 坑）
    return errs


def test_d_timezone_normalize() -> None:
    print("\n=== D. once run_at 时区归一（aware UTC vs naive local）===")
    try:
        errs = asyncio.run(_async_d_timezone_normalize())
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"  ✗ D 段异常: {type(e).__name__}: {e}")
        errs = [f"[D] 异常: {e}"]
    assert not errs, "\n".join(errs)


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main() -> int:
    print("=" * 70)
    print("任务18c 回归：定时任务 e2e 暴露的四类问题（job 注册/agent 未就绪/"
          "状态未更新/时区）")
    print("=" * 70)
    for fn in (test_a_invalid_configs, test_b_delete_agent_cascade,
               test_c_delete_group_cascade, test_d_timezone_normalize):
        fn()  # assert 内置失败即 raise
    print("\n" + "=" * 70)
    print("PASS — 任务18c 四类问题修复并锁住：")
    print("  · A 空配置（cron/run_at 空 / interval≤0 / once 过期）→ 400 + 无孤儿行")
    print("    （validate_schedule 前移到 persist 前）；load_from_store 跳过坏行不崩；")
    print("  · B delete_agent 级联摘 APScheduler job（防 agent 删后孤儿 fire）；")
    print("  · C delete_group 级联摘 job（防群解散后死 inbox 堆积）；")
    print("  · D once run_at aware/naive 归一到 UTC（前端 Z 与直调 naive 同一时刻 fire）")
    print("    + 过去 once 被拒（修原静默永不火）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
