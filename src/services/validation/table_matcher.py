"""Deterministic model-table -> database-table mapping.

Which warehouse table backs a model table is a *factual* question the schema
already answers, so Python decides it rather than the AI guessing.

The guess used to go wrong in a very specific way: a database holding both the
real warehouse (``dbo.customer_data``) and an unrelated sample schema
(``SalesLT.Customer``) offers the AI an exact name match that is entirely the
wrong table. Comparing column names instead settles it — ``customer_data``
shares 7 columns with the model's Customer table, ``SalesLT.Customer`` shares 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.domain.models import DashboardMetadata, DbSchema, DbTable, normalise_table_name

__all__ = ["TableMatch", "map_model_tables", "format_table_map"]

#: Model tables Power BI generates for time intelligence — never in a warehouse.
_INTERNAL_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")


def _norm_column(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


@dataclass
class TableMatch:
    model_table: str
    db_table: str
    shared_columns: int
    candidates: int

    @property
    def confident(self) -> bool:
        """At least one column in common, or the only candidate."""
        return self.shared_columns > 0 or self.candidates == 1


def map_model_tables(
    metadata: DashboardMetadata, schema: DbSchema
) -> list[TableMatch]:
    """Best database table for each model table, ranked by column overlap."""
    matches: list[TableMatch] = []
    for model_table in metadata.tables:
        if model_table.name.startswith(_INTERNAL_PREFIXES):
            continue
        key = normalise_table_name(model_table.name)
        candidates: list[DbTable] = [
            t for t in schema.tables if normalise_table_name(t.name) == key
        ]
        if not candidates:
            continue

        model_columns = {_norm_column(c.name) for c in model_table.columns}
        scored = sorted(
            (
                (len(model_columns & {_norm_column(c.name) for c in t.columns}),
                 # Stable, and prefers the shorter/plainer name on a tie.
                 -len(t.full_name), t)
                for t in candidates
            ),
            key=lambda row: (row[0], row[1]),
            reverse=True,
        )
        best_score, _, best = scored[0]
        matches.append(
            TableMatch(model_table.name, best.full_name, best_score, len(candidates))
        )
    return matches


def format_table_map(matches: list[TableMatch]) -> str:
    """Render the mapping as a prompt section. Empty when nothing is confident."""
    usable = [m for m in matches if m.confident]
    if not usable:
        return ""
    lines = [
        "MODEL TABLE -> DATABASE TABLE (already resolved — use exactly these):"
    ]
    for m in usable:
        note = ""
        if m.candidates > 1:
            note = f"   (chosen over {m.candidates - 1} similarly-named table(s))"
        lines.append(f"  {m.model_table} -> {m.db_table}{note}")
    lines.append(
        "Do NOT substitute a different table even if its name looks closer to "
        "the model's."
    )
    return "\n".join(lines)
