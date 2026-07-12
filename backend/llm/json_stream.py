"""Streaming JSON envelope extractor (split from engine/coordinator.py, task B9).

``ContentExtractor`` decodes the ``content`` string field of a streaming JSON
object emitted by an LLM — chunk by chunk — so a 流式 bubble can render the
model's visible reply *as it arrives* without first seeing the JSON skeleton.

Why this lives in ``llm/``: it is a pure streaming-JSON-parsing utility with
no engine / graph / event dependency. Both the coordinator
(``_stream_coordinator_decision``) and the worker (``_stream_brain_decision``)
consume it, and previously the worker reached it via
``from engine.coordinator import _ContentExtractor`` — a reverse import that
made ``engine.worker`` depend on ``engine.coordinator`` (the two modules
otherwise have no coordinator→worker edge; coordinator never imports worker).
Centralizing it under ``llm/`` removes that coupling (B9) and gives the
helper a home next to ``extract_json`` (the non-streaming JSON extractor),
which is its natural sibling: ``extract_json`` parses a *complete* JSON blob
post-hoc, ``ContentExtractor`` parses the ``content`` field *incrementally*
during the stream.

Renamed from ``_ContentExtractor`` (private, underscore-prefixed in
coordinator) to ``ContentExtractor`` (public, in a dedicated module): a
module-private name made sense when it was coordinator-internal, but now that
it is a shared llm utility the leading underscore would signal "do not
import" — the opposite of its purpose. A public name documents that both
coordinator and worker are intended consumers.
"""
from __future__ import annotations


class ContentExtractor:
    """Extract the decoded ``content`` string from a streaming JSON envelope.

    Feed raw ``feed(delta)`` chunks as they arrive from the LLM. ``take()``
    returns the decoded content emitted since the last call (an incremental
    substring of the final content value, suitable for ``emit_coordinator_token``
    / ``emit_task_token``).

    The machine scans for ``"content"`` after the first ``{``, then tracks the
    subsequent string state (normal / after-backslash / done). Only characters
    inside that string are emitted — the JSON skeleton, the ``action``/``plan``
    fields, and any leading prose before ``{`` are skipped silently. A missing
    or non-string ``content`` field yields nothing (the caller falls back to the
    full raw text via extract_json, which is unaffected).
    """

    _KEY = '"content"'

    def __init__(self) -> None:
        # byte-ish buffer of unprocessed input; kept as str (deltas may split a
        # key/escape across chunks, so we retain a small lookback)
        self._buf = ""
        # True once we've located the "content" key and its opening quote
        self._in_content = False
        # True when the previous char was an unescaped backslash (next char is
        # literal, not a string terminator / escape control)
        self._escaped = False
        # accumulated decoded content not yet taken
        self._out = ""
        # track whether the "content" key matched so far (prefix length)
        self._key_idx = 0
        # whether we've seen the opening brace yet (prose before { is skipped)
        self._brace_seen = False

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self._buf += delta
        # process as much as we can; we stop when a char might be part of a
        # multi-char token (partial key / escape) that could be completed by a
        # later delta. We re-scan the buffer in a loop, trimming consumed head.
        i = 0
        n = len(self._buf)
        hold = False
        while i < n:
            ch = self._buf[i]
            if not self._brace_seen:
                if ch == "{":
                    self._brace_seen = True
                    i += 1
                    continue
                # skip prose before the first brace
                i += 1
                continue
            if not self._in_content:
                # try to match the "content" key at position i
                if self._buf[i : i + len(self._KEY)] == self._KEY:
                    self._key_idx = len(self._KEY)
                    i += len(self._KEY)
                    continue
                # partial match of the key at the buffer tail → wait for more
                tail = self._buf[i:]
                if len(tail) < len(self._KEY) and self._KEY.startswith(tail):
                    hold = True
                    break
                # not matching the key: look for the colon + opening quote after
                # a complete key match, or skip one char otherwise.
                if self._key_idx == len(self._KEY):
                    # we matched the full key; now expect optional ws + ':' + ws + '"'
                    if ch in ' \t\r\n':
                        i += 1
                        continue
                    if ch == ":":
                        i += 1
                        continue
                    if ch == '"':
                        self._in_content = True
                        self._escaped = False
                        i += 1
                        continue
                    # content was a non-string (null/number/obj) — reset key,
                    # keep scanning for a later "content" (rare; LLM contract is str)
                    self._key_idx = 0
                    i += 1
                    continue
                # reset partial key tracking and advance
                self._key_idx = 0
                i += 1
                continue
            # inside the content string
            if self._escaped:
                # previous was backslash: this char is the escape body
                mapping = {
                    '"': '"',
                    "\\": "\\",
                    "/": "/",
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    "b": "\b",
                    "f": "\f",
                }
                self._out += mapping.get(ch, ch)
                self._escaped = False
                i += 1
                continue
            if ch == "\\":
                # consume the backslash; the next char (possibly in a later
                # delta) completes the escape. Hold here so a chunk split mid-
                # escape ("\" then "n" in two deltas) decodes correctly.
                self._escaped = True
                i += 1
                hold = True
                # don't break yet — if there's more in buf we can keep going,
                # but the escape needs the next char which may be index i now.
                # Re-loop: if i < n we process the escape body immediately.
                continue
            if ch == '"':
                # closing quote — content value ended
                self._in_content = False
                self._key_idx = 0
                i += 1
                continue
            # normal literal char
            self._out += ch
            i += 1
        # retain the unconsumed tail (held partial token) for the next feed
        if hold:
            self._buf = self._buf[i:]
        else:
            self._buf = ""

    def take(self) -> str:
        """Return and clear the decoded content accumulated since the last call."""
        if not self._out:
            return ""
        out = self._out
        self._out = ""
        return out


__all__ = ["ContentExtractor"]
