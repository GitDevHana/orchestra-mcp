"""Strict final-result extraction and bounded context for sessions without resume."""
from __future__ import annotations

import json

HISTORY_TURNS = 8
HISTORY_CHARS = 24000
SUMMARY_CHARS = 6000


def response_object(text: str) -> dict:
    fallback = {"summary": text, "verdict": "continue", "deferred": []}
    decoder = json.JSONDecoder()
    cursor = 0
    last = None
    while (start := text.find("{", cursor)) >= 0:
        try:
            value, length = decoder.raw_decode(text[start:])
        except ValueError:
            cursor = start + 1
            continue
        cursor = start + length  # Never reinterpret a nested/example object as the result.
        last = (value, cursor)
    if last is None:
        return fallback
    value, end = last
    if text[end:].strip() not in {"", "```"}:
        return fallback
    if (not isinstance(value, dict) or not isinstance(value.get("summary"), str)
            or value.get("verdict") not in ("done", "continue")
            or not isinstance(value.get("deferred", []), list)):
        return fallback
    return value


def remember(history: list[dict], result: dict) -> None:
    history.append({"summary": str(result.get("summary", ""))[:SUMMARY_CHARS],
                    "verdict": result.get("verdict", "continue")})
    del history[:-HISTORY_TURNS]
    while len(json.dumps(history, ensure_ascii=False)) > HISTORY_CHARS:
        history.pop(0)
