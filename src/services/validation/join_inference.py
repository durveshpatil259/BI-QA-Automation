"""Infer join paths between tables from column naming.

Star-schema tables loaded from CSV/flat files usually have **no declared
foreign keys**, which leaves an AI with no way to know that
``Sales_data.OrderDateKey`` joins to ``date_data.DateKey``. This module
reconstructs those paths deterministically from key-column naming so the join
information can be handed to the model.

Rules (in priority order), applied only to key-like columns:

1. **Exact match** — ``Sales_data.ProductKey`` = ``product_data.ProductKey``.
2. **Suffix match** — ``Sales_data.OrderDateKey`` ends with ``DateKey``, the
   key column of ``date_data``.

The target column must be a primary key, or the table's most key-like column,
so facts point at dimensions rather than the reverse.
"""

from __future__ import annotations

from src.domain.models import DbSchema, DbTable, JoinHint

_KEY_SUFFIXES = ("key", "id", "no", "code")


def _is_key_like(name: str) -> bool:
    n = name.strip().casefold()
    return any(n.endswith(s) for s in _KEY_SUFFIXES)


def _key_columns(table: DbTable) -> list[str]:
    """Candidate identifying columns of *table*, best first."""
    pks = [c.name for c in table.columns if c.is_primary_key]
    if pks:
        return pks
    return [c.name for c in table.columns if _is_key_like(c.name)]


def infer_join_hints(schema: DbSchema, max_hints: int = 60) -> list[JoinHint]:
    """Return declared FKs plus inferred join paths, de-duplicated."""
    hints: list[JoinHint] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(ft, fc, tt, tc, inferred):
        key = (ft.casefold(), fc.casefold(), tt.casefold(), tc.casefold())
        rev = (tt.casefold(), tc.casefold(), ft.casefold(), fc.casefold())
        if key in seen or rev in seen:
            return
        seen.add(key)
        hints.append(JoinHint(from_table=ft, from_column=fc,
                              to_table=tt, to_column=tc, inferred=inferred))

    # 1) Declared foreign keys always win.
    for t in schema.tables:
        for fk in t.foreign_keys:
            add(t.full_name, fk.column, fk.ref_table, fk.ref_column, False)

    # 2) Infer the rest from naming.
    targets: list[tuple[DbTable, str]] = [
        (t, col) for t in schema.tables for col in _key_columns(t)
    ]
    for source in schema.tables:
        for col in source.columns:
            if not _is_key_like(col.name):
                continue
            cn = col.name.casefold()
            for target, tcol in targets:
                if target.full_name.casefold() == source.full_name.casefold():
                    continue
                tn = tcol.casefold()
                # exact match, or the source column ends with the target key
                # (OrderDateKey -> DateKey) but is not identical noise.
                if cn == tn or (len(tn) >= 4 and cn.endswith(tn)):
                    add(source.full_name, col.name, target.full_name, tcol, True)
                    break
            if len(hints) >= max_hints:
                return hints
    return hints
