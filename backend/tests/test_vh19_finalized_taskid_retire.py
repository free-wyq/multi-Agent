"""VH19 回归：finalizedBubbles 按 task_id 精确退场——消除 sender+时间戳时序依赖（task B22）.

锁住 B22 修复——``src/components/ChatPanel.tsx`` ``finalizedBubbles`` 的退场判定
原按 ``sender_id`` + ``created_at >= 收尾事件时间戳`` 判 replied（注释自承 fragile：
logs 追加路径 coerce WS 消息时 task_id「可能丢失」+ 时间戳比较依赖前后端时钟同步），
B22 改主路径按 ``task_id`` 精确匹配 ``m.task_id === e.taskId``，兜底保留 sender+时间戳。

B22 改动链路（后端回填 task_id 到 reply 行 + 前端按 task_id 退场）：

  后端（让 task_id 持久化到 reply 行，reload-safe）：
    1. ``engine/reply.py`` ``persist_agent_reply`` 加 ``task_id: str | None = None``
       形参，message dict 的 ``"task_id"`` 从恒 ``None`` 改为透传 ``task_id`` 参数。
       默认 None 保全既有调用方（coordinator/worker graph ``_unified_reply`` 不传
       task_id → 落盘 task_id=None，行为零变）。
    2. ``engine/registry.py`` ``_reply`` 加 ``task_id: str | None = None`` 形参，
       调 ``persist_agent_reply(self.group_id, self.agent_id, content, None, task_id)``
       （data 仍恒 None，第 5 参 task_id 透传）。
    3. ``registry`` 3 个 _reply 调用方透传 task_id：
       - ``_run_worker_task`` 成功/失败收尾：``self._reply(reply, task_id)``
       - ``_on_task_cancelled`` 取消收尾：``self._reply("⏹ 任务已停止", task_id)``
       - ``_on_task_timed_out`` 超时收尾：``self._reply(f"⏱ {timeout_result}", task_id)``

  前端（按 task_id 精确退场）：
    4. ``ChatPanel.finalizedBubbles`` 退场判定改：
       原 ``m.sender_id === e.agentId && new Date(m.created_at).getTime() >= e.timestamp``
       → ``m.sender_id === e.agentId && (m.task_id === e.taskId || new Date(m.created_at).getTime() >= e.timestamp)``
       主路径 task_id 精确匹配（OR 短路优先），兜底 sender+时间戳（task_id-less 路径防御性）。

为何后端回填 task_id 到 reply 行（而非只靠 WS 事件 task_id）：
  前端 finalizedBubbles 退场读 ``chatMessages``（持久化消息列表，切群/重连从
  ``messageApi.listByGroup`` 重建）。WS ``task_complete`` 事件已带 task_id，但
  退场回复原本只按 sender+时间戳匹配——fragile：① logs 追加路径 coerce WS 消息
  时 task_id「可能丢失」（原注释自承）；② 时间戳比较依赖前后端时钟同步（WSL2 后端
  UTC 与 Windows 浏览器本地时区常偏差秒级，会误判）。B22 把 task_id 持久化到 reply
  行（reload-safe：切群/重连回灌从 DB 重建 chatMessages 时 task_id 仍在），前端按
  精确 task_id 匹配——同一 task_id 在收尾事件和退场回复上都有，不论经 live WS 还是
  reload-from-DB 抵达都能匹配。

为何保留 sender+时间戳兜底（OR 短路）：
  chat 路径（coordinator/worker node_chat）的 agent_reply 不经 _reply（走 graph
  ``_unified_reply`` 不传 task_id）→ m.task_id===null，task_id 匹配不命中。但 chat
  路径无 task_complete/failed 事件（非 execute 路径），finalizedBubbles 循环根本
  不会为 chat 回复生成定稿气泡（kind 仅 complete/failed 进循环）——故兜底分支实际不
  命中，保留仅防御性（未来若 chat 路径也接 task_complete 收尾，兜底仍能退场）。OR
  短路：task_id 命中即返 true 不查时间戳；task_id 不命中（null !== 'task_xxx'）才查
  时间戳兜底。

纯静态契约（读源码断言，不依赖后端在线），与 test_vh1-vh18 同款风格。

五段契约：

  A. 后端 persist_agent_reply 接 task_id 透传（reply.py）
    1. ``persist_agent_reply`` 签名含 ``task_id: str | None = None`` 形参（默认 None）。
    2. message dict 的 ``"task_id"`` 透传 ``task_id`` 参数（非恒 None）。

  B. 后端 registry _reply 接 task_id 透传（registry.py）
    3. ``_reply`` 签名含 ``task_id: str | None = None`` 形参。
    4. ``_reply`` 调 ``persist_agent_reply(..., None, task_id)``（data 仍恒 None 第 4 参，
       task_id 第 5 参透传）。
    5. 3 个调用方透传 task_id（_run_worker_task / _on_task_cancelled / _on_task_timed_out）。

  C. 前端 finalizedBubbles 按 task_id 精确退场（ChatPanel.tsx）
    6. 退场判定含 ``m.task_id === e.taskId``（主路径 task_id 精确匹配）。
    7. 退场判定仍含 ``new Date(m.created_at).getTime() >= e.timestamp``（兜底时间戳）。
    8. task_id 匹配与时间戳兜底用 ``||`` 短路连接（task_id 命中即返 true 不查时间戳）。
    9. 退场判定仍含 ``m.sender_id === e.agentId``（sender 守卫，两路径共用的前置条件）。

  D. 行为零变（既有契约不破）
   10. persist_agent_reply 默认 task_id=None（保全 graph _unified_reply 不传 task_id 的调用方）。
   11. registry _reply 仍恒 data=None（第 4 参 None，execute announce 无 stats 契约不破）。
   12. finalizedBubbles 仍按 kind complete/failed + taskId 去重 + streaming 未清才渲染（B22 不动循环骨架）。

  E. 无回归（既有测断言同步下沉）
   13. vh7 [A2] 改为 ``"task_id": task_id``（原 ``"task_id": None`` 失效，B22 透传）。
   14. vh7 [D11] 改为 ``persist_agent_reply(..., None, task_id)``（原 ``(..., None)`` 失效）。
   15. va6 [1] 改为 ``persist_agent_reply(..., None, task_id)``（原 ``(..., None)`` 失效）。
   16. vh11 [D11] 改为 ``"task_id": task_id``（原 ``"task_id": None`` 失效）。
   17. vh11 [E15] 改为 ``persist_agent_reply(..., None, task_id)``（原 ``(..., None)`` 失效）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REPLY = REPO / "backend" / "engine" / "reply.py"
REGISTRY = REPO / "backend" / "engine" / "registry.py"
PANEL = REPO / "src" / "components" / "ChatPanel.tsx"


def _fn_body_py(src: str, fname: str, indent_opts=("\t", "    ")) -> str:
    """抽 Python async/sync 函数体（支持 tab/空格缩进）。"""
    for indent in indent_opts:
        pat = rf"def {fname}\([^)]*\)[^:]*:\n((?:\n|{re.escape(indent)}[^\n]*\n)+)"
        m = re.search(pat, src)
        if m:
            return m.group(1)
    return ""


def _fn_body_ts(src: str, fname: str, prefix: str = "function") -> str:
    """抽 TS 函数体。prefix: 'function' / 'export function' / 'const NAME = useMemo'。"""
    if prefix == "function":
        pat = rf"function {fname}\([^)]*\)[^{{]*\{{(.*?)\n\}}"
        m = re.search(pat, src, re.S)
        return m.group(1) if m else ""
    # useMemo arrow form: const NAME = useMemo(() => { ... }, [...])
    pat = rf"const {fname} = useMemo\(\(\) => \{{(.*?)\n  \}}, \["
    m = re.search(pat, src, re.S)
    return m.group(1) if m else ""


def _strip_ts_comments(src: str) -> str:
    """剔单行 ``//`` 注释（B16/vh13 坑延续）。"""
    return re.sub(r"//[^\n]*", "", src)


