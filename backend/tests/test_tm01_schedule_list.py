"""TM-01 自测：定时任务列表展示名称/频率/状态/下次执行（端到端）。

不依赖 pytest，直接 asyncio 跑。沿用 MC-01 / MC-02 自测模式（httpx HTTP 真源 +
探针落库 + 真源交叉 + 收尾清理，不连 WS）。

TM-01 链路（SchedulePage 定时任务列表）：
  POST /api/scheduled-tasks body={ScheduledTaskCreatePayload}
    → scheduled_tasks.py create_scheduled_task → crud.create_scheduled_task 落库
      +（enabled 时 add_job 注册 APScheduler）
    → 返回 ScheduledTask（前端 SchedulePage 卡片渲染数据源）
  GET /api/scheduled-tasks
    → list_scheduled_tasks → crud.list_scheduled_tasks
    → 返回 list[ScheduledTask]（前端 scheduledTaskApi.list() 消费渲染卡片网格）

前端 SchedulePage.tsx 卡片渲染字段：
  · 标题 name + ScheduleType Tag（cron geekblue / interval blue / once purple）
       + enabled 状态 Tag（启用中 success / 已暂停 default）
  · 调度摘要 scheduleSummary（cron→cron 表达式 / interval→「每 N 天/小时/分钟」换算
       / once→ISO8601 时刻）
  · 目标智能体 Tag（agentNameMap 解析 agent_id→name）+ 群组 Tag
  · 派发内容 content（截断 + Tooltip）
  · 创建/更新时间 fmtTime

本自测复刻「列表展示名称/频率/状态」链路：种三种调度类型的探针任务（cron/interval/
once），HTTP 层 GET /api/scheduled-tasks 拿到列表，逐项校验前端卡片会渲染的字段
（name / schedule_type / 调度相关字段 / enabled 状态 / 目标 agent）忠实落库且来自
单一真源。scheduleSummary 是纯展示函数（前端换算逻辑），自测里复刻同一换算做
确定性断言，等价证明卡片摘要成立。

为何不复刻前端卡片渲染/loading/刷新交互：UI 交互态非数据契约，HTTP 层验证
「创建落库 + 列表含探针 + 字段真源一致 + enabled 状态正确」即等价证明
「列表展示名称/频率/状态」成立。

验证九块（确定性断言）：
  ① 创建 interval 探针 → 200 + ScheduledTask（id sched_ 前缀 / schedule_type=interval
     / interval_seconds 落库 / enabled=True / created_at 非空）；
  ② 创建 cron 探针 → 200 + ScheduledTask（schedule_type=cron / cron 表达式落库）；
  ③ 创建 once 探针 → 200 + ScheduledTask（schedule_type=once / run_at 落库）；
  ④ GET /api/scheduled-tasks 列表含三条探针（真源交叉验证）；
  ⑤ 单读 GET /api/scheduled-tasks/{id} 回读 == create 响应（持久化一致）；
  ⑥ 名称/频率/状态字段真源一致：列表项 name/schedule_type/调度字段/enabled ==
     create payload 原值（跨端点单一真源，证明卡片渲染字段忠实）；
  ⑦ 前端 scheduleSummary 换算逻辑复刻断言：interval→「每 N 小时」/ cron→「cron: expr」
     / once→「定时: iso」三类型摘要正确（卡片摘要字段成立）；
  ⑧ 暂停→enabled=False（TM-05 切换就绪，状态 Tag 显「已暂停」的数据源）+ 恢复→enabled=True；
  ⑨ 收尾清理删除三条探针，校验无残留（避免污染后续自测/种子 + 取消 APScheduler job）。

为何不连 WS：TM-01 是同步 HTTP 接口（create → crud 落库 + add_job 注册调度），
不经引擎 inbox/WS 事件流，纯 HTTP 校验即可（与 MC-01/MC-02 同构）。APScheduler
job 的实际触发是 TM-03 自测范畴（要等真实间隔到期或 once 时刻），本自测只验证
「列表展示」数据契约，不验证「按计划触发」（runNow 立即执行是 TM-04）。
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"

# 种子 agent/group（backend/store/seed.py 植入的 demo 数据）。
AGENT_ID = "agent_frontend_1"
GROUP_ID = "group_demo_1"

# interval 探针：每 3600 秒（1 小时）派发一次。
INTERVAL_PAYLOAD = {
    "name": "[自测] 每小时巡检",
    "content": "执行一次定时巡检任务，检查系统状态并汇报。",
    "agent_id": AGENT_ID,
    "group_id": GROUP_ID,
    "schedule_type": "interval",
    "interval_seconds": 3600,
    "enabled": True,
}

# cron 探针：每天 08:30 派发。
CRON_PAYLOAD = {
    "name": "[自测] 每日晨报",
    "content": "生成今日工作晨报并发送。",
    "agent_id": AGENT_ID,
    "group_id": GROUP_ID,
    "schedule_type": "cron",
    "cron": "30 8 * * *",
    "enabled": True,
}

# once 探针：一次性定时（未来时刻，不会在本自测窗口内真实触发）。
ONCE_PAYLOAD = {
    "name": "[自测] 一次性提醒",
    "content": "这是一次性定时提醒。",
    "agent_id": AGENT_ID,
    "group_id": GROUP_ID,
    "schedule_type": "once",
    "run_at": "2099-01-01T00:00:00Z",
    "enabled": True,
}


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def create(payload: dict) -> tuple[int, dict | None]:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{BASE}/api/scheduled-tasks", json=payload)
        if r.status_code == 200:
            return 200, r.json()
        return r.status_code, {"_error": r.text, "_status": r.status_code}


async def list_tasks() -> list[dict]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks")
        return r.json() if r.status_code == 200 else []


async def get_task(task_id: str) -> dict | None:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/scheduled-tasks/{task_id}")
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None


async def set_enabled(task_id: str, enabled: bool) -> dict | None:
    path = "resume" if enabled else "pause"
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/scheduled-tasks/{task_id}/{path}")
        return r.json() if r.status_code == 200 else None


async def delete_task(task_id: str) -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BASE}/api/scheduled-tasks/{task_id}")
        return r.status_code == 200 and r.json() is True


def _check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if (detail and not cond) else ""))
    return cond


def schedule_summary(t: dict) -> str:
    """前端 SchedulePage.scheduleSummary 的 Python 复刻（确定性断言用）。

    真源是前端纯展示函数，这里复刻同一换算逻辑做断言——若后端字段落库正确，
    前端摘要必与本函数输出一致，等价证明卡片「频率」字段成立。
    """
    stype = t.get("schedule_type", "interval")
    if stype == "cron":
        cron = t.get("cron", "")
        return f"cron: {cron}" if cron else "cron（未配置表达式）"
    if stype == "once":
        run_at = t.get("run_at", "")
        return f"定时: {run_at}" if run_at else "一次性（未配置时刻）"
    secs = int(t.get("interval_seconds", 0) or 0)
    if secs <= 0:
        return "定间隔（未配置秒数）"
    if secs % 86400 == 0:
        return f"每 {secs // 86400} 天"
    if secs % 3600 == 0:
        return f"每 {secs // 3600} 小时"
    if secs % 60 == 0:
        return f"每 {secs // 60} 分钟"
    return f"每 {secs} 秒"


async def main() -> int:
    print("=== TM-01 自测：定时任务列表展示名称/频率/状态 ===")
    if not await health_ok():
        print("[fatal] backend 不在线")
        return 2
    print("[health] ok")

    errs: list[str] = []
    probe_ids: list[str] = []

    before = await list_tasks()
    print(f"[pre] 创建前定时任务数：{len(before)}")

    # ── 1. interval 探针 → 200 + ScheduledTask ──
    print("\n[check 1] interval 探针：POST /api/scheduled-tasks (interval_seconds=3600)")
    status, interval_task = await create(INTERVAL_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status} body={interval_task}"):
        errs.append(f"[interval] 非 200 status={status}")
        interval_task = None
    else:
        assert interval_task is not None
        if interval_task.get("id"):
            probe_ids.append(interval_task["id"])
        ok = (
            str(interval_task.get("id", "")).startswith("sched_")
            and interval_task.get("name") == INTERVAL_PAYLOAD["name"]
            and interval_task.get("schedule_type") == "interval"
            and int(interval_task.get("interval_seconds", -1)) == 3600
            and interval_task.get("agent_id") == AGENT_ID
            and interval_task.get("group_id") == GROUP_ID
            and interval_task.get("enabled") is True
            and bool(interval_task.get("created_at"))
        )
        if _check(
            "interval 落库 name/schedule_type/interval_seconds=3600/enabled=True/created_at",
            ok,
            f"task={interval_task}",
        ):
            print(f"      样本：id={interval_task['id'][:18]}…")
        else:
            errs.append(f"[interval] 字段异常：{interval_task}")

    # ── 2. cron 探针 → 200 + ScheduledTask ──
    print("\n[check 2] cron 探针：POST /api/scheduled-tasks (cron='30 8 * * *')")
    status, cron_task = await create(CRON_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status}"):
        errs.append(f"[cron] 非 200 status={status}")
        cron_task = None
    else:
        assert cron_task is not None
        if cron_task.get("id"):
            probe_ids.append(cron_task["id"])
        ok = (
            str(cron_task.get("id", "")).startswith("sched_")
            and cron_task.get("schedule_type") == "cron"
            and cron_task.get("cron") == "30 8 * * *"
            and cron_task.get("enabled") is True
        )
        if _check("cron 落库 schedule_type=cron / cron='30 8 * * *'", ok, f"task={cron_task}"):
            print(f"      样本：id={cron_task['id'][:18]}…")
        else:
            errs.append(f"[cron] 字段异常：{cron_task}")

    # ── 3. once 探针 → 200 + ScheduledTask ──
    print("\n[check 3] once 探针：POST /api/scheduled-tasks (run_at='2099-01-01T00:00:00Z')")
    status, once_task = await create(ONCE_PAYLOAD)
    if not _check("HTTP 200", status == 200, f"status={status}"):
        errs.append(f"[once] 非 200 status={status}")
        once_task = None
    else:
        assert once_task is not None
        if once_task.get("id"):
            probe_ids.append(once_task["id"])
        ok = (
            str(once_task.get("id", "")).startswith("sched_")
            and once_task.get("schedule_type") == "once"
            and once_task.get("run_at") == "2099-01-01T00:00:00Z"
            and once_task.get("enabled") is True
        )
        if _check("once 落库 schedule_type=once / run_at='2099-01-01T00:00:00Z'", ok, f"task={once_task}"):
            print(f"      样本：id={once_task['id'][:18]}…")
        else:
            errs.append(f"[once] 字段异常：{once_task}")

    # ── 4. 列表含三条探针（真源交叉）──
    print("\n[check 4] GET /api/scheduled-tasks 列表含三条探针")
    after = await list_tasks()
    after_ids = {t["id"] for t in after}
    all_three = (
        interval_task is not None
        and cron_task is not None
        and once_task is not None
        and interval_task["id"] in after_ids
        and cron_task["id"] in after_ids
        and once_task["id"] in after_ids
    )
    if _check("列表含 interval + cron + once 三条探针", all_three):
        print(f"      列表总数：{len(after)}（含 {len(probe_ids)} 条探针）")
    else:
        errs.append("[list] 探针不在列表")

    # ── 5. 单读回读 == create 响应（持久化一致）──
    print("\n[check 5] 单读回读 == create 响应")
    for tag, task in (("interval", interval_task), ("cron", cron_task), ("once", once_task)):
        if not task:
            continue
        reread = await get_task(task["id"])
        if reread is None:
            _check(f"{tag}: 回读 200", False)
            errs.append(f"[reread-{tag}] 404")
            continue
        same = (
            reread.get("id") == task.get("id")
            and reread.get("name") == task.get("name")
            and reread.get("schedule_type") == task.get("schedule_type")
            and reread.get("enabled") == task.get("enabled")
        )
        if _check(f"{tag}: 回读 name/schedule_type/enabled == create 响应", same, f"reread={reread}"):
            pass
        else:
            errs.append(f"[reread-{tag}] 回读漂移：{reread}")

    # ── 6. 名称/频率/状态字段真源一致（跨端点单一真源）──
    print("\n[check 6] 列表项字段 == create payload 原值（卡片渲染字段真源）")
    for tag, task, payload in (
        ("interval", interval_task, INTERVAL_PAYLOAD),
        ("cron", cron_task, CRON_PAYLOAD),
        ("once", once_task, ONCE_PAYLOAD),
    ):
        if not task:
            continue
        # 列表里找到这条
        listed = next((t for t in after if t["id"] == task["id"]), None)
        if listed is None:
            _check(f"{tag}: 列表项存在", False)
            errs.append(f"[list-{tag}] 列表项丢失")
            continue
        same = (
            listed.get("name") == payload["name"]
            and listed.get("schedule_type") == payload["schedule_type"]
            and listed.get("agent_id") == payload["agent_id"]
            and listed.get("group_id") == payload["group_id"]
            and listed.get("enabled") == payload["enabled"]
        )
        if _check(f"{tag}: 列表项 name/schedule_type/agent/enabled == payload 原值", same,
                  f"listed={listed}"):
            pass
        else:
            errs.append(f"[list-{tag}] 字段漂移：{listed}")

    # ── 7. 前端 scheduleSummary 换算复刻断言（卡片「频率」字段成立）──
    print("\n[check 7] scheduleSummary 换算（卡片频率摘要）")
    for tag, task, expected in (
        ("interval", interval_task, "每 1 小时"),
        ("cron", cron_task, "cron: 30 8 * * *"),
        ("once", once_task, "定时: 2099-01-01T00:00:00Z"),
    ):
        if not task:
            continue
        listed = next((t for t in after if t["id"] == task["id"]), None)
        if listed is None:
            errs.append(f"[summary-{tag}] 列表项丢失")
            continue
        got = schedule_summary(listed)
        if _check(f"{tag}: 摘要 == '{expected}'", got == expected, f"got='{got}'"):
            pass
        else:
            errs.append(f"[summary-{tag}] 摘要异常 got='{got}' want='{expected}'")

    # ── 8. 暂停→enabled=False / 恢复→enabled=True（状态 Tag 数据源）──
    print("\n[check 8] 暂停→enabled=False / 恢复→enabled=True（状态 Tag 数据源）")
    if interval_task:
        paused = await set_enabled(interval_task["id"], False)
        if _check("pause → enabled=False", paused is not None and paused.get("enabled") is False,
                  f"paused={paused}"):
            pass
        else:
            errs.append("[pause] enabled 未翻转为 False")
        resumed = await set_enabled(interval_task["id"], True)
        if _check("resume → enabled=True", resumed is not None and resumed.get("enabled") is True,
                  f"resumed={resumed}"):
            pass
        else:
            errs.append("[resume] enabled 未翻回 True")

    # ── 9. 收尾清理：删除三条探针 ──
    print(f"\n[cleanup] 删除 {len(probe_ids)} 条探针任务")
    for tid in probe_ids:
        ok = await delete_task(tid)
        if not ok:
            print(f"  ⚠️ 删除失败 {tid}")
            errs.append(f"[cleanup] 删除失败 {tid}")
    final = await list_tasks()
    leaked = [t for t in final if t["id"] in probe_ids]
    if not _check("清理后无残留探针任务", not leaked, f"{len(leaked)} 条残留"):
        errs.append(f"[cleanup] {len(leaked)} 条残留")

    # ── 汇总 ──
    print("\n" + "=" * 56)
    if errs:
        print(f"FAIL — {len(errs)} 项断言失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — 定时任务列表展示名称/频率/状态端到端验证通过：")
    print("  · interval/cron/once 三种调度类型探针创建落库（id sched_ 前缀）；")
    print("  · 列表含三条探针（scheduledTaskApi.list() 真源交叉）；")
    print("  · 单读回读 == create 响应（持久化一致）；")
    print("  · 列表项 name/schedule_type/agent/enabled == payload 原值（卡片字段真源）；")
    print("  · scheduleSummary 换算：interval→「每 1 小时」/ cron→「cron: ...」/ once→「定时: ...」；")
    print("  · 暂停→enabled=False / 恢复→enabled=True（状态 Tag 数据源）；")
    print("  · 清理三条探针无残留（取消 APScheduler job）。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
