"""JSON repair for LLM outputs.

Common LLM mistakes that this fixes:
- Wrapping JSON in ```json fences or other preamble.
- Trailing commas before } or ].
- Extra text after the JSON object.
"""
from __future__ import annotations

import json
import re

from core.runtime import log


def repair_json(text: str) -> str:
    """Best-effort repair: strip fences/preamble, find matching close, drop trailing commas."""
    # strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())

    # strip any preamble before the first { or [
    first_brace = text.find("{")
    first_bracket = text.find("[")
    if first_brace == -1 and first_bracket == -1:
        return text
    starts = [i for i in [first_brace, first_bracket] if i >= 0]
    text = text[min(starts):]

    # find matching end
    depth = 0
    end = 0
    opener = text[0]
    closer = "}" if opener == "{" else "]"
    for i, ch in enumerate(text):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
        if depth == 0:
            end = i
            break
    if end > 0:
        text = text[:end + 1]

    # fix trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text


def safe_parse_json(text: str, run_id: str, context: str = ""):
    """Parse JSON, falling back to `repair_json` on failure. Returns None if both fail."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    repaired = repair_json(text)
    try:
        result = json.loads(repaired)
        log(run_id, f"{context} JSON required repair but parsed successfully")
        return result
    except (json.JSONDecodeError, ValueError) as e:
        log(run_id, f"{context} JSON parse failed even after repair: {e}")
        return None
