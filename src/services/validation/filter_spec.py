"""One canonical form for a scenario filter.

A filter reached the plan in two shapes. The model writes strings —
``Date[Fiscal Year] = 'FY2018'`` — while the scenario builder emits pairs,
``("Date[Fiscal Year]", "FY2018")``. Code that parsed only the string form
matched nothing on a pair and moved on, so a compiled measure lost its filter
and returned the *unfiltered* total with full confidence: every filtered
scenario of Total Sales Amount reported 109,809,274 instead of the 23,860,891
the dashboard shows.

Two rules follow from that:

* Normalise both shapes here, so no caller has to know which it received.
* A filter that cannot be parsed is never silently dropped. Losing a filter
  does not produce an error, it produces a plausible wrong number, which is the
  one outcome this application exists to prevent.
"""

from __future__ import annotations

import re

__all__ = ["FILTER_RE", "normalise", "normalise_all", "parse"]

#: ``Table[Column] = 'Value'`` — the canonical written form.
FILTER_RE = re.compile(
    r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*(?:=|==)\s*'?\"?(.*?)'?\"?\s*$"
)

#: ``Table[Column]`` on its own, as the left half of a pair.
_TARGET_RE = re.compile(r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*$")


def normalise(raw) -> str:
    """Return the canonical ``Table[Column] = 'Value'`` string, or ''.

    An empty return means "not a filter I understand" — callers must treat that
    as a refusal, never as "no filter to apply".
    """
    if raw is None:
        return ""
    # A (target, value) pair from the scenario builder.
    if isinstance(raw, (tuple, list)):
        if len(raw) != 2:
            return ""
        target, value = raw
        match = _TARGET_RE.match(str(target))
        if not match:
            return ""
        table, column = match.groups()
        return f"{table}[{column}] = '{str(value)}'"

    # Rebuild rather than pass through, so spacing and DAX table quoting stop
    # mattering: two filters meaning the same thing must compare equal, or
    # deduplication and caching both treat them as different.
    match = FILTER_RE.match(str(raw).strip())
    if not match:
        return ""
    table, column, value = match.groups()
    return f"{table.strip()}[{column.strip()}] = '{value}'"


def normalise_all(filters) -> list[str]:
    """Canonical forms for every filter, dropping only the genuinely empty."""
    return [n for n in (normalise(f) for f in (filters or [])) if n]


def parse(raw) -> tuple[str, str, str] | None:
    """``(table, column, value)`` for a filter in either shape, or None."""
    text = normalise(raw)
    if not text:
        return None
    match = FILTER_RE.match(text)
    if not match:
        return None
    table, column, value = match.groups()
    return table.strip(), column.strip(), value