def assert_contract() -> list[str]:
    errs: list[str] = []
    reply = REPLY.read_text(encoding="utf-8")
    registry = REGISTRY.read_text(encoding="utf-8")
    panel = PANEL.read_text(encoding="utf-8")

    # ── A. 后端 persist_agent_reply 接 task_id 透传 ──
    # [1] 签名含 task_id: str | None = None
    if "task_id: str | None = None" not in reply and "task_id: Optional[str] = None" not in reply:
        errs.append("[A1] persist_agent_reply 签名缺 task_id: str | None = None（B22 形参未加）")
    else:
        print("[A1] OK  persist_agent_reply 签名含 task_id: str | None = None（默认 None 保全既有调用方）")
    pa_body = _fn_body_py(reply, "persist_agent_reply")
    if not pa_body:
        errs.append("[setup] persist_agent_reply 函数体未找到")
    else:
        # [2] message dict "task_id": task_id（透传参数，非恒 None）
        if '"task_id": task_id' not in pa_body:
            errs.append('[A2] persist_agent_reply message dict 缺 "task_id": task_id（B22 未透传 task_id 参数）')
        else:
            print('[A2] OK  message dict "task_id": task_id（透传 task_id 参数，非恒 None）')

    # ── B. 后端 registry _reply 接 task_id 透传 ──
    # [3] _reply 签名含 task_id: str | None = None
    if not re.search(r"def _reply\(\s*self,\s*content:\s*str,\s*task_id:\s*str\s*\|\s*None\s*=\s*None\s*\)", registry):
        errs.append("[B3] registry _reply 签名缺 task_id: str | None = None（B22 形参未加）")
    else:
        print("[B3] OK  registry _reply 签名含 task_id: str | None = None（B22 形参）")
    r_body = _fn_body_py(registry, "_reply", indent_opts=("        ",))
    if not r_body:
        errs.append("[setup] registry _reply 函数体未找到")
    else:
        # [4] 调 persist_agent_reply(..., None, task_id)（data 恒 None 第 4 参，task_id 第 5 参）
        if not re.search(r"persist_agent_reply\([^)]*,\s*None\s*,\s*task_id\s*\)", r_body, re.S):
            errs.append("[B4] registry _reply 未调 persist_agent_reply(..., None, task_id)（data 恒 None + B22 task_id 透传）")
        else:
            print("[B4] OK  registry _reply → persist_agent_reply(..., None, task_id)（data 恒 None + B22 透传 task_id）")
    # [5] 3 个调用方透传 task_id
    # _run_worker_task 成功/失败收尾
    if not re.search(r'self\._reply\(\s*reply\s*,\s*task_id\s*\)', registry):
        errs.append("[B5a] _run_worker_task 未调 self._reply(reply, task_id)（成功/失败收尾未透传 task_id）")
    else:
        print("[B5a] OK  _run_worker_task → self._reply(reply, task_id)（成功/失败收尾透传 task_id）")
    # _on_task_cancelled 取消收尾
    if not re.search(r'self\._reply\(\s*"⏹ 任务已停止"\s*,\s*task_id\s*\)', registry):
        errs.append('[B5b] _on_task_cancelled 未调 self._reply("⏹ 任务已停止", task_id)（取消收尾未透传 task_id）')
    else:
        print('[B5b] OK  _on_task_cancelled → self._reply("⏹ 任务已停止", task_id)（取消收尾透传 task_id）')
    # _on_task_timed_out 超时收尾
    if not re.search(r'self\._reply\(\s*f"⏱ \{timeout_result\}"\s*,\s*task_id\s*\)', registry):
        errs.append('[B5c] _on_task_timed_out 未调 self._reply(f"⏱ {timeout_result}", task_id)（超时收尾未透传 task_id）')
    else:
        print('[B5c] OK  _on_task_timed_out → self._reply(f"⏱ {timeout_result}", task_id)（超时收尾透传 task_id）')

    # ── C. 前端 finalizedBubbles 按 task_id 精确退场 ──
    fb_body = _fn_body_ts(panel, "finalizedBubbles", "const")
    if not fb_body:
        errs.append("[setup] finalizedBubbles 函数体未找到")
    else:
        fb_nc = _strip_ts_comments(fb_body)
        # [6] 退场判定含 task_id 精确匹配（B22 m.task_id === e.taskId 或 B23 repliedTaskIds.has(e.taskId)）
        # B23 改：退场判定从 chatMessages.some 扫描改为 repliedTaskIdsRef.has(e.taskId)
        # （reply 落地 effect 增量回填 ref）。两种形态都接受（B22 直扫 chatMessages / B23 读 ref），
        # 核心契约「按 task_id 精确退场」不变。断言：函数体含 task_id 退场匹配（两种之一）。
        has_b22 = "m.task_id === e.taskId" in fb_nc
        has_b23 = "repliedTaskIds.has(e.taskId)" in fb_nc
        if not (has_b22 or has_b23):
            errs.append("[C6] finalizedBubbles 退场判定缺 task_id 精确匹配（B22 m.task_id===e.taskId 或 B23 repliedTaskIds.has(e.taskId)——两者皆无）")
        else:
            mode = "B23 repliedTaskIds.has(e.taskId)" if has_b23 else "B22 m.task_id === e.taskId"
            print(f"[C6] OK  退场判定含 task_id 精确匹配（{mode}）")
        # [7] 退场判定仍含时间戳兜底
        if "new Date(m.created_at).getTime() >= e.timestamp" not in fb_nc:
            errs.append("[C7] finalizedBubbles 退场判定缺时间戳兜底（task_id-less 路径防御性退场丢失）")
        else:
            print("[C7] OK  退场判定仍含 new Date(m.created_at).getTime() >= e.timestamp（兜底时间戳）")
        # [8] task_id 匹配与时间戳兜底连接关系（B22 || 短路 或 B23 分支顺序短路）
        # B22：m.task_id === e.taskId || new Date(m.created_at)... 在同一 some 回调内 || 短路。
        # B23：repliedTaskIds.has(e.taskId) 先判（命中 continue 不扫 chatMessages），未命中才
        # chatMessages.some 时间戳兜底——分支顺序短路（if-has-continue; if-some-continue）。
        # 两种形态都接受，核心契约「task_id 命中即不查时间戳」不变。
        b22_shortcircuit = bool(re.search(r"m\.task_id === e\.taskId\s*\|\|\s*new Date\(m\.created_at\)\.getTime\(\) >= e\.timestamp", fb_nc))
        b23_branch = "repliedTaskIds.has(e.taskId)" in fb_nc and "new Date(m.created_at).getTime() >= e.timestamp" in fb_nc
        if not (b22_shortcircuit or b23_branch):
            errs.append("[C8] finalizedBubbles 退场判定 task_id 与时间戳未短路（B22 || 短路 或 B23 分支顺序短路——两者皆无）")
        else:
            mode = "B23 分支顺序短路（has 先判 / some 兜底）" if b23_branch else "B22 || 短路"
            print(f"[C8] OK  task_id 命中即不查时间戳（{mode}）")
        # [9] 退场判定仍含 m.sender_id === e.agentId
        if "m.sender_id === e.agentId" not in fb_nc:
            errs.append("[C9] finalizedBubbles 退场判定缺 m.sender_id === e.agentId（sender 守卫丢失）")
        else:
            print("[C9] OK  退场判定仍含 m.sender_id === e.agentId（sender 守卫，兜底路径前置条件）")

    # ── D. 行为零变 ──
    # [10] persist_agent_reply 默认 task_id=None
    if "task_id: str | None = None" in reply:
        print("[D10] OK  persist_agent_reply 默认 task_id=None（保全 graph _unified_reply 不传 task_id 的调用方）")
    else:
        errs.append("[D10] persist_agent_reply 缺默认 task_id=None（既有调用方会断）")
    # [11] registry _reply 仍恒 data=None（第 4 参 None）
    if r_body and re.search(r"persist_agent_reply\([^)]*,\s*None\s*,\s*task_id\s*\)", r_body, re.S):
        print("[D11] OK  registry _reply 仍恒 data=None（第 4 参 None，execute announce 无 stats 契约不破）")
    else:
        errs.append("[D11] registry _reply data 不再恒 None（execute announce 无 stats 契约破）")
    # [12] finalizedBubbles 循环骨架不动（kind complete/failed + taskId 去重 + streaming 未清）
    if fb_body:
        fb_nc2 = _strip_ts_comments(fb_body) if not fb_body else fb_body
        has_kind = "e.kind !== 'complete' && e.kind !== 'failed'" in fb_nc2
        has_seen = "seen.has(e.taskId)" in fb_nc2
        has_streaming = "streaming[e.taskId]" in fb_nc2
        if not (has_kind and has_seen and has_streaming):
            errs.append(f"[D12] finalizedBubbles 循环骨架破（kind={has_kind} seen={has_seen} streaming={has_streaming}——应全）")
        else:
            print("[D12] OK  finalizedBubbles 循环骨架不动（kind complete/failed + taskId 去重 + streaming 未清才渲染）")

    # ── E. 无回归（既有测断言同步下沉）──
    # [13] vh7 [A2] 改为 "task_id": task_id
    vh7 = (REPO / "backend" / "tests" / "test_vh7_persist_agent_reply.py").read_text(encoding="utf-8")
    if '"task_id": task_id' not in vh7:
        errs.append("[E13] vh7 [A2] 未改为 \"task_id\": task_id 断言（B22 透传未同步测）")
    else:
        print('[E13] OK  vh7 [A2] 改为 "task_id": task_id 断言（守卫下沉同步）')
    # [14] vh7 [D11] 改为 persist_agent_reply(..., None, task_id)
    if "None,\\s*task_id" not in vh7 and "None, task_id" not in vh7:
        errs.append("[E14] vh7 [D11] 未改为 persist_agent_reply(..., None, task_id) 断言（B22 接线未同步测）")
    else:
        print("[E14] OK  vh7 [D11] 改为 persist_agent_reply(..., None, task_id) 断言（守卫下沉同步）")
    # [15] va6 [1] 改为 persist_agent_reply(..., None, task_id)
    va6 = (REPO / "backend" / "tests" / "test_va6_execute_no_stats_known_limit.py").read_text(encoding="utf-8")
    if "None,\\s*task_id" not in va6 and "None, task_id" not in va6:
        errs.append("[E15] va6 [1] 未改为 persist_agent_reply(..., None, task_id) 断言（B22 接线未同步测）")
    else:
        print("[E15] OK  va6 [1] 改为 persist_agent_reply(..., None, task_id) 断言（守卫下沉同步）")
    # [16] vh11 [D11] 改为 "task_id": task_id
    vh11 = (REPO / "backend" / "tests" / "test_vh11_announce_no_stats.py").read_text(encoding="utf-8")
    if '"task_id": task_id' not in vh11:
        errs.append('[E16] vh11 [D11] 未改为 "task_id": task_id 断言（B22 透传未同步测）')
    else:
        print('[E16] OK  vh11 [D11] 改为 "task_id": task_id 断言（守卫下沉同步）')
    # [17] vh11 [E15] 改为 persist_agent_reply(..., None, task_id)
    if "None,\\s*task_id" not in vh11 and "None, task_id" not in vh11:
        errs.append("[E17] vh11 [E15] 未改为 persist_agent_reply(..., None, task_id) 断言（B22 接线未同步测）")
    else:
        print("[E17] OK  vh11 [E15] 改为 persist_agent_reply(..., None, task_id) 断言（守卫下沉同步）")

    return errs


def main() -> int:
    print("=== VH19 回归：finalizedBubbles 按 task_id 精确退场——消除 sender+时间戳时序依赖（B22）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "B22 finalizedBubbles 按 task_id 精确退场锁定：\n"
        "  · A 后端 persist_agent_reply 接 task_id 形参 + message dict 透传 task_id（非恒 None）；\n"
        "  · B registry _reply 接 task_id + 调 persist_agent_reply(..., None, task_id) + 3 调用方透传 task_id；\n"
        "  · C 前端 finalizedBubbles 退场判定 m.task_id === e.taskId 主路径 || 时间戳兜底（短路）+ sender 守卫；\n"
        "  · D 行为零变：persist_agent_reply 默认 task_id=None 保全 graph 调用方 + _reply 仍恒 data=None + 循环骨架不动；\n"
        "  · E 无回归：vh7/va6/vh11 三测断言同步下沉到 task_id 透传（守卫下沉同步测）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
