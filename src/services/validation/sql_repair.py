"""Deterministic repair of structurally broken generated SQL.

A model asked for fifteen queries in one reply compresses, and the first thing
it drops is the clause it considers obvious: ``SELECT SUM(F.[Sales Amount])``
with no ``FROM`` at all. The statement is syntactically plausible, passes the
read-only guard, and then fails at execution with "the multi-part identifier
could not be bound" — 18 of 164 tests on a real run.

Asking the model to try again costs another round-trip and may fail the same
way. The plan item already records which table the KPI is aggregated over, so
Python can supply the missing clause itself, exactly and for free.

Only structural repairs belong here. Nothing in this module changes what a
query *computes* — a repair that altered the arithmetic would turn a visible
failure into a silent wrong answer, which is far worse.
"""

from __future__ import annotations

import re

from src.core.logger import get_logger

_logger = get_logger()

__all__ = ["needs_from_clause", "repair_sql"]

#: ``F.[Sales Amount]`` or ``F.SalesAmount`` — a qualified column reference.
_ALIAS_REF = re.compile(r"\b([A-Za-z]\w*)\s*\.\s*[\[\w]")
_QUOTED = re.compile(r"'[^']*'")


def _strip_subqueries(sql: str) -> str:
    """Remove ``( SELECT … )`` groups, leaving everything else in place.

    Only *subqueries* may be removed, not every parenthesised group. A whole
    projection is routinely wrapped in ``FORMAT(ROUND(SUM(F.[x])…))``, so
    dropping all parens deletes the very alias reference this module exists to
    find — and the repair then omits the alias and fails identically.
    """
    text = sql
    while True:
        match = re.search(r"\(\s*SELECT\b", text, re.IGNORECASE)
        if not match:
            return text
        depth, end = 0, len(text)
        for i in range(match.start(), len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        else:
            return text[:match.start()]     # unbalanced: drop the remainder
        text = text[:match.start()] + " " + text[end:]


def needs_from_clause(sql: str) -> bool:
    """True when the outer SELECT has no FROM of its own."""
    text = (sql or "").strip()
    if not text.upper().startswith("SELECT"):
        return False
    top = _strip_subqueries(_QUOTED.sub("''", text))
    return not re.search(r"\bFROM\b", top, re.IGNORECASE)


def _outer_aliases(sql: str) -> set[str]:
    """Alias prefixes used outside any subquery."""
    top = _QUOTED.sub("''", _strip_subqueries(sql))
    # Exclude SQL keywords that can precede a dot-free identifier.
    return {
        a for a in _ALIAS_REF.findall(top)
        if a.upper() not in {"SELECT", "FROM", "WHERE", "AS", "ON", "AND", "OR"}
    }


def repair_sql(sql: str, table: str) -> tuple[str, str]:
    """Return ``(sql, note)``. *note* is empty when nothing needed doing.

    The only repair performed is supplying a missing top-level ``FROM``, using
    the table the plan already assigned to this KPI and binding whatever alias
    the statement refers to.
    """
    text = (sql or "").strip()
    if not text or not table or not needs_from_clause(text):
        return sql, ""

    aliases = _outer_aliases(text)
    if len(aliases) > 1:
        # Two unbound aliases means a join was intended and its shape is not
        # recoverable from the plan. Guessing one would silently change the
        # result set; leave it to fail visibly.
        return sql, ""

    clause = f" FROM {table}" + (f" AS {next(iter(aliases))}" if aliases else "")
    # Anything trailing the projection (ORDER BY, GROUP BY) must stay last.
    tail = re.search(r"\b(GROUP\s+BY|ORDER\s+BY|HAVING)\b", text, re.IGNORECASE)
    repaired = (text[:tail.start()].rstrip() + clause + " " + text[tail.start():]
                if tail else text.rstrip().rstrip(";") + clause)

    note = f"added missing FROM {table}"
    _logger.info("SQL repair: %s | %s", note, " ".join(text.split())[:90])
    return repaired, note
