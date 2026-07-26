"""Usage aggregation report models (PRD 3.6 Token 仪表盘 · 任务15a).

``messages.data`` carries per-turn streaming run-stats
(``{reply_id, elapsed_ms, tokens, model, reasoning_tokens, reasoning?}``) for
``agent_reply`` rows produced by the chat/ask paths (coordinator ``node_chat``
+ worker ``node_brain_decide``). Execute-path announce + ``user_input`` rows
have ``data=None`` and carry no stats — they are excluded from aggregation.

The usage endpoint sums ``tokens`` / ``elapsed_ms`` / ``reasoning_tokens`` and
counts messages, grouped by a dimension (``model`` / ``day`` /
``conversation`` / ``agent``), optionally filtered by time range + model. The
aggregate is computed in SQL via SQLite JSON1 (``json_extract``) + ``GROUP BY``
so a long message history is not pulled back into Python.

``key`` is the group label — the model id (``group_by=model``), the ISO date
``YYYY-MM-DD`` (``group_by=day``), the ``conversation_id`` (``group_by=
conversation``) or the ``sender_id`` (``group_by=agent``). Empty string when
the dimension value is absent (e.g. a chat reply whose ``data.model`` is "" —
worker sets ``model=str(config.get("model") or "")``).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UsageRow(BaseModel):
    """One aggregated group (one row of the GROUP BY result)."""

    model_config = ConfigDict(extra="allow")

    key: str = ""
    tokens: int = 0
    elapsed_ms: int = 0
    reasoning_tokens: int = 0
    messages: int = 0


class UsageTotals(BaseModel):
    """Grand totals across all groups (sum of every row)."""

    model_config = ConfigDict(extra="allow")

    tokens: int = 0
    elapsed_ms: int = 0
    reasoning_tokens: int = 0
    messages: int = 0


class UsageReport(BaseModel):
    """Aggregated usage report returned by ``GET /api/usage``."""

    model_config = ConfigDict(extra="allow")

    start: str | None = None
    end: str | None = None
    model: str | None = None
    group_by: str = "model"
    totals: UsageTotals = UsageTotals()
    rows: list[UsageRow] = []
