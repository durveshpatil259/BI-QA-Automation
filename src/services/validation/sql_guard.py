"""Shared read-only SQL guard.

A single source of truth for "is this a safe, single read-only statement?" —
used both when executing queries (connector) and when validating AI-generated
SQL in a validation plan. Only a single ``SELECT``/``WITH`` statement is allowed;
batches and any write/DDL verb are rejected.
"""

from __future__ import annotations

import re

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|merge|exec|execute|grant|revoke)\b",
    re.IGNORECASE,
)


def is_read_only(sql: str) -> bool:
    """Return True if *sql* is a single read-only SELECT/WITH statement."""
    q = (sql or "").strip().rstrip(";")
    if not q or ";" in q:
        return False
    return bool(_READ_ONLY.match(q)) and not _FORBIDDEN.search(q)


#: ``FORMAT(x, '0.0%')`` already scales by 100, so an explicit ``100 *`` as well
#: multiplies the answer by 10,000 — and it fails *silently*, returning a
#: plausible-looking number rather than an error.
_DOUBLE_PERCENT = re.compile(
    r"FORMAT\s*\([^()]*?\b100(?:\.0+)?\s*\*.*?,\s*'[^']*%'\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def double_percent_scaling(sql: str) -> bool:
    """True when a query multiplies by 100 *and* uses a percent format code."""
    return bool(_DOUBLE_PERCENT.search(sql or ""))
