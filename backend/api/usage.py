"""Usage aggregation route (PRD 3.6 Token 仪表盘 · 任务15a).

Routes map to frontend ``usageApi``:
  GET /api/usage?start=&end=&model=&group_by=  → get_usage

Aggregates token usage from ``messages.data`` (the per-turn streaming run-stats
``{tokens, elapsed_ms, model, reasoning_tokens, ...}`` carried on chat/ask-path
``agent_reply`` rows). Execute-path announce + ``user_input`` rows have
``data=None`` and carry no stats — they are excluded by the
``json_extract(data,'$.elapsed_ms') IS NOT NULL`` filter in ``aggregate_usage``.

Aggregation is done in SQL via SQLite JSON1 + ``GROUP BY`` so a long message
history is one query (not pulled back into Python). See
``store.crud.aggregate_usage`` for the GROUP BY dimension mapping (model/day/
conversation/agent) + the totals/rows split.

Note: execute-path replies (registry ``_reply`` announce like ``任务完成 🎉``)
and tool-call turns do NOT pass through ``persist_agent_reply`` with stats —
their ``data`` is ``None``, so they are not counted here. Only chat/ask brain
LLM replies (coordinator ``node_chat`` + worker ``node_brain_decide``) carry
stats. This is the documented scope of 任务15a/15c — the「execute 路径未计入
口径」caveat is surfaced in the frontend UsageDashboard (任务15c).
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from models import UsageReport
from store import crud

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("")
async def get_usage(
    start: str | None = Query(
        default=None,
        description="ISO-8601 下界（含），按 messages.created_at 字符串比较。None=不限。",
    ),
    end: str | None = Query(
        default=None,
        description="ISO-8601 上界（不含）。None=不限。",
    ),
    model: str | None = Query(
        default=None,
        description="按 data.model 精确匹配过滤。None=全部模型。",
    ),
    group_by: str = Query(
        default="model",
        description="聚合维度：model(默认) / day / conversation / agent。",
    ),
) -> UsageReport:
    """Aggregate token usage by dimension, optionally filtered by range + model.

    Returns a ``UsageReport`` (``totals`` + ``rows``). Always 200 — empty
    result yields a report with zeroed totals + empty ``rows``, never 404.
    Invalid ``group_by`` falls back to ``model`` (lenient, no 400).
    """
    return await crud.aggregate_usage(start=start, end=end, model=model, group_by=group_by)
