"""Verify AI-generated SQL only references identifiers that actually exist.

The LLM is deliberately given **names and types only** — never column
contents — so it cannot confirm anything about the data it is writing SQL
against. That makes a Python-side check essential: before a generated query is
executed, every table and column it mentions is matched against the real schema
read by :class:`~src.services.schema_service.SchemaService`.

This catches hallucinated identifiers locally, at zero cost, instead of letting
them reach the database and fail there (or worse, silently return the wrong
thing from a mistyped-but-valid column).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.domain.models import DbSchema

# FROM/JOIN <name> — captures dbo.Table, [dbo].[Table] or bare Table.
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:\[[^\]]+\]|[A-Za-z_][\w$]*)"
    r"(?:\s*\.\s*(?:\[[^\]]+\]|[A-Za-z_][\w$]*))*)",
    re.IGNORECASE,
)
# [Bracketed Column] references — the reliable signal for column names.
_BRACKETED = re.compile(r"\[([^\]]+)\]")

# Words that appear in brackets but are not columns.
_SQL_NOISE = {
    "select", "from", "where", "group", "by", "order", "having", "join", "on",
    "as", "and", "or", "not", "null", "case", "when", "then", "else", "end",
    "sum", "avg", "min", "max", "count", "distinct", "top", "inner", "left",
    "right", "outer", "full", "cast", "convert", "round", "concat", "format",
    "nullif", "isnull", "coalesce", "n0", "n2",
}


@dataclass
class IdentifierCheck:
    ok: bool
    unknown_tables: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        parts = []
        if self.unknown_tables:
            parts.append(f"unknown table(s): {', '.join(self.unknown_tables)}")
        if self.unknown_columns:
            parts.append(f"unknown column(s): {', '.join(self.unknown_columns)}")
        return "Generated SQL references " + "; ".join(parts) if parts else ""


def _strip(name: str) -> str:
    return name.strip().strip("[]").strip().casefold()


def _bare(qualified: str) -> str:
    """Last segment of a dotted name: ``[dbo].[Sales]`` -> ``sales``."""
    parts = [p for p in re.split(r"\s*\.\s*", qualified) if p.strip()]
    return _strip(parts[-1]) if parts else ""


def check_identifiers(sql: str, schema: DbSchema | None) -> IdentifierCheck:
    """Return which tables/columns in *sql* are absent from *schema*.

    Deliberately conservative: only clearly-resolvable references are judged,
    because a false rejection is worse than a missed one — the query would
    still fail loudly at execution time.
    """
    if not sql or schema is None or not schema.tables:
        return IdentifierCheck(ok=True)

    known_tables = {t.name.casefold() for t in schema.tables}
    known_tables |= {t.full_name.casefold() for t in schema.tables}
    known_columns = {c.name.casefold() for t in schema.tables for c in t.columns}

    unknown_tables = []
    for ref in _TABLE_REF.findall(sql):
        name = _bare(ref)
        # Derived tables/CTEs and aliases are not schema objects.
        if name and name not in known_tables and _strip(ref) not in known_tables:
            unknown_tables.append(ref.strip())

    unknown_columns = []
    for token in _BRACKETED.findall(sql):
        name = _strip(token)
        if not name or name in _SQL_NOISE or name in known_columns:
            continue
        # A bracketed token may be a table name (e.g. [Sales_data]).
        if name in known_tables:
            continue
        unknown_columns.append(token.strip())

    return IdentifierCheck(
        ok=not unknown_tables and not unknown_columns,
        unknown_tables=sorted(set(unknown_tables)),
        unknown_columns=sorted(set(unknown_columns)),
    )
