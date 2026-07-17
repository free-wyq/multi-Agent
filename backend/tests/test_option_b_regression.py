"""Option B 删除回归契约——把 Option B 批次删除固化成静态源码字符串断言.

Option B 决策：删停关键词入站 + 协作式软停层（request_stop / is_stopped /
reset_stop / _stop_event）+ 节点入口 is_stopped 守卫，停止只留两入口：
  · UI 终止按钮 ``cancel_turn``（硬切 ``task.cancel`` mid-stream 断流）；
  · 系统内置 50 封顶（``SESSION_SPEECH_CAP`` 跨回合兜底）。

@收束（``converge``）是去中心化专属的另一个独立入口——不是停止，是新回合收尾，
详见 memory ``converge-turn-design`` + test ``test_vh54_converge_turn``。

本测试锁「删除已彻底」——纯静态源码字符串断言，不依赖 live server / 真实 LLM：

  a. ``mention.py`` 源码无 ``_STOP_PHRASES`` / ``_is_stop_phrase``（停关键词入站已删，①）；
  b. ``group_runtime.py`` 源码无 ``def request_stop`` / ``def is_stopped`` /
     ``def reset_stop`` / ``self._stop_event``（软停三件 + event 字段已删，③）；
  c. ``group_graph.py`` ``route_entry`` standalone + closure-bound twin 体内
     无 ``is_stopped()`` 调用（节点入口守卫已删，②）；
  d. ``worker.py`` ``make_agent_node`` 体内无 ``is_stopped()`` 调用（同②）；
  e. ``cancel_turn`` 体内无 ``_stop_event.set()``，只有 ``task.cancel()``（纯硬切，③）。

注：group_runtime.py docstring 里仍以反引号 `` ``request_stop`` `` 形式提及已删件
（说明性引用，非代码）——本测试断言钉在「函数定义」与「属性赋值」上，不误伤 docstring。

设计真源见 memory ``converge-turn-design`` + ``stop-signal-cooperative-cancel-design``
（Option B 删软停层）+ ``session-speech-cap-backstop``（两层停止）.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
MENTION_PY = BACKEND / "engine" / "mention.py"
GROUP_RUNTIME_PY = BACKEND / "engine" / "group_runtime.py"
GROUP_GRAPH_PY = BACKEND / "engine" / "group_graph.py"
WORKER_PY = BACKEND / "engine" / "worker.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_body(src: str, fn_name: str) -> str:
    """Return one method/function body (def ... to next def at column 0).

    Only breaks on a *top-level* (column-0) ``def``/``async def`` so a nested
    closure function (e.g. ``build_route_entry``'s inner ``_route_entry``) is
    INCLUDED in the body, not treated as the end of the outer function.
    """
    idx = src.find(f"async def {fn_name}(")
    if idx < 0:
        idx = src.find(f"def {fn_name}(")
    if idx < 0:
        return ""
    rest = src[idx:]
    lines = rest.splitlines()
    body_lines = [lines[0]]
    for ln in lines[1:]:
        # break ONLY on a column-0 def (next top-level function); indented nested
        # defs (closures) are part of this body.
        if ln.startswith("def ") or ln.startswith("async def "):
            break
        body_lines.append(ln)
    return "\n".join(body_lines)


def _strip_comments_and_strings(src: str) -> str:
    """Strip ``#`` comments and triple/single-quoted strings → leave only code.

    Lets us assert ``is_stopped()`` / ``_stop_event.set()`` don't appear as
    actual code, while tolerating them as prose in docstrings/comments.
    Triple-quoted blocks (docstrings) and ``#`` line comments are removed.
    """
    # remove triple-quoted blocks (greedy across docstrings)
    no_doc = re.sub(r'"""[\s\S]*?"""', "", src)
    no_doc = re.sub(r"'''[\s\S]*?'''", "", no_doc)
    out_lines = []
    for ln in no_doc.splitlines():
        # drop everything from a '#' to EOL (but only when # is not inside a
        # string literal — simple heuristic good enough for these assertions)
        hash_idx = ln.find("#")
        if hash_idx >= 0:
            ln = ln[:hash_idx]
        out_lines.append(ln)
    return "\n".join(out_lines)


def assert_contract() -> list[str]:
    errs: list[str] = []

    mention_src = _read(MENTION_PY)
    gr_src = _read(GROUP_RUNTIME_PY)
    gg_src = _read(GROUP_GRAPH_PY)
    worker_src = _read(WORKER_PY)

    # ── a. mention.py 停关键词入站已删（Option B·①）─────────────────────
    if "_STOP_PHRASES" in mention_src:
        errs.append("[a] mention.py 仍含 `_STOP_PHRASES`（Option B·① 应删）")
    else:
        print("[a] OK  mention.py 无 `_STOP_PHRASES`（停关键词集合已删）")

    if "_is_stop_phrase" in mention_src:
        errs.append("[a] mention.py 仍含 `_is_stop_phrase`（Option B·① 应删）")
    else:
        print("[a] OK  mention.py 无 `_is_stop_phrase`（停关键词判定函数已删）")

    # ── b. group_runtime.py 软停三件 + event 字段已删（Option B·③）─────
    # 钉在「函数定义」上（`def request_stop(`），不误伤 docstring 反引号提及
    for defn in ("def request_stop(", "def is_stopped(", "def reset_stop("):
        if defn in gr_src:
            errs.append(f"[b] group_runtime.py 仍定义 `{defn}`（Option B·③ 应删）")
        else:
            print(f"[b] OK  group_runtime.py 无 `{defn}` 定义（软停件已删）")

    # self._stop_event（属性赋值/读取）全仓零命中
    gr_code = _strip_comments_and_strings(gr_src)
    if "_stop_event" in gr_code:
        errs.append("[b] group_runtime.py 代码（去注释/字符串后）仍含 `_stop_event`（应删）")
    else:
        print("[b] OK  group_runtime.py 代码层无 `_stop_event`（event 字段已删）")

    # ── c. group_graph.py route_entry standalone + closure twin 无 is_stopped() ─
    route_entry_body = _fn_body(gg_src, "route_entry")
    if not route_entry_body:
        errs.append("[c] group_graph.py 未找到 `route_entry` 函数体")
    elif "is_stopped()" in route_entry_body:
        errs.append("[c] route_entry（standalone）体内仍调 `is_stopped()`（Option B·② 应删）")
    else:
        print("[c] OK  route_entry（standalone）体内无 `is_stopped()` 调用")

    build_route_entry_body = _fn_body(gg_src, "build_route_entry")
    if not build_route_entry_body:
        errs.append("[c] group_graph.py 未找到 `build_route_entry` 函数体")
    elif "is_stopped()" in build_route_entry_body:
        errs.append("[c] build_route_entry（closure twin）体内仍调 `is_stopped()`（Option B·② 应删）")
    else:
        print("[c] OK  build_route_entry（closure twin）体内无 `is_stopped()` 调用")

    # ── d. worker.py make_agent_node 无 is_stopped()（Option B·②）─────────
    make_agent_node_body = _fn_body(worker_src, "make_agent_node")
    if not make_agent_node_body:
        errs.append("[d] worker.py 未找到 `make_agent_node` 函数体")
    elif "is_stopped()" in make_agent_node_body:
        errs.append("[d] make_agent_node 体内仍调 `is_stopped()`（Option B·② 应删）")
    else:
        print("[d] OK  make_agent_node 体内无 `is_stopped()` 调用")

    # ── e. cancel_turn 体内无 _stop_event.set()，只有 task.cancel()（Option B·③）─
    cancel_body = _fn_body(gr_src, "cancel_turn")
    if not cancel_body:
        errs.append("[e] group_runtime.py 未找到 `cancel_turn` 函数体")
    else:
        cancel_code = _strip_comments_and_strings(cancel_body)
        if "_stop_event.set()" in cancel_code:
            errs.append("[e] cancel_turn 代码仍含 `_stop_event.set()`（Option B·③ 应纯 task.cancel）")
        else:
            print("[e] OK  cancel_turn 体内无 `_stop_event.set()`")
        if ".cancel()" not in cancel_code:
            errs.append("[e] cancel_turn 体内无 `task.cancel()` / `.cancel()`（硬切丢失）")
        else:
            print("[e] OK  cancel_turn 体内有 `.cancel()` 硬切")

    return errs


def main() -> int:
    print("=== Option B 删除回归契约（静态源码字符串断言，不依赖 live server / LLM）===\n")
    errs = assert_contract()
    if errs:
        print("\nFAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("\n=== 结果: PASS ===")
    print(
        "Option B 删除已彻底锁定：\n"
        "  · a mention.py 停关键词入站（_STOP_PHRASES / _is_stop_phrase）已删；\n"
        "  · b group_runtime.py 软停三件（request_stop / is_stopped / reset_stop）+ _stop_event 字段已删；\n"
        "  · c group_graph.py route_entry（standalone + closure twin）无 is_stopped() 守卫；\n"
        "  · d worker.py make_agent_node 无 is_stopped() 守卫；\n"
        "  · e cancel_turn 纯 task.cancel 硬切，无 _stop_event.set()。\n"
        "停止只留两入口：UI 终止按钮 cancel_turn + 系统 50 封顶（SESSION_SPEECH_CAP）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
