"""Structured result-card fragment parser (需求2 设计契约 backend side).

Mirror of the frontend ``parseCards`` regex (single regex source of truth for
the `````card```` fence wire format defined in
``docs/structured-result-card-schema.md`` §3/§6). Backend-side concerns:

- ``count_card_fragments(content)`` — best-effort observability: how many
  `````card```` fenced blocks the LLM emitted in this reply. Used by
  ``engine.reply.persist_agent_reply`` to log whether the worker followed the
  ``CARD_OUTPUT_CONTRACT`` prompt (log only when >0, never blocks persistence).
- ``extract_card_payloads(content)`` — decode each fenced block's JSON payload
  into a dict (skip invalid-JSON blocks, mirroring the frontend's "keep as
  plain code block, no crash" graceful-degradation contract). Provided for any
  future backend-side card surfacing; v1 does NOT store cards in ``data`` /
  ``task.artifact`` (cards are a ``content`` substring, per the design doc's
  no-DB-change decision).

Why a dedicated module (sibling to ``extract_json`` / ``json_stream``):
``extract_json`` parses the brain envelope (``{action,content,reasoning}``);
``json_stream.ContentExtractor`` decodes the ``content`` field incrementally
during streaming. This module parses the `````card```` fenced fragments that
live *inside* the already-decoded ``content`` string — a distinct concern, so
it gets its own home (no overloading of the envelope parsers).
"""
from __future__ import annotations

import json
import re

# Card fence regex — MUST stay byte-identical to the frontend ``CARD_RE`` in
# ``src/components/ChatMessageBubble.tsx`` (需求2-前端) so backend count and
# frontend parse agree on what a "card block" is. Pattern: three backticks +
# literal ``card`` info string + a newline, then capture (non-greedy, across
# newlines) up to the closing three backticks. ``[\s\S]`` = any char incl. \n.
CARD_FRAGMENT_RE = re.compile(r"```card\s*\n([\s\S]*?)```")


def count_card_fragments(content: str) -> int:
    """Return the number of `````card```` fenced blocks in ``content``.

    Counts fenced blocks regardless of whether the inner JSON is valid — the
    fence itself is the wire-format marker (a malformed-JSON card is still a
    card attempt; the frontend degrades it to a plain code block rather than
    silently dropping it, per the design doc §6). Used for observability only.
    """
    if not content:
        return 0
    return len(CARD_FRAGMENT_RE.findall(content))


def extract_card_payloads(content: str) -> list[dict]:
    """Decode each `````card```` block's JSON payload into a dict.

    Mirrors the frontend ``parseCards`` graceful-degradation contract: invalid
    JSON is skipped (frontend renders the block as a plain code block; backend
    simply does not surface it as a parsed card). Returns parsed payloads in
    document order. Non-string / non-dict top-level JSON values are skipped
    (the schema requires a top-level object).

    Not stored on the reply row in v1 — provided so a future backend feature
    (e.g. a ``/replies/<id>/cards`` endpoint) can decode cards server-side
    without re-implementing the regex.
    """
    out: list[dict] = []
    if not content:
        return out
    for m in CARD_FRAGMENT_RE.finditer(content):
        raw = m.group(1)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


__all__ = ["CARD_FRAGMENT_RE", "count_card_fragments", "extract_card_payloads"]
