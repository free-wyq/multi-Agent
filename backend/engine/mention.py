"""Mention routing + 30s anti-loop (Rust middleware.rs).

``find_mentions`` scans content for ``@token`` sequences stripping trailing
punctuation. ``resolve_mention`` matches against group members by agent_id,
agent name, or alias substring. ``route_mentions`` deduplicates per
(sender->target, 30s) key to prevent routing loops. ``route_user_message``
routes an inbound user message: @mention -> target agent, otherwise -> coordinator.

A2A 来回对话：``route_mentions`` 用 ``push_notify``（非 ``push_task``），让被 @ 的
peer 走 brain→chat 轻路径而非 execute 重路径——这样成语接龙/讨论等互动型任务能在
成员间来回传递（调度路径仍由 ``dispatcher._dispatch_one`` 直调 ``push_task``，互不
干涉）。反向清键（push 后清掉 target→sender）允许持续交替；同方向 30s 内连发仍被
拦（防死循环）。``_a2a_turns`` 按 group 计数 + ``_A2A_CAP`` 上限，防两个 LLM 无限刷屏。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from engine.inbox import push_notify, push_task
from store import crud

logger = logging.getLogger("multi-agent.mention")

# trailing punctuation to strip from mention tokens (Chinese + ASCII).
# 含全角/半角括号与冒号：LLM 常写「@前端工程师（agent_frontend_1）」或「@后端工程师：」，
# 旧版只吃掉句末标点，遇到「名字（备注）」会把整串「前端工程师（agent_frontend_1），请…」
# 当成一个 token，resolve 失败 → 接龙断在开局委派。括号等一旦出现就截断，只留括号前的名字。
_TRAIL = "，。：！？.,:!?、()（）[]【】「」\"'"

# 上述标点同时也作为 mention token 的「终止符」——遇到即停（不只是 rstrip 尾部）。
# 这样「@前端工程师（agent_frontend_1）」→ token 取到「前端工程师」即止，括号内容不再并入。
_TERM = _TRAIL

# A2A 来回对话的轮次上限（防两个 worker 互相 @ 无限刷屏）。env 可调；用户每发一条
# 新消息（route_user_message）就把该群计数清零，故一轮接龙从 0 重新数。
# 默认 50：给接龙/讨论等互动留够来回空间，正常互动几乎不会触顶。最终停止机制应靠
# 协调者监听群消息主动收尾（待实现），cap 仅作兜底防 LLM 失灵无限刷屏烧 token。
_A2A_CAP = max(1, int(os.environ.get("MULTI_AGENT_A2A_TURNS", "50")))
# group_id -> 已发生的 A2A @mention 传递次数
_a2a_turns: dict[str, int] = {}

# group_id -> {f"{sender_id}->{target_id}": timestamp} 防循环计数。
# **群级共享**（非 per-engine）：route_mentions 在群里任意成员回复时都可能被调（协调者
# 委派、worker 互相 @），防循环必须看「这对成员在群里最近一次同向传递」的全局视图。
# 原来传 self._recent_routes（每个 engine 一个空 dict）→ 前端@后端写前端的 dict、
# 后端@前端写后端的 dict，反向清键打不中对方 dict → 前端第二次@后端撞自己 dict 里 30s
# 内的「前端→后端」被拦 → 接龙 4 轮就断。改群级共享后：后端@前端反向清的是群 dict 里
# 的「前端→后端」，下次前端@后端不再被拦，持续交替；同方向连发（前端连两次@后端中间
# 无后端@前端）仍被「前端→后端」30s 内存在拦住——防死循环保留。
_group_recent_routes: dict[str, dict[str, float]] = {}


def _get_recent_routes(group_id: str) -> dict[str, float]:
    """获取（惰性创建）某群的共享防循环 dict。"""
    routes = _group_recent_routes.get(group_id)
    if routes is None:
        routes = {}
        _group_recent_routes[group_id] = routes
    return routes


def clear_group_routes(group_id: str) -> None:
    """清空某群的防循环状态（reset_session 调）。"""
    _group_recent_routes.pop(group_id, None)
    _a2a_turns.pop(group_id, None)


def find_mentions(content: str) -> list[str]:
    """Scan ``content`` for ``@name`` tokens, stripping trailing punctuation.

    Token 从 ``@`` 后开始，到**空白或终止标点**（括号/冒号/句末标点等，见 ``_TERM``）
    为止，再 rstrip 尾部标点。终止符让「@前端工程师（agent_frontend_1）」只取
    「前端工程师」、「@后端工程师：请…」只取「后端工程师」，避免把括号备注/后续句子
    并入 token 导致 resolve 失败。Duplicates are preserved (caller dedups).
    """
    tokens: list[str] = []
    i = 0
    while i < len(content):
        if content[i] == "@":
            start = i + 1
            j = start
            while j < len(content) and not content[j].isspace() and content[j] not in _TERM:
                j += 1
            if j > start:
                name = content[start:j].rstrip(_TRAIL)
                if name:
                    tokens.append(name)
            i = j
        else:
            i += 1
    return tokens


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict (``.get``) or a Pydantic model (``getattr``).

    Members and agents cross between dict and model forms, so call sites
    (``resolve_mention`` tiers + the coordinator fallback in ``route_mentions``)
    share this one normalizer instead of redefining it locally.
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_mention(
    members: list[Any],
    mention: str,
    agents: list[Any],
) -> str | None:
    """Three-tier match: (a) agent_id in members, (b) agent name in members, (c) alias contains token.

    ``members`` and ``agents`` may be dicts or Pydantic models; both are
    normalized to attribute access via ``_get``. Returns the matched agent_id
    or ``None``.
    """
    # (a) agent_id direct hit on a member
    for m in members:
        if _get(m, "agent_id") == mention:
            return _get(m, "agent_id")
    # (b) agent name hit on a member's agent
    for a in agents:
        if _get(a, "name") == mention and any(
            _get(m, "agent_id") == _get(a, "id") for m in members
        ):
            return _get(a, "id")
    # (b2) agent role hit on a member's agent (e.g. @frontend_engineer / @后端工程师).
    # role 是创建时的稳定标识（snake_case 英文），LLM 偶尔会用它 @人；name 才是首选，
    # 但 role 作为兜底匹配让接龙/委派不致因 token 形态不同而断链。仅匹配群成员的 agent。
    for a in agents:
        if _get(a, "role") == mention and any(
            _get(m, "agent_id") == _get(a, "id") for m in members
        ):
            return _get(a, "id")
    # (c) alias contains the token
    for m in members:
        alias = _get(m, "alias")
        if alias and mention in alias:
            return _get(m, "agent_id")
    return None


async def route_mentions(
    group_id: str,
    sender_id: str,
    sender_name: str,
    content: str,
    recent_routes: dict[str, float] | None = None,
) -> None:
    """Outbound mention routing with 30s anti-loop + A2A turn cap.

    Scans ``content`` for @mentions, resolves each to a target agent, and
    ``push_notify`` to the target（让 peer 走 brain→chat 轻路径，而非 push_task
    的 execute 重路径——互动型任务如成语接龙/讨论需成员间来回对话，不应被塞进
    create_react_agent）。防循环计数 ``recent_routes`` 记 ``f"{sender_id}->{target_id}"``
    -> timestamp，同方向 30s 内已路由则跳过（A 连发两次 @B = 死循环，挡掉）。成功
    push 后清掉反向 key（target→sender），允许 A→B→A→B 持续交替——若不清，B→A 之后
    A→B 会被 30s 内已存在拦死，来回只能跑 2 轮。

    ``recent_routes`` **必须是群级共享**的同一个 dict（见 ``_get_recent_routes``）。
    原来传每个 engine 自己的 ``self._recent_routes`` → 反向清键打不中对方 dict → 接龙
    4 轮就断。现在统一从群级 ``_group_recent_routes`` 取，A 写 A→B、B 回 B→A 时反向
    清的是群 dict 里的 A→B，下次 A→B 不再被拦。``recent_routes`` 参数保留向后兼容
    （None 时自动取群级共享 dict），但调用方应传群级共享 dict 或 None。

    ``_a2a_turns`` 按 group 计数每次 @ 传递，达 ``_A2A_CAP`` 后不再 push（回复仍
    落库+emit，话筒自然落地）；用户发新消息时 route_user_message 把计数清零。
    """
    # 群级共享防循环 dict（None 时取 _get_recent_routes 的群级映射）
    if recent_routes is None:
        recent_routes = _get_recent_routes(group_id)

    mentions = find_mentions(content)
    if not mentions:
        return

    # A2A 轮次上限：已达 cap 就不再路由（防无限刷屏）。回复已由调用方 _unified_reply
    # 落库+emit，用户看得到最后一条，话筒自然落地。
    if _a2a_turns.get(group_id, 0) >= _A2A_CAP:
        logger.debug(
            "[mention] group=%s a2a_turns=%d reached cap=%d, stop routing @mentions",
            group_id, _a2a_turns.get(group_id, 0), _A2A_CAP,
        )
        return

    now = time.time()
    # prune entries older than 30s
    stale = [k for k, t in recent_routes.items() if now - t >= 30.0]
    for k in stale:
        recent_routes.pop(k, None)

    members = await crud.list_group_members_with_agent(group_id)
    agents = await crud.list_agents()
    # coordinator 不是 members 表里的成员（members 只存普通成员），但 LLM 常把话筒 @回
    # 群主（@协调者 / @agent_coord_1）。resolve_mention 只看 members，会漏掉群主 →
    # 接龙开局后第一条回 @群主 就断链。这里把 coordinator_id 也并入候选解析。
    group = await crud.get_group(group_id)
    coordinator_id = (group.coordinator_id if group else "") or ""

    routed_any = False
    for mention in mentions:
        # skip self (by id or name)
        if mention == sender_id or mention == sender_name:
            continue
        target_id = resolve_mention(members, mention, agents)
        # 兜底：token 命中 coordinator_id / coordinator 名字 / coordinator role
        if not target_id and coordinator_id:
            coord_agent = next((a for a in agents if _get(a, "id") == coordinator_id), None)
            if mention == coordinator_id or (
                coord_agent and mention in (_get(coord_agent, "name", ""), _get(coord_agent, "role", ""))
            ):
                target_id = coordinator_id
        if not target_id or target_id == sender_id:
            logger.debug(
                "[mention] group=%s sender=%s mention=%r -> unresolved (or self), skip",
                group_id, sender_id, mention,
            )
            continue
        key = f"{sender_id}->{target_id}"
        if key in recent_routes:
            continue  # anti-loop: 同方向 30s 内已路由过（A 连发两次 @B）
        # push_notify（非 push_task）：peer 走 brain→chat 轻路径，互动型来回对话
        # 不进 execute 重路径。kind=agent_reply + sender_id，peer 的 brain 能看到
        # incoming_sender / incoming_message（_format_display_msg 加 [来自智能体 X] 前缀）。
        await push_notify(
            group_id, "agent_reply", sender_id, target_id, content, None
        )
        recent_routes[key] = now
        # 反向清键：A→B push 后清 B→A，允许 B 回 @A 再 A 回 @B 持续交替。
        # 不清的话 B→A 之后 A→B 撞 30s 内已存在，来回 2 轮就死。
        recent_routes.pop(f"{target_id}->{sender_id}", None)
        routed_any = True

    if routed_any:
        _a2a_turns[group_id] = _a2a_turns.get(group_id, 0) + 1


async def route_user_message(group_id: str, content: str, *, converge: bool = False) -> None:
    """Route an inbound user message onto the group's decentralized swarm graph.

    task-19/20: the production inbound path now drives the per-group
    ``GroupRuntime.invoke_turn`` (the compiled group graph = one turn) instead
    of pushing a notify to the resident coordinator ``AgentEngine``. The graph's
    ``route_entry`` forks the turn:

      · **@mention** → ``invoke_turn(incoming_kind="agent_reply"``,
        ``incoming_data=None``) — a peer handoff (no ``task_id``), so
        ``route_entry`` hands the turn to the @mentioned agent node
        (decentralized chat / 成语接龙 path).
      · **no @mention** → ``invoke_turn(incoming_kind="coordinator_reply")`` —
        engineering demand, ``route_entry`` routes to the Leader's ``classify``
        (centralized path).

    **Stop entries (Option B)**: Option B removed the inbound stop-keyword
    path (``停`` / ``stop`` / ``中断`` no longer short-circuit to
    ``request_stop``). Stopping now has two entries only — the UI stop button
    (``POST /groups/{id}/stop-turn`` → ``cancel_turn`` hard stop) and the
    session speech cap (``SESSION_SPEECH_CAP=50`` cross-turn backstop). A bare
    「停」 message is therefore routed like any ordinary chat message (no @ →
    coordinator central path, @人 → agent node), NOT as a stop signal.

    **@收束 回合收敛** (task ``converge-turn-design``): ``converge=True`` is the
    one-shot「收束」switch (UI toggle + @人 → a new turn that converges). It is
    ONLY meaningful on the @mention path — 收束必须 @ 收口对象. A 收束 turn with
    no @mention raises ``ValueError`` (the API turns it into a 400) so a bare
    收束 message never routes to a speaker or the Leader. On the @mention path
    it forwards ``converge=True`` to ``invoke_turn``, which injects it into the
    initial state so ``make_agent_node`` forces ``next_speaker=None`` (agent
    replies once, then ENDs without handoff — the turn converges). Fills the
    「人工停止」gap left by Option B's stop-keyword removal on the decentralized
    path. Purely additive — ``converge=False`` (default) changes nothing.

    **Dual-track fallback**: if the group has no ``GroupRuntime`` (cold group /
    compile failure / pre-load race), degrades to the legacy ``push_notify``
    path so the resident engine still drives the turn — additive, not a flag.
    A 收束 turn with no runtime degrades the same way (the resident engine has
    no converge semantics; the message still routes, just no convergence — the
    UI switch is best-effort against a cold runtime).

    **Path C single-chat split**: single-chat conversations no longer reach
    this function — the API layer (``api/messages.py``) routes single-chat
    messages to ``engine.direct.route_direct_message`` instead (the bypass
    that used to live here at mention.py:298-305 moved to ``engine/direct.py``).
    ``route_user_message`` is now group-chat-only.
    """
    from engine.registry import registry  # noqa: PLC0415 — defer to break the
    # registry→mention import cycle (registry imports mention at load time).

    group = await crud.get_group(group_id)
    mentions = find_mentions(content)
    if mentions:
        members = await crud.list_group_members_with_agent(group_id)
        agents = await crud.list_agents()
        for mention in mentions:
            target_id = resolve_mention(members, mention, agents)
            if target_id:
                # @mention → decentralized peer handoff onto the group graph
                # (no task_id → route_entry picks the agent node). Falls back to
                # the legacy notify when no runtime exists.
                rt = await registry.ensure_runtime(group_id)
                if rt is not None:
                    # @收束 (converge-turn-design): only the @mention path may
                    # 收束 (must @ the converging agent). ``converge`` is
                    # forwarded into invoke_turn's initial state so make_agent_node
                    # forces next_speaker=None (reply once → END, no handoff).
                    await rt.invoke_turn(
                        incoming_kind="agent_reply",
                        incoming_message=content,
                        incoming_sender="user",
                        incoming_data=None,
                        converge=converge,
                    )
                    return
                await push_notify(
                    group_id, "agent_reply", "user", target_id, content, None
                )
                return  # route to the first @mentioned agent only
    # no mention hit -> coordinator (centralized path). A 收束 turn with no
    # @mention is invalid (收束必须 @ 收口对象) — reject before routing so a
    # bare 收束 message neither reaches a speaker nor the Leader.
    if converge:
        raise ValueError("收束必须 @ 收口对象（@某成员后再开收束开关）")
    # no mention hit -> coordinator (centralized path)
    if group and group.coordinator_id:
        rt = await registry.ensure_runtime(group_id)  # local import above
        if rt is not None:
            await rt.invoke_turn(
                incoming_kind="coordinator_reply",
                incoming_message=content,
                incoming_sender="user",
                incoming_data=None,
            )
            return
        # legacy fallback: push a notify to the resident coordinator engine
        await push_notify(
            group_id,
            "coordinator_reply",
            "user",
            group.coordinator_id,
            content,
            None,
        )


async def route_plan_resume(
    group_id: str, payload: dict | None = None
) -> str | None:
    """PL-02: resume the group graph's paused dispatch node via ``resume_plan``.

    task-19/22: the production plan-confirm path now calls
    ``GroupRuntime.resume_plan(payload)`` directly — it issues
    ``Command(resume=<payload>)`` on the thread the prior ``invoke_turn`` paused
    at ``node_dispatch``'s ``interrupt()`` (the native LangGraph resume path),
    so ``interrupt()`` returns the payload and ``dispatch_next_group`` fans out
    the pending steps. Bypasses the inbox notify loop entirely (the old
    ``push_notify("plan_resume", ...)`` → coordinator engine ``_handle_notify``
    → ``Command(resume=)`` queue detour is retired for the runtime path).

    Called by the plan-confirm API endpoints (``/plan/confirm`` | ``/direct`` |
    ``/modify``); the payload is forwarded verbatim — the API owns the ``mode``
    semantics, the routing layer only resolves the runtime + resumes.

    **Dual-track fallback**: if the group has no ``GroupRuntime`` (cold / compile
    failure), degrades to the legacy ``push_notify("plan_resume")`` path so the
    resident engine's ``_handle_notify`` plan_resume branch still drives the
    resume. Returns the coordinator's ``agent_id`` if routed, else ``None``
    (group has no coordinator) — preserving the return-value contract the
    plan-confirm API callers rely on.
    """
    group = await crud.get_group(group_id)
    if not group or not group.coordinator_id:
        return None
    # local import (registry→mention cycle at load time; defer to call time).
    from engine.registry import registry  # noqa: PLC0415

    rt = await registry.ensure_runtime(group_id)
    if rt is not None:
        await rt.resume_plan(payload or {})
        return group.coordinator_id
    # legacy fallback: queue a plan_resume notify to the resident coordinator
    await push_notify(
        group_id,
        "plan_resume",
        "user",
        group.coordinator_id,
        "用户确认执行计划",
        payload,
    )
    return group.coordinator_id
