"""Robust JSON extraction from LLM responses.

Models often wrap JSON in prose or markdown fences. These helpers recover the
JSON value (object or array) from such responses without failing hard.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str):
    """Return the first parseable JSON value (dict or list) in *text*, or None."""
    text = (text or "").strip()
    if not text:
        return None

    candidates: list[str] = []
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1))

    # First balanced-looking object or array span.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start : end + 1])

    candidates.append(text)
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    return None
