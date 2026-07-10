"""Extract first JSON object from LLM text response (Rust llm.rs extract_json).

Strips ```json fences and does string-literal-aware brace matching so braces
inside string values do not break pairing. Returns dict | None.
"""
from __future__ import annotations

import json


def extract_json(raw: str) -> dict | None:
    """Extract the first ``{...}`` JSON object from ``raw``.

    Handles ```` ```json ```` fences and tracks string-literal state while
    counting brace depth so a ``}`` inside a string value does not corrupt
    the match. Returns the parsed dict or ``None`` if no valid object is found.
    """
    s = raw.strip()
    # strip markdown ```json ... ``` fence
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        end = s.rfind("```")
        if end != -1:
            s = s[:end]
        s = s.strip()

    start = s.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None
