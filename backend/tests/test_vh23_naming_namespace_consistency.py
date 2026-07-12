"""VH23 回归：全仓命名一致性审计锁契约（task B26）.

锁住 B26 审计——「两套身份分类轴」(graph_kind vs single_chat) 与「三套 id 命名空间」
(reply_id / task_id / thread_id) 的形状/作用域/复用规则 + 文档化消歧义（不改语义）.

B26 审计结论（两轴非平行是输入→派生；三套 id 有意跨命名空间复用 + 前缀判别）：

  ── 两套身份分类轴（输入→派生，非平行） ──
    single_chat（输入·群级配置 bool）→ graph_kind（派生·编译哪张图 str）.
    非两个可独立翻转的维度。single_chat=True 把 is_coordinator=True 的 agent 降级
    成 worker 图（单聊=退化的多智能体，supervisor 只在多 agent 里存在）.

    派生真值表（registry.py:126）：
      is_coordinator=True  + single_chat=False → graph_kind="coordinator"
      is_coordinator=True  + single_chat=True  → graph_kind="worker"  （降级）
      is_coordinator=False + single_chat=*     → graph_kind="worker"

    两轴各有读处（勿混「输入」与「派生」）：
      single_chat 读处：选图公式 / sys_for_invoke 守卫 / ensure_engine 取群配置 / 前端 4 处
      graph_kind 读处：_handle_task 看门狗 / _execute_body 分流 / _handle_notify coord 分支 /
                      reset_session aupdate_state(END)

  ── 三套 id 命名空间（形状/作用域/复用规则各异，有意的跨命名空间复用） ──
    task_id：`task_`+uuid hex（crud._next_id("task")，_PREFIX_MAP["task"]="task_"）.
              DAG 任务身份. 落 TaskEntity.id / MessageEntity.task_id / 6 类 task_* WS 事件.
              有意复用作 thread_id（agent_loop.py:257 thread_id=task_id or uuid4）→ task-scoped 检查点.
    reply_id：裸 uuid.uuid4().hex（**无 task_ 前缀**——判别特征）. 单轮流式归并键.
              2 处生成（coordinator._stream_coordinator_decision:1348 + worker._stream_brain_decision:161）.
              落 coordinator_token/reasoning/stats 的 data.reply_id + agent_reply.data["reply_id"].
              有意塞进 task_token 事件的 task_id 槽——前端靠 `task_` 前缀判别分流.
    thread_id：两型. ① 驻留引擎图 f"{group_id}:{agent_id}"（registry.py:132 稳定键）;
              ② create_react_agent task_id-or-uuid（agent_loop.py:257 per-exec 键）.
              LangGraph MemorySaver 检查点键，跨 invoke 持久化图状态.

  ── 跨命名空间判别速查 ──
    真 task 流式 vs worker 单聊 reply_id：WS 事件 task_id 字段有无 `task_` 前缀（useBusEvent.ts:430）
    驻留图检查点 vs 执行检查点：thread_id 是 `{group}:{agent}`（稳定）还是 `task_*`/uuid（per-exec）
    agent_reply 关闭哪个 task：agent_reply.task_id（B22 回填 exact 匹配）；无 task_id 回落 sender+timestamp

B26 只文档化 + 加交叉引用注释（registry 选图分支 / agent_loop thread_id 赋值 / worker reply_id
生成 / coordinator reply_id 生成 / useBusEvent 前缀判别 / docs/naming-conventions.md），不动运行时语义.
纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh22 同款风格.

六段契约：

  A. graph_kind 派生公式（输入→派生，非两套平行分类）
    1. 选图分支 ``if self.is_coordinator and not self.single_chat:`` 仍在（派生公式）.
    2. graph_kind 取值仅 "coordinator" / "worker"（无第三值，无拼写漂移）.
    3. graph_kind 注释含「输入→派生」口径说明（B26 加的交叉引用，指向 docs/naming-conventions.md §1）.
    4. single_chat 是选图公式的**输入项**（`not self.single_chat`），非 graph_kind 平行兄弟.

  B. 两轴各有读处（勿混输入与派生）
    5. single_chat 读 sys_for_invoke 守卫（`if not self.single_chat:` 加 TEAM_INTERACTION_SUFFIX）.
    6. single_chat 读 ensure_engine 取群配置（`bool((g.config or {}).get("single_chat"))`）.
    7. graph_kind 读 _handle_task 看门狗（`if self.graph_kind == "worker"` 装 MT-17 看门狗）.
    8. graph_kind 读 reset_session aupdate_state（`if self.graph_kind == "coordinator"` 清 interrupt）.

  C. task_id 命名空间（形状 + 前缀 + 复用）
    9. _PREFIX_MAP 含 ``"task": "task_"``（task_id 恒有 task_ 前缀）.
   10. create_task 用 _next_id("task")（task_id 生成唯一入口）.
   11. agent_loop.py ``thread_id = task_id or str(uuid4())``（task_id 复用作 thread_id，task-scoped 检查点）.

  D. reply_id 命名空间（裸 hex 无前缀 + 2 处生成 + 塞进 task_id 槽）
   12. coordinator.py:1348 ``reply_id = uuid.uuid4().hex``（协调者流式归并键生成）.
   13. worker.py:161 ``reply_id = uuid.uuid4().hex``（单聊 worker 流式归并键生成，同构）.
   14. reply_id 落 agent_reply.data["reply_id"]（定稿气泡退场后仍可按 reply_id 找回流式统计）.
   15. emit_coordinator_token/reasoning/stats 三处的 data 含 ``"reply_id": reply_id``.

  E. thread_id 命名空间（两型 + 不碰撞）
   16. registry.py ``self.thread_id = f"{group_id}:{self.agent_id}"``（驻留图稳定键）.
   17. agent_loop.py thread_id 两型共存（稳定键 vs task_id-or-uuid per-exec 键）.

  F. 跨命名空间判别（前端前缀分流 + 后端 exact 匹配）
   18. useBusEvent.ts ``key.startsWith('task_')`` 判真 task 流式 vs worker 单聊 reply_id.
   19. agent_reply.task_id exact 匹配 task_complete/failed（B22 回填，reload-safe）.
   20. docs/naming-conventions.md 存在（B26 单一真源文档，含两轴 + 三 id + 判别速查）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REGISTRY_PY = REPO / "backend" / "engine" / "registry.py"
COORD_PY = REPO / "backend" / "engine" / "coordinator.py"
WORKER_PY = REPO / "backend" / "engine" / "worker.py"
AGENT_LOOP_PY = REPO / "backend" / "engine" / "agent_loop.py"
REPLY_PY = REPO / "backend" / "engine" / "reply.py"
CRUD_PY = REPO / "backend" / "store" / "crud.py"
BUS_PY = REPO / "backend" / "events" / "bus.py"
USEBUSEVENT_TS = REPO / "src" / "hooks" / "useBusEvent.ts"
NAMING_DOC = REPO / "docs" / "naming-conventions.md"


def _fn_body_py(src: str, fname: str, is_async: bool = False) -> str:
    """抽 Python 函数体（到下一个顶层 def 为止）。"""
    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


def assert_contract() -> list[str]:
    errs: list[str] = []
    registry = REGISTRY_PY.read_text(encoding="utf-8")
    coord = COORD_PY.read_text(encoding="utf-8")
    worker = WORKER_PY.read_text(encoding="utf-8")
    agent_loop = AGENT_LOOP_PY.read_text(encoding="utf-8")
    crud = CRUD_PY.read_text(encoding="utf-8")
    bus = BUS_PY.read_text(encoding="utf-8")
    usebus = USEBUSEVENT_TS.read_text(encoding="utf-8")

    # ── A. graph_kind 派生公式 ──
    init_body = _fn_body_py(registry, "__init__")
    if not init_body:
        errs.append("[setup] AgentEngine.__init__ 函数体未找到")
    else:
        # [1] 选图分支 if is_coordinator and not single_chat
        if not re.search(r"if\s+self\.is_coordinator\s+and\s+not\s+self\.single_chat\s*:", init_body):
            errs.append("[A1] 缺选图分支 `if self.is_coordinator and not self.single_chat:`（派生公式破）")
        else:
            print("[A1] OK  选图分支 `if is_coordinator and not single_chat:` 在（输入→派生公式）")
        # [2] graph_kind 取值仅 coordinator/worker
        gk_values = set(re.findall(r'self\.graph_kind\s*(:\s*str\s*)?=\s*["\'](\w+)["\']', init_body))
        gk_strs = {v for _, v in gk_values} if gk_values else set()
        # 退化兼容：直接抓字符串字面量
        if not gk_strs:
            gk_strs = set(re.findall(r'graph_kind[^=]*=\s*["\'](\w+)["\']', init_body))
        if gk_strs != {"coordinator", "worker"}:
            errs.append(f"[A2] graph_kind 取值 {{coordinator,worker}} 漂移：{gk_strs}")
        else:
            print("[A2] OK  graph_kind 取值仅 coordinator/worker（无第三值无拼写漂移）")
        # [3] graph_kind 注释含输入→派生口径说明（指向 docs/naming-conventions.md §1）
        # B26 加的交叉引用注释在选图分支上方
        if "naming-conventions.md" not in init_body or "输入" not in init_body or "派生" not in init_body:
            errs.append("[A3] graph_kind 选图分支缺「输入→派生」口径注释（B26 交叉引用缺失）")
        else:
            print("[A3] OK  graph_kind 注释含「输入→派生」口径（指向 naming-conventions.md §1）")
        # [4] single_chat 是选图公式的输入项（not self.single_chat）
        if "not self.single_chat" not in init_body:
            errs.append("[A4] 选图公式缺 `not self.single_chat`（single_chat 非输入项）")
        else:
            print("[A4] OK  single_chat 是选图公式输入项（`not self.single_chat`，非 graph_kind 平行兄弟）")

    # ── B. 两轴各有读处 ──
    # [5] single_chat 读 sys_for_invoke 守卫
    notify_body = _fn_body_py(registry, "_handle_notify", is_async=True)
    if not notify_body:
        errs.append("[setup] _handle_notify 函数体未找到")
    elif "if not self.single_chat:" not in notify_body:
        errs.append("[B5] _handle_notify 缺 `if not self.single_chat:` 守卫（sys_for_invoke 不加互动语义）")
    else:
        print("[B5] OK  single_chat 读 sys_for_invoke 守卫（`if not self.single_chat:` 加 TEAM_INTERACTION_SUFFIX）")
    # [6] single_chat 读 ensure_engine 取群配置
    if not re.search(r'bool\(\(g\.config\s+or\s+\{\}\)\.get\(["\']single_chat["\']\)\)', registry):
        errs.append("[B6] ensure_engine 缺 `(g.config or {}).get('single_chat')`（single_chat 未从群配置取）")
    else:
        print("[B6] OK  single_chat 读 ensure_engine 取群配置（bool((g.config or {}).get('single_chat'))）")
    # [7] graph_kind 读 _handle_task 看门狗
    if 'if self.graph_kind == "worker"' not in registry:
        errs.append("[B7] 缺 `if self.graph_kind == 'worker'`（看门狗未按 graph_kind 分流）")
    else:
        print("[B7] OK  graph_kind 读 _handle_task 看门狗（`if graph_kind == 'worker'` 装 MT-17）")
    # [8] graph_kind 读 reset_session aupdate_state
    if 'if self.graph_kind == "coordinator"' not in registry:
        errs.append("[B8] 缺 `if self.graph_kind == 'coordinator'`（reset_session 未按 graph_kind 清 interrupt）")
    else:
        print("[B8] OK  graph_kind 读 reset_session aupdate_state（`if graph_kind == 'coordinator'` 清 interrupt）")

    # ── C. task_id 命名空间 ──
    # [9] _PREFIX_MAP 含 "task": "task_"
    if not re.search(r'["\']task["\']\s*:\s*["\']task_["\']', crud):
        errs.append("[C9] _PREFIX_MAP 缺 'task': 'task_'（task_id 无 task_ 前缀）")
    else:
        print("[C9] OK  _PREFIX_MAP 含 'task': 'task_'（task_id 恒有 task_ 前缀）")
    # [10] create_task 用 _next_id("task")
    create_task_body = _fn_body_py(crud, "create_task", is_async=True)
    if not create_task_body:
        errs.append("[setup] create_task 函数体未找到")
    elif '_next_id("task")' not in create_task_body:
        errs.append("[C10] create_task 未用 _next_id('task')（task_id 生成非唯一入口）")
    else:
        print("[C10] OK  create_task 用 _next_id('task')（task_id 生成唯一入口）")
    # [11] agent_loop thread_id = task_id or str(uuid4())
    if not re.search(r'thread_id\s*=\s*task_id\s+or\s+str\(uuid4\(\)\)', agent_loop):
        errs.append("[C11] agent_loop 缺 `thread_id = task_id or str(uuid4())`（task_id 未复用作 thread_id）")
    else:
        print("[C11] OK  agent_loop `thread_id = task_id or str(uuid4())`（task_id 复用作 thread_id，task-scoped 检查点）")

    # ── D. reply_id 命名空间 ──
    # [12] coordinator.py:1348 reply_id = uuid.uuid4().hex
    scd_body = _fn_body_py(coord, "_stream_coordinator_decision", is_async=True)
    if not scd_body:
        errs.append("[setup] _stream_coordinator_decision 函数体未找到")
    elif "reply_id = uuid.uuid4().hex" not in scd_body:
        errs.append("[D12] _stream_coordinator_decision 缺 `reply_id = uuid.uuid4().hex`（协调者流式归并键破）")
    else:
        print("[D12] OK  coordinator _stream_coordinator_decision reply_id = uuid.uuid4().hex（协调者流式归并键）")
    # [13] worker.py:161 reply_id = uuid.uuid4().hex
    sbd_body = _fn_body_py(worker, "_stream_brain_decision", is_async=True)
    if not sbd_body:
        errs.append("[setup] _stream_brain_decision 函数体未找到")
    elif "reply_id = uuid.uuid4().hex" not in sbd_body:
        errs.append("[D13] _stream_brain_decision 缺 `reply_id = uuid.uuid4().hex`（单聊 worker 流式归并键破）")
    else:
        print("[D13] OK  worker _stream_brain_decision reply_id = uuid.uuid4().hex（单聊 worker 流式归并键同构）")
    # [14] reply_id 落 agent_reply.data["reply_id"]
    # persist_agent_reply 落 data（reply_id 由调用方塞进 data dict）——确认 reply.py 提到 reply_id
    reply_mod = REPLY_PY.read_text(encoding="utf-8")
    if "reply_id" not in reply_mod:
        errs.append("[D14] reply.py 未提及 reply_id（agent_reply.data['reply_id'] 退场后找回流式统计断）")
    else:
        print("[D14] OK  reply.py 提及 reply_id（落 agent_reply.data['reply_id']，定稿气泡退场后可找回统计）")
    # [15] emit_coordinator_token/reasoning/stats 三处 data 含 "reply_id": reply_id
    tok_body = _fn_body_py(bus, "emit_coordinator_token", is_async=True)
    rea_body = _fn_body_py(bus, "emit_coordinator_reasoning", is_async=True)
    sta_body = _fn_body_py(bus, "emit_coordinator_stats", is_async=True)
    miss = []
    if not tok_body or '"reply_id": reply_id' not in tok_body:
        miss.append("token")
    if not rea_body or '"reply_id": reply_id' not in rea_body:
        miss.append("reasoning")
    if not sta_body or '"reply_id": reply_id' not in sta_body:
        miss.append("stats")
    if miss:
        errs.append(f"[D15] emit_coordinator_{'/'.join(miss)} 缺 `\"reply_id\": reply_id`（流式归并键未落 data）")
    else:
        print("[D15] OK  emit_coordinator_token/reasoning/stats 三处 data 含 'reply_id': reply_id")

    # ── E. thread_id 命名空间 ──
    # [16] registry self.thread_id = f"{group_id}:{self.agent_id}"
    if not re.search(r'self\.thread_id\s*=\s*f"\{group_id\}:\{self\.agent_id\}"', registry):
        errs.append("[E16] registry 缺 `self.thread_id = f'{group_id}:{self.agent_id}'`（驻留图稳定键破）")
    else:
        print("[E16] OK  registry `self.thread_id = f'{group_id}:{self.agent_id}'`（驻留图稳定键）")
    # [17] agent_loop thread_id 两型共存
    if "thread_id = task_id or str(uuid4())" not in agent_loop:
        errs.append("[E17] agent_loop 缺 per-exec thread_id（两型 thread_id 缺一）")
    else:
        print("[E17] OK  agent_loop per-exec thread_id（与 registry 稳定键两型共存，不碰撞）")

    # ── F. 跨命名空间判别 ──
    # [18] useBusEvent.ts key.startsWith('task_')
    if "startsWith('task_')" not in usebus:
        errs.append("[F18] useBusEvent.ts 缺 `startsWith('task_')`（真 task vs worker 单聊 reply_id 判别破）")
    else:
        print("[F18] OK  useBusEvent.ts `startsWith('task_')` 判真 task 流式 vs worker 单聊 reply_id")
    # [19] agent_reply.task_id exact 匹配（B22 回填，reply.py persist_agent_reply 接受 task_id 形参）
    if "task_id: str | None = None" not in reply_mod:
        errs.append("[F19] persist_agent_reply 缺 `task_id: str | None = None` 形参（B22 exact 匹配回填破）")
    else:
        print("[F19] OK  persist_agent_reply 接受 task_id 形参（B22 exact 匹配，reload-safe）")
    # [20] docs/naming-conventions.md 存在 + 含两轴 + 三 id + 判别速查
    if not NAMING_DOC.exists():
        errs.append("[F20] docs/naming-conventions.md 缺失（B26 单一真源文档未创建）")
    else:
        doc = NAMING_DOC.read_text(encoding="utf-8")
        checks = {
            "两套身份分类轴": "两套身份分类轴" in doc or "身份分类轴" in doc,
            "三套 id 命名空间": "三套 id 命名空间" in doc or "id 命名空间" in doc,
            "graph_kind 派生真值表": "graph_kind" in doc and "真值表" in doc,
            "task_id 形状 task_ 前缀": "task_" in doc and "前缀" in doc,
            "reply_id 裸 hex": "reply_id" in doc and "裸" in doc,
            "thread_id 两型": "thread_id" in doc and "两型" in doc,
            "判别速查": "判别" in doc,
        }
        missing = [k for k, v in checks.items() if not v]
        if missing:
            errs.append(f"[F20] naming-conventions.md 缺章节：{missing}")
        else:
            print("[F20] OK  docs/naming-conventions.md 存在（两轴 + 三 id + 真值表 + 判别速查齐全）")

    return errs


def main() -> int:
    print("=== VH23 回归：全仓命名一致性审计锁契约（B26）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B26 全仓命名一致性审计锁定：\n"
        "  · A graph_kind 派生公式（single_chat 输入 → graph_kind 派生，非两套平行分类）；\n"
        "  · B 两轴各有读处（single_chat 读选图/sys_for_invoke/ensure_engine；graph_kind 读看门狗/分流/reset）；\n"
        "  · C task_id 命名空间（task_+hex 前缀 + _next_id 唯一入口 + 复用作 thread_id）；\n"
        "  · D reply_id 命名空间（裸 hex 无前缀 + 2 处生成 + 落 data + 三处 emit）；\n"
        "  · E thread_id 命名空间（驻留图稳定键 + per-exec 键两型共存）；\n"
        "  · F 跨命名空间判别（前端 task_ 前缀分流 + 后端 task_id exact 匹配 + docs 真源文档）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
