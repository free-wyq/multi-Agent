"""Task 12 (验证A): backend coordinator/registry/api/mention 互调 import 通过.

Asserts the M12 interrupt migration's cross-module wiring resolves at runtime:
  [1] the four modules import cleanly (no ImportError after tasks 1-11 edits);
  [2] main (full app wiring incl all routers + engine tree) imports OK;
  [3] api.plan binds route_plan_resume (migrated channel) and NOT route_plan_confirm
      (legacy pusher removed in task 11);
  [4] engine.mention exposes route_plan_resume, NOT route_plan_confirm;
  [5] registry.AgentEngine._handle_notify uses Command(resume=...);
  [6] registry.AgentEngine.reset_session uses aupdate_state(as_node=END);
  [7] coordinator.node_dispatch calls interrupt();
  [8] coordinator.node_classify_incoming keeps the plan_confirm defensive branch;
  [9] api.plan.plan_get reads via _read_resident_plan -> graph.get_state (checkpointer truth);
  [10] route_after_dispatch body has no wait_confirm branch (docstring-only mention is OK).
"""
from __future__ import annotations

import inspect
import os
import sys

# tests/ → backend/ root so `engine` / `api` / `main` packages resolve (mirrors
# every other test script in this dir, which all run from the backend/ cwd).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    errs: list[str] = []

    # [1] the four modules import cleanly
    try:
        import engine.coordinator  # noqa: F401
        import engine.registry  # noqa: F401
        import engine.mention  # noqa: F401
        import api.plan  # noqa: F401
        print("[1] OK  4 模块 import 无 ImportError (coordinator/registry/mention/api.plan)")
    except Exception as e:
        errs.append(f"[1] module import failed: {type(e).__name__}: {e}")

    # [2] main (full app wiring) imports OK
    try:
        import main  # noqa: F401
        print("[2] OK  main 全量装配 import (所有 router + engine 树)")
    except Exception as e:
        errs.append(f"[2] main import failed: {type(e).__name__}: {e}")

    # [3] api.plan binds route_plan_resume (migrated) and NOT route_plan_confirm (removed)
    try:
        from api import plan as plan_api
        if not callable(getattr(plan_api, "route_plan_resume", None)):
            errs.append("[3] api.plan does not bind route_plan_resume")
        else:
            print("[3] OK  api.plan.route_plan_resume bound (migrated channel)")
        if hasattr(plan_api, "route_plan_confirm"):
            errs.append("[3] api.plan still binds route_plan_confirm (should be removed)")
    except Exception as e:
        errs.append(f"[3] api.plan inspection failed: {type(e).__name__}: {e}")

    # [4] engine.mention exposes route_plan_resume, NOT route_plan_confirm
    try:
        import engine.mention as m
        if not callable(getattr(m, "route_plan_resume", None)):
            errs.append("[4] engine.mention.route_plan_resume missing")
        else:
            print("[4] OK  engine.mention.route_plan_resume callable (sole confirm channel)")
        if hasattr(m, "route_plan_confirm"):
            errs.append("[4] engine.mention.route_plan_confirm still present (should be removed)")
    except Exception as e:
        errs.append(f"[4] engine.mention inspection failed: {type(e).__name__}: {e}")

    # [5] registry._handle_notify uses Command(resume=...)
    try:
        from engine.registry import AgentEngine
        src = inspect.getsource(AgentEngine._handle_notify)
        if "Command(resume=" not in src:
            errs.append("[5] registry._handle_notify does not use Command(resume=)")
        else:
            print("[5] OK  registry._handle_notify uses Command(resume=...)")
    except Exception as e:
        errs.append(f"[5] registry inspection failed: {type(e).__name__}: {e}")

    # [6] registry.reset_session uses aupdate_state(as_node=END)
    try:
        src2 = inspect.getsource(AgentEngine.reset_session)
        if "aupdate_state" not in src2 or "as_node=END" not in src2:
            errs.append("[6] registry.reset_session does not use aupdate_state(as_node=END)")
        else:
            print("[6] OK  registry.reset_session uses aupdate_state(as_node=END)")
    except Exception as e:
        errs.append(f"[6] reset_session inspection failed: {type(e).__name__}: {e}")

    # [7] coordinator.node_dispatch calls interrupt()
    try:
        from engine import coordinator as coord
        src_d = inspect.getsource(coord.node_dispatch)
        if "interrupt(" not in src_d:
            errs.append("[7] coordinator.node_dispatch does not call interrupt()")
        else:
            print("[7] OK  coordinator.node_dispatch calls interrupt()")
    except Exception as e:
        errs.append(f"[7] node_dispatch inspection failed: {type(e).__name__}: {e}")

    # [8] coordinator.node_classify_incoming keeps the plan_confirm defensive branch
    try:
        src_c = inspect.getsource(coord.node_classify_incoming)
        if "plan_confirm" not in src_c:
            errs.append("[8] coordinator.node_classify_incoming lost plan_confirm defensive branch")
        else:
            print("[8] OK  coordinator.node_classify_incoming keeps plan_confirm defensive branch")
    except Exception as e:
        errs.append(f"[8] classify inspection failed: {type(e).__name__}: {e}")

    # [9] plan_get reads via _read_resident_plan -> graph.get_state (checkpointer truth)
    try:
        src_p = inspect.getsource(plan_api.plan_get)
        if "_read_resident_plan" not in src_p:
            errs.append("[9] plan_get does not call _read_resident_plan (checkpointer read)")
        else:
            src_rp = inspect.getsource(plan_api._read_resident_plan)
            if "get_state" not in src_rp:
                errs.append("[9] _read_resident_plan does not call graph.get_state")
            else:
                print("[9] OK  plan_get -> _read_resident_plan -> graph.get_state (checkpointer truth)")
    except Exception as e:
        errs.append(f"[9] plan_get inspection failed: {type(e).__name__}: {e}")

    # [10] route_after_dispatch body has no wait_confirm branch (docstring-only mention OK)
    try:
        src_rad = inspect.getsource(coord.route_after_dispatch)
        doc_close = src_rad.find('"""', src_rad.find('"""') + 3) + 3
        body = src_rad[doc_close:]
        if "wait_confirm" in body:
            errs.append(f"[10] route_after_dispatch executable body refs wait_confirm: {body.strip()!r}")
        elif "dispatch_next" not in body:
            errs.append("[10] route_after_dispatch body does not wire dispatch_next")
        else:
            print("[10] OK  route_after_dispatch body wires dispatch_next (wait_confirm only in docstring)")
    except Exception as e:
        errs.append(f"[10] route_after_dispatch inspection failed: {type(e).__name__}: {e}")

    print("=" * 60)
    if errs:
        print(f"FAIL — {len(errs)} 项：")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PASS — coordinator/registry/api/mention 互调 import + 符号解析全通过：")
    print("  · 4 模块 import 无 ImportError；main 全量装配 OK；")
    print("  · 迁移触及的跨模块符号全部在运行时解析到位：")
    print("    - api.plan.route_plan_resume 绑定（legacy route_plan_confirm 已无残留）；")
    print("    - engine.mention.route_plan_resume 可调（route_plan_confirm 已删）；")
    print("    - registry._handle_notify Command(resume=) + reset_session aupdate_state(END)；")
    print("    - coordinator.node_dispatch interrupt() + classify plan_confirm 防御分支；")
    print("    - plan_get -> _read_resident_plan -> graph.get_state（checkpointer 真源）；")
    print("    - route_after_dispatch body 无 wait_confirm 分支（仅 docstring 提及）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
