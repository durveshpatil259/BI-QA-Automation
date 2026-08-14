"""Resolve model tables and columns onto a SQL Server schema.

The DAX compiler needs four things from whatever it is compiling against:
which physical table backs a model table, which physical column backs a model
column, how to join two tables, and how a filter reaches a given table. For
Excel/CSV those come from :mod:`source_bundle`; this supplies the same four for
a database.

The mapping is deterministic — column-overlap for tables, the same tiered
matcher used everywhere else for columns — so the same dashboard resolves to
the same SQL on every run, and a measure the model would have got wrong is
never asked about.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.logger import get_logger
from src.services.execution.dax_compiler import TSQL

_logger = get_logger()

__all__ = ["SqlResolver"]


@dataclass
class _Field:
    """Mirrors source_bundle.ResolvedField so the compiler sees one shape."""

    dataset: str          # the physical table, e.g. dbo.Sales_data
    column: str           # the physical column
    confidence: float = 1.0
    method: str = ""


class SqlResolver:
    """Deterministic model -> warehouse resolution for the DAX compiler."""

    def __init__(self, metadata, db_schema):
        self._metadata = metadata
        self._schema = db_schema
        self._table_cache: dict[str, str] = {}
        self._map = {}
        if metadata and db_schema:
            from src.services.validation.table_matcher import map_model_tables

            # One deterministic assignment for the whole run: an exactly-named
            # but unrelated table (SalesLT.Customer) otherwise beats the real
            # one (dbo.customer_data) on some measures and not others.
            for m in map_model_tables(metadata, db_schema):
                if m.db_table:
                    self._map[m.model_table.casefold()] = m.db_table

    # --- tables -----------------------------------------------------------
    def table_for(self, model_table: str) -> str:
        name = (model_table or "").strip()
        if not name:
            return ""
        key = name.casefold()
        if key in self._table_cache:
            return self._table_cache[key]
        found = self._map.get(key, "")
        if not found:
            # Fall back to a direct name match, which covers a warehouse whose
            # tables are named exactly as the model's.
            for t in (self._schema.tables if self._schema else []):
                if t.name.casefold() == key or t.full_name.casefold() == key:
                    found = t.full_name
                    break
        self._table_cache[key] = found
        return found

    def _table_object(self, full_name: str):
        for t in (self._schema.tables if self._schema else []):
            if t.full_name == full_name:
                return t
        return None

    def _model_columns(self, model_table: str) -> list[str]:
        for t in (self._metadata.tables if self._metadata else []):
            if t.name.casefold() == (model_table or "").casefold():
                return [c.name for c in t.columns]
        return []

    # --- columns ----------------------------------------------------------
    def resolve(self, model_table: str, model_column: str | None):
        """(table, column) -> a field the compiler can quote, or None."""
        table = self.table_for(model_table)
        if not table:
            return None
        if model_column is None:
            return _Field(table, "", 1.0, "table")

        obj = self._table_object(table)
        if obj is None:
            return None
        from src.services.validation.column_mapper import is_match

        wanted = (model_column or "").strip()
        for c in obj.columns:                      # exact first, always
            if c.name.casefold() == wanted.casefold():
                return _Field(table, c.name, 1.0, "exact")
        for c in obj.columns:                      # then the tiered matcher
            if is_match(wanted, c.name):
                return _Field(table, c.name, 0.8, "matched")
        return None

    # --- joins ------------------------------------------------------------
    def join_condition(self, left_table: str, right_table: str):
        """(left_key, right_key) from declared FKs or the schema's join hints."""
        if not self._schema:
            return None
        for j in (self._schema.join_hints or []):
            for a_t, a_c, b_t, b_c in (
                (j.from_table, j.from_column, j.to_table, j.to_column),
                (j.to_table, j.to_column, j.from_table, j.from_column),
            ):
                if a_t == left_table and b_t == right_table:
                    return a_c, b_c
        left = self._table_object(left_table)
        if left:
            for fk in left.foreign_keys:
                if fk.ref_table == right_table:
                    return fk.column, fk.ref_column
        right = self._table_object(right_table)
        if right:
            for fk in right.foreign_keys:
                if fk.ref_table == left_table:
                    return fk.ref_column, fk.column
        return None

    # --- the three hooks the compiler asks for ----------------------------
    def related_resolver(self):
        """RELATED(Dim[Col]) -> (join template, table, column), or None."""
        def related_for(base_table: str, target_table: str, target_column: str):
            base = self.table_for(base_table)
            field = self.resolve(target_table, target_column)
            if not base or field is None:
                return None
            join = self.join_condition(base, field.dataset)
            if not join:
                return None
            join_sql = (f"JOIN {TSQL.quote(field.dataset)} AS {{alias}} "
                        f"ON base.{TSQL.quote(join[0])} = {{alias}}.{TSQL.quote(join[1])}")
            return join_sql, field.dataset, field.column
        return related_for

    def column_filter_resolver(self):
        """Where a CALCULATE filter lands relative to the table being aggregated."""
        def column_filter(dataset: str, table: str, column: str, alias: str):
            field = self.resolve(table, column)
            if field is None:
                return None
            if field.dataset == dataset:
                return [], f"base.{TSQL.quote(field.column)}", field.dataset, field.column
            join = self.join_condition(dataset, field.dataset)
            if not join:
                # No path: in DAX the filter simply does not reach this table,
                # which is a no-op rather than an error.
                return [], "", field.dataset, field.column
            join_sql = (f"JOIN {TSQL.quote(field.dataset)} AS {alias} "
                        f"ON base.{TSQL.quote(join[0])} = {alias}.{TSQL.quote(join[1])}")
            return [join_sql], f"{alias}.{TSQL.quote(field.column)}", \
                field.dataset, field.column
        return column_filter
