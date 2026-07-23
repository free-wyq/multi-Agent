"""VH60 单元契约：worker delegate(skill_id, subtask) @tool.

锁住 task-2026-07-23「给 worker 执行路径加阻塞式 delegate @tool，按 skill 拉起临时子
智能体」的核心逻辑契约（纯单元，不依赖 live server / 真实 LLM）：

  A. 静态契约（agent_loop.py 源码字符串断言）
    1. ``_DELEGATE_DEPTH`` contextvar 存在 + ``_DELEGATE_MAX_DEPTH = 2``。
    2. ``_build_delegate_tool`` 工厂存在。
    3. ``run_agent_loop`` 注入点 ``if _DELEGATE_DEPTH.get() == 0`` 存在（仅 depth==0 注入）。
    4. ``run_skill_loop`` 函数体内不写 ``_DELEGATE_DEPTH``（子执行体不自己 set depth，
       只靠外层 delegate 的 set/reset——断 delegate 路径未污染 skill run loop）。

  B. depth 守卫
    5. 把 ``_DELEGATE_DEPTH.set(_DELEGATE_MAX_DEPTH)`` 后调 delegate → 返回含「已达最大
       嵌套深度」+ ``crud.get_skill`` 未被调 + ``run_skill_loop`` 未被调。测后还原。

  C. skill 校验
    6. ``crud.get_skill`` 返 None → 返回「技能不存在」+ run_skill_loop 未调。
    7. ``requires_tools`` 空 → 返回「未声明合法 requires_tools」+ run_skill_loop 未调。
    8. ``requires_tools`` 含未知工具 → 同 7。

  D. happy path
    9. mock crud.get_skill 返合法 skill + patch run_skill_loop 返 ``{"success":True,
       "output":"done"}``，且在 mock 内读 ``_DELEGATE_DEPTH.get()`` 存下断言==1
       （depth 透传进子执行体）；patch skill_assets.skill_output_path 返一个含一个
       产物文件的目录 → 调 delegate → 断言返回含 "done" + "[产物]"；finally 后
       ``_DELEGATE_DEPTH.get() == 0``（还原）；``run_skill_loop`` 被以 ``skill_id`` /
       ``prompt`` / ``tools`` / ``on_event=None`` 正确参数调一次。

  E. 产物为空
   10. happy path 但无产物文件 → 返回不含 "[产物]"，只有 "done"。

调用约定：LangChain ``@tool`` 装饰的 ``delegate`` 是 BaseTool，async 调用用
``await delegate.ainvoke({"skill_id": "...", "subtask": "..."})``，返回 str。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def check(errs: list[str], label: str, cond: bool) -> None:
    if cond:
        print(f"[OK] {label}")
    else:
        errs.append(label)
        print(f"[FAIL] {label}")


# ── 静态契约 ──────────────────────────────────────────────────────
def assert_static_contract(errs: list[str]) -> None:
    from engine import agent_loop

    src = inspect.getsource(agent_loop)

    # [1] _DELEGATE_DEPTH contextvar + _DELEGATE_MAX_DEPTH = 2
    check(
        errs,
        "[A1] _DELEGATE_DEPTH contextvar 声明存在",
        "_DELEGATE_DEPTH" in src and "contextvars.ContextVar" in src,
    )
    check(
        errs,
        "[A1] _DELEGATE_MAX_DEPTH = 2",
        "_DELEGATE_MAX_DEPTH = 2" in src or "_DELEGATE_MAX_DEPTH=2" in src,
    )

    # [2] _build_delegate_tool 工厂存在
    check(
        errs,
        "[A2] _build_delegate_tool 工厂存在",
        "def _build_delegate_tool(" in src,
    )

    # [3] 注入点 if _DELEGATE_DEPTH.get() == 0（仅顶层 worker turn 注入）
    check(
        errs,
        "[A3] run_agent_loop 内 delegate 注入点（depth==0）存在",
        "if _DELEGATE_DEPTH.get() == 0:" in src,
    )

    # [4] run_skill_loop 函数体内不写 _DELEGATE_DEPTH（不 set/reset，无耦合）
    # 用 inspect.getsource 精确取函数源码（regex 跨函数体会误吞嵌套 async def）
    rsl_src = inspect.getsource(agent_loop.run_skill_loop)
    check(
        errs,
        "[A4] run_skill_loop 体内不写 _DELEGATE_DEPTH（delegate 路径未污染 skill loop）",
        "_DELEGATE_DEPTH" not in rsl_src,
    )


def _fn_body(src: str, fname: str, is_async: bool = False) -> str:
    """Extract a Python function body up to the next top-level def."""
    import re

    prefix = "async def" if is_async else "def"
    pat = rf"{prefix} {fname}\([^)]*\).*?(?=\n(?:async )?def |\Z)"
    m = re.search(pat, src, re.S)
    return m.group(0) if m else ""


# ── 动态单元（mock crud / run_skill_loop / skill_assets） ─────────
def _make_skill(
    skill_id: str = "sk1",
    name: str = "Regex专家",
    requires_tools: list[str] | None = None,
    content: str = "# skill body",
):
    if requires_tools is None:
        requires_tools = ["file_read", "bash_run"]
    return SimpleNamespace(
        id=skill_id,
        name=name,
        description="d",
        content=content,
        requires_tools=list(requires_tools),
        triggers=[],
        outputs=[],
    )


async def run_dynamic_cases() -> list[str]:
    """Cases B/C/D/E. Returns list of error labels (empty if all pass)."""
    errs: list[str] = []
    from engine import agent_loop
    from engine.agent_loop import _DELEGATE_DEPTH, _DELEGATE_MAX_DEPTH

    # 安全网：测试开始前 depth 必须为 0（每个 case 自己 set/reset 还原）
    check(errs, "[setup] 初始 _DELEGATE_DEPTH.get() == 0", _DELEGATE_DEPTH.get() == 0)

    # ── B. depth 守卫 ─────────────────────────────────────────────
    # set 到 max depth 后调 delegate → 应拒绝 + 不调 crud/run_skill_loop
    crud_mock = AsyncMock()
    skill_loop_mock = AsyncMock(return_value={"success": True, "output": "should not"})
    token_depth = _DELEGATE_DEPTH.set(_DELEGATE_MAX_DEPTH)
    try:
        with patch.object(agent_loop, "crud", crud_mock), \
             patch.object(agent_loop, "run_skill_loop", skill_loop_mock), \
             patch.object(agent_loop, "skill_assets"):
            delegate_tool = agent_loop._build_delegate_tool(
                "g1", "alice", "t1", on_log=None
            )
            ret = await delegate_tool.ainvoke(
                {"skill_id": "sk1", "subtask": "do x"}
            )
            check(
                errs,
                "[B5] 深度封顶 → 返回含「已达最大嵌套深度」",
                "已达最大嵌套深度" in str(ret),
            )
            check(
                errs,
                "[B5] 深度封顶 → crud.get_skill 未被调",
                crud_mock.get_skill.call_count == 0,
            )
            check(
                errs,
                "[B5] 深度封顶 → run_skill_loop 未被调",
                skill_loop_mock.call_count == 0,
            )
    finally:
        _DELEGATE_DEPTH.reset(token_depth)
    check(
        errs,
        "[B5] 深度封顶测后 _DELEGATE_DEPTH 还原 == 0",
        _DELEGATE_DEPTH.get() == 0,
    )

    # ── C. skill 校验 ────────────────────────────────────────────
    # C6 skill 不存在
    crud_c6 = AsyncMock()
    crud_c6.get_skill = AsyncMock(return_value=None)
    skill_loop_c6 = AsyncMock()
    with patch.object(agent_loop, "crud", crud_c6), \
         patch.object(agent_loop, "run_skill_loop", skill_loop_c6), \
         patch.object(agent_loop, "skill_assets"):
        delegate_tool = agent_loop._build_delegate_tool(
            "g1", "alice", "t1", on_log=None
        )
        ret = await delegate_tool.ainvoke(
            {"skill_id": "missing", "subtask": "do x"}
        )
        check(
            errs,
            "[C6] skill 不存在 → 返回含「技能不存在」",
            "技能" in str(ret) and "不存在" in str(ret),
        )
        check(
            errs,
            "[C6] skill 不存在 → run_skill_loop 未被调",
            skill_loop_c6.call_count == 0,
        )

    # C7 requires_tools 空
    crud_c7 = AsyncMock()
    crud_c7.get_skill = AsyncMock(return_value=_make_skill(requires_tools=[]))
    skill_loop_c7 = AsyncMock()
    with patch.object(agent_loop, "crud", crud_c7), \
         patch.object(agent_loop, "run_skill_loop", skill_loop_c7), \
         patch.object(agent_loop, "skill_assets"):
        delegate_tool = agent_loop._build_delegate_tool(
            "g1", "alice", "t1", on_log=None
        )
        ret = await delegate_tool.ainvoke(
            {"skill_id": "sk1", "subtask": "do x"}
        )
        check(
            errs,
            "[C7] requires_tools=[] → 返回含「未声明合法 requires_tools」",
            "未声明合法 requires_tools" in str(ret),
        )
        check(
            errs,
            "[C7] requires_tools=[] → run_skill_loop 未被调",
            skill_loop_c7.call_count == 0,
        )

    # C8 requires_tools 含未知工具
    crud_c8 = AsyncMock()
    crud_c8.get_skill = AsyncMock(
        return_value=_make_skill(requires_tools=["file_read", "unknown_tool"])
    )
    skill_loop_c8 = AsyncMock()
    with patch.object(agent_loop, "crud", crud_c8), \
         patch.object(agent_loop, "run_skill_loop", skill_loop_c8), \
         patch.object(agent_loop, "skill_assets"):
        delegate_tool = agent_loop._build_delegate_tool(
            "g1", "alice", "t1", on_log=None
        )
        ret = await delegate_tool.ainvoke(
            {"skill_id": "sk1", "subtask": "do x"}
        )
        check(
            errs,
            "[C8] requires_tools 含未知工具 → 返回含「未声明合法 requires_tools」",
            "未声明合法 requires_tools" in str(ret),
        )
        check(
            errs,
            "[C8] requires_tools 含未知工具 → run_skill_loop 未被调",
            skill_loop_c8.call_count == 0,
        )

    # ── D. happy path ─────────────────────────────────────────────
    # D9 mock 合法 skill + run_skill_loop 返 done + tmp dir 有产物文件
    depth_seen: list[int] = []

    async def _fake_run_skill_loop(*, skill_id, skill_name, skill_content,
                                   prompt, tools, on_event, max_turns,
                                   agent_model=""):
        depth_seen.append(_DELEGATE_DEPTH.get())
        return {"success": True, "output": "done"}

    crud_d = AsyncMock()
    crud_d.get_skill = AsyncMock(return_value=_make_skill())
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text("# product", encoding="utf-8")

        sa_d = SimpleNamespace()
        sa_d.skill_workspace_path = lambda sid: Path(td)
        sa_d.skill_output_path = lambda sid: out_dir

        skill_loop_d = AsyncMock(side_effect=_fake_run_skill_loop)
        with patch.object(agent_loop, "crud", crud_d), \
             patch.object(agent_loop, "run_skill_loop", skill_loop_d), \
             patch.object(agent_loop, "skill_assets", sa_d):
            delegate_tool = agent_loop._build_delegate_tool(
                "g1", "alice", "t1", on_log=None
            )
            ret = await delegate_tool.ainvoke(
                {"skill_id": "sk1", "subtask": "请生成正则报告"}
            )
            check(
                errs,
                "[D9] happy path → 返回含 'done'",
                "done" in str(ret),
            )
            check(
                errs,
                "[D9] happy path → 返回含 '[产物]'",
                "[产物]" in str(ret),
            )
            check(
                errs,
                "[D9] happy path → 返回含产物文件名 'report.md'",
                "report.md" in str(ret),
            )
            # depth 透传：run_skill_loop 调用期间 _DELEGATE_DEPTH == 1
            check(
                errs,
                "[D9] depth 透传：run_skill_loop 期间 _DELEGATE_DEPTH == 1",
                len(depth_seen) == 1 and depth_seen[0] == 1,
            )
            # finally 后还原
            check(
                errs,
                "[D9] finally 后 _DELEGATE_DEPTH.get() == 0",
                _DELEGATE_DEPTH.get() == 0,
            )
            # run_skill_loop 调用参数正确
            check(
                errs,
                "[D9] run_skill_loop 被调一次",
                skill_loop_d.call_count == 1,
            )
            call_kwargs = skill_loop_d.call_args.kwargs
            check(
                errs,
                "[D9] run_skill_loop 参数 skill_id 正确",
                call_kwargs.get("skill_id") == "sk1",
            )
            check(
                errs,
                "[D9] run_skill_loop 参数 prompt 正确（透传 subtask）",
                call_kwargs.get("prompt") == "请生成正则报告",
            )
            check(
                errs,
                "[D9] run_skill_loop 参数 on_event=None",
                call_kwargs.get("on_event") is None,
            )
            check(
                errs,
                "[D9] run_skill_loop 参数 tools 非空 list",
                isinstance(call_kwargs.get("tools"), list)
                and len(call_kwargs["tools"]) > 0,
            )

    # ── E. 产物为空 ───────────────────────────────────────────────
    # E10 happy path 但 output/ 目录下无文件
    crud_e = AsyncMock()
    crud_e.get_skill = AsyncMock(return_value=_make_skill())
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 不写任何产物文件

        sa_e = SimpleNamespace()
        sa_e.skill_workspace_path = lambda sid: Path(td)
        sa_e.skill_output_path = lambda sid: out_dir

        skill_loop_e = AsyncMock(
            return_value={"success": True, "output": "done"}
        )
        with patch.object(agent_loop, "crud", crud_e), \
             patch.object(agent_loop, "run_skill_loop", skill_loop_e), \
             patch.object(agent_loop, "skill_assets", sa_e):
            delegate_tool = agent_loop._build_delegate_tool(
                "g1", "alice", "t1", on_log=None
            )
            ret = await delegate_tool.ainvoke(
                {"skill_id": "sk1", "subtask": "do x"}
            )
            check(
                errs,
                "[E10] 无产物 → 返回含 'done'",
                "done" in str(ret),
            )
            check(
                errs,
                "[E10] 无产物 → 返回不含 '[产物]'",
                "[产物]" not in str(ret),
            )

    # ── on_log 透传 ──────────────────────────────────────────────
    # F11 happy path 带 on_log，验证 tool_start/tool_end 都被调
    log_calls: list[tuple] = []

    async def _on_log(kind, content, data):
        log_calls.append((kind, content, data))

    crud_f = AsyncMock()
    crud_f.get_skill = AsyncMock(return_value=_make_skill())
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        sa_f = SimpleNamespace()
        sa_f.skill_workspace_path = lambda sid: Path(td)
        sa_f.skill_output_path = lambda sid: out_dir

        skill_loop_f = AsyncMock(
            return_value={"success": True, "output": "done"}
        )
        with patch.object(agent_loop, "crud", crud_f), \
             patch.object(agent_loop, "run_skill_loop", skill_loop_f), \
             patch.object(agent_loop, "skill_assets", sa_f):
            delegate_tool = agent_loop._build_delegate_tool(
                "g1", "alice", "t1", on_log=_on_log
            )
            await delegate_tool.ainvoke(
                {"skill_id": "sk1", "subtask": "do x"}
            )
            kinds = [c[0] for c in log_calls]
            check(
                errs,
                "[F11] on_log 透传：tool_start 被调",
                "tool_start" in kinds,
            )
            check(
                errs,
                "[F11] on_log 透传：tool_end 被调",
                "tool_end" in kinds,
            )

    return errs


def main() -> int:
    print("=== VH60 单元契约：worker delegate(skill_id, subtask) @tool ===\n")
    errs: list[str] = []

    print("── 阶段 A：静态契约 ──")
    assert_static_contract(errs)
    if errs:
        print("\n[阶段A] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1
    print("[阶段A] PASS\n")

    print("── 阶段 B-E：动态单元（mock crud/run_skill_loop/skill_assets） ──")
    dyn_errs = asyncio.run(run_dynamic_cases())
    errs.extend(dyn_errs)
    if errs:
        print("\n[动态] FAIL:")
        for e in dyn_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(
        "worker delegate @tool 契约通：\n"
        "  · A 静态：_DELEGATE_DEPTH contextvar + _DELEGATE_MAX_DEPTH=2 + "
        "_build_delegate_tool 工厂 + depth==0 注入点 + run_skill_loop 不耦合；\n"
        "  · B depth 守卫：depth>=2 拒绝 delegate + 不调 crud/run_skill_loop；\n"
        "  · C skill 校验：不存在/空 requires_tools/未知工具 三态拒绝；\n"
        "  · D happy path：返回 done + [产物] + depth 透传到 1 + finally 还原 0 + "
        "run_skill_loop 参数正确（on_event=None / skill_id / prompt / tools）；\n"
        "  · E 无产物：返回不含 [产物]；\n"
        "  · F on_log 透传：tool_start/tool_end 都被调。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
