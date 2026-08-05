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
