"""Deterministic Excel/CSV execution.

Executes the *same* validation plan as SQL Server mode, but from the plan's
structured intent — aggregation, column, dimension, filters — rather than from
generated SQL. pandas loads the file; DuckDB performs the aggregation, so the
arithmetic is real SQL and reproducible rather than a chain of DataFrame calls.

DuckDB reads the pandas DataFrames directly. Its own Excel reader is an
extension that downloads at runtime, which fails on locked-down machines and
would put a network dependency in the middle of a QA run.

The SQL built here is an internal detail. What the report shows is the source,
sheet, operation and filters — evidence a reader can reproduce by hand.
"""

from __future__ import annotations

import re
import time

from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, ValidationPlanItem
from src.services.execution.base import ExecutionAdapter, ExecutionOutcome
from src.services.execution.source_bundle import SourceBundle

_logger = get_logger()

__all__ = ["DuckDbAdapter"]

#: ``Table[Column] = 'Value'`` — how the plan states a slicer selection.
_FILTER = re.compile(
    r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*(=|==)\s*'?\"?(.*?)'?\"?\s*$"
)


def _quote(name: str) -> str:
    """Quote an identifier for DuckDB; column names contain spaces and dashes."""
    return '"' + str(name).replace('"', '""') + '"'


def _sql_literal(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class DuckDbAdapter(ExecutionAdapter):
    """Runs plan items against Excel/CSV with DuckDB over pandas frames."""

    name = "Excel/CSV"

    #: Recognised aggregations. The plan's ``aggregation`` is free text from the
    #: model ("COUNT(DISTINCT ...)", "Sum"), so it is normalised rather than
    #: trusted verbatim.
    _AGGREGATIONS = (
        ("DISTINCTCOUNT", "COUNT(DISTINCT {col})"),
        ("COUNT DISTINCT", "COUNT(DISTINCT {col})"),
        ("COUNT(DISTINCT", "COUNT(DISTINCT {col})"),
        ("COUNTROWS", "COUNT(*)"),
        ("AVERAGE", "AVG({col})"),
        ("AVG", "AVG({col})"),
        ("SUM", "SUM({col})"),
        ("MIN", "MIN({col})"),
        ("MAX", "MAX({col})"),
        ("COUNT", "COUNT({col})"),
    )

    def __init__(self, config: DatasourceConfig, metadata=None):
        self._config = config
        self._metadata = metadata
        self._bundle: SourceBundle | None = None
        self._load_error = ""

    # --- lifecycle --------------------------------------------------------
    def _source(self) -> SourceBundle | None:
        if self._bundle is None and not self._load_error:
            try:
                self._bundle = SourceBundle.load(self._config)
            except Exception as exc:  # noqa: BLE001 - surfaced as an outcome
                self._load_error = f"Could not read the source file: {exc}"
                _logger.warning(self._load_error)
        return self._bundle

    def is_ready(self) -> tuple[bool, str]:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            return False, "DuckDB is not installed; run: pip install duckdb"
        if self._source() is None:
            return False, self._load_error or "No source data loaded."
        return True, ""

    def describe_source(self) -> str:
        bundle = self._source()
        return bundle.label if bundle else self.name

    # --- plan interpretation ---------------------------------------------
    def _aggregation_sql(self, item: ValidationPlanItem, column: str) -> str | None:
        """Turn the plan's free-text aggregation into a SQL expression.

        The column is qualified with the base alias: a filter join can bring in
        a table sharing the column name (``subject_id`` on both the fact and
        the patient dimension), and an unqualified reference is ambiguous.
        """
        text = (item.aggregation or "").upper()
        for token, template in self._AGGREGATIONS:
            if token in text:
                return template.format(col=f"base.{_quote(column)}")
        return None

    #: A measure that is exactly one aggregate over one column, e.g.
    #: ``SUM(Sales[Sales Amount])``. Anything else cannot be expressed by the
    #: plan's (aggregation, column) pair.
    _SIMPLE_DAX = re.compile(
        r"^\s*(SUM|AVERAGE|AVG|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\s*\(\s*"
        r"'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)\s*$",
        re.IGNORECASE,
    )

    def _measure_dax(self, name: str) -> str:
        if not self._metadata:
            return ""
        for measure in self._metadata.all_measures:
            if (measure.name or "").casefold() == (name or "").casefold():
                return measure.dax_expression or ""
        return ""

    def _verify_against_dax(self, item: ValidationPlanItem) -> str | None:
        """Refuse to compute when the plan does not match the measure's DAX.

        The plan's ``column``/``aggregation`` come from a model that was writing
        SQL, and they lose information: Total Profit is ``SUM(Sales[Profit])``
        but the plan recorded ``SUM`` of ``Sales Amount``, because its SQL did
        the subtraction inline. Executing the plan verbatim returned the sales
        figure and called it profit — a confident, wrong number.

        Returns a reason to refuse, or None when the plan is trustworthy.
        """
        dax = " ".join(self._measure_dax(item.kpi_name).split())
        if not dax:
            return None                      # nothing to check against

        simple = self._SIMPLE_DAX.match(dax)
        if not simple:
            return (
                f"'{item.kpi_name}' is a calculated measure ({dax[:70]}) that "
                "cannot be reduced to a single aggregation over one column. "
                "Complex DAX — manual review required."
            )

        _, _, dax_column = simple.groups()
        from src.services.validation.column_mapper import is_match

        if not is_match(dax_column.strip(), item.column or ""):
            return (
                f"The plan aggregates '{item.column}' but the measure is "
                f"defined over '{dax_column.strip()}'. Refusing to compute a "
                "value the dashboard does not use."
            )
        return None

    def _compile(self, item: ValidationPlanItem):
        """Compile the measure's DAX to SQL over the source, or None."""
        from src.services.execution.dax_compiler import compile_measure

        def resolve(table, column):
            bundle = self._source()
            if bundle is None:
                return None
            if column is None:                     # the table itself
                dataset = bundle.dataset_for(table, self._model_columns(table))
                if not dataset:
                    return None
                from src.services.execution.source_bundle import ResolvedField

                return ResolvedField(dataset, "", 1.0, "table")
            return bundle.resolve(table, column, self._model_columns(table))

        def filter_for(dataset: str):
            """``(joins, clauses)`` applying the scenario's filter to a dataset.

            A DAX filter context narrows every aggregate it can reach. Where no
            relationship connects the filter to this dataset it simply does not
            apply — that is Power BI's behaviour, not an error, and it is why a
            Date slicer leaves a Customer-table count unchanged.
            """
            clauses, joins, _, _, _ = self._filter_clauses(item, dataset)
            return (joins, clauses) if clauses else ([], [])

        return compile_measure(item.kpi_name, self._metadata, resolve,
                               filter_for, self._related_resolver(),
                               self._column_filter_resolver())

    def _column_filter_resolver(self):
        """How a CALCULATE filter on ``Table[Column]`` reaches a dataset.

        Returns ``(joins, ref, dataset, column)`` — the SQL reference to use in
        a predicate, and whatever join is needed to get there. An empty ``ref``
        means the model does not propagate the filter to that dataset, so it is
        a no-op; ``None`` means it could not be placed at all, and the measure
        is refused rather than computed with the filter quietly dropped.
        """
        def column_filter(dataset: str, table: str, column: str, alias: str):
            bundle = self._source()
            if bundle is None:
                return None
            field = bundle.resolve(table, column, self._model_columns(table))
            if field is None:
                return None
            if field.dataset == dataset:
                return [], f"base.{_quote(field.column)}", field.dataset, field.column
            if not self._filter_reaches(table, dataset):
                return [], "", field.dataset, field.column
            join = self._join_condition(dataset, field.dataset)
            if not join:
                return None                  # reachable, but more than one hop
            join_sql = (f'JOIN {_quote(field.dataset)} AS {alias} '
                        f'ON base.{_quote(join[0])} = {alias}.{_quote(join[1])}')
            return ([join_sql], f"{alias}.{_quote(field.column)}",
                    field.dataset, field.column)
        return column_filter

    def _related_resolver(self):
        """RELATED(Dim[Col]) -> (join template, dataset, column), or None."""
        def related_for(base_table: str, target_table: str, target_column: str):
            bundle = self._source()
            if bundle is None:
                return None
            base_dataset = bundle.dataset_for(
                base_table, self._model_columns(base_table))
            target_dataset = bundle.dataset_for(
                target_table, self._model_columns(target_table))
            if not base_dataset or not target_dataset:
                return None
            field = bundle.resolve(target_table, target_column,
                                   self._model_columns(target_table))
            if field is None:
                return None
            join = self._join_condition(base_dataset, target_dataset)
            if not join:
                return None
            join_sql = (f'JOIN {_quote(target_dataset)} AS {{alias}} '
                        f'ON base.{_quote(join[0])} = {{alias}}.{_quote(join[1])}')
            return join_sql, target_dataset, field.column
        return related_for

    def _run_compiled(self, item: ValidationPlanItem, compiled) -> ExecutionOutcome:
        """Execute a compiled measure, filters included.

        The scenario's filter is pushed into every aggregate subquery, because
        a DAX filter context narrows each one independently. Wrapping the
        finished expression instead would filter nothing.
        """
        outcome = ExecutionOutcome()
        described: list[str] = []
        for raw in item.filters or []:
            parsed = _FILTER.match(str(raw))
            if parsed:
                f_table, f_column, _, f_value = parsed.groups()
                described.append(f"{f_table}[{f_column}] = {f_value}")

        parts = [
            f"Source: {self.describe_source()}",
            f"Dataset: {', '.join(compiled.datasets) or 'n/a'}",
            f"Measure: {compiled.description}",
        ]
        if described:
            parts.append("Filters: " + "; ".join(described))
        parts.append("Computed from the model's own DAX")
        outcome.evidence = " · ".join(parts)
        started = time.perf_counter()
        try:
            rows = self._run_sql(compiled.sql, self._source().frames)
        except Exception as exc:  # noqa: BLE001 - surfaced, never raised
            outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            outcome.error = f"Calculation failed: {exc}"
            return outcome
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        value = rows[0][0] if rows and rows[0] else None
        outcome.value = "" if value is None else str(value)
        return outcome

    def _model_columns(self, model_table: str) -> list[str]:
        """Model column names for a table, so table->dataset uses coverage."""
        if not self._metadata:
            return []
        bare = (model_table or "").rsplit(".", 1)[-1].strip()
        from src.domain.models import normalise_table_name

        target = normalise_table_name(bare)
        for table in self._metadata.tables:
            if normalise_table_name(table.name) == target:
                return [c.name for c in table.columns]
        return []

    def _resolve_measure(self, item: ValidationPlanItem):
        bundle = self._source()
        return bundle.resolve(
            item.table, item.column, self._model_columns(item.table)
        ) if bundle else None

    def _model_table_for(self, dataset: str) -> str:
        """Which model table this dataset backs — the reverse of dataset_for."""
        bundle = self._source()
        if bundle is None or not self._metadata:
            return ""
        for table in self._metadata.tables:
            if bundle.dataset_for(table.name, self._model_columns(table.name)) == dataset:
                return table.name
        return ""

    def _filter_reaches(self, filter_table: str, dataset: str) -> bool:
        """Would Power BI propagate this filter to this dataset?

        Relationships are directional and an inactive one carries nothing, so a
        slicer often leaves a measure untouched. That is not an error — it is
        the value the dashboard shows — and treating it as one failed 21
        correct comparisons on a single dashboard.
        """
        if not self._metadata or not filter_table:
            return True                      # cannot reason — assume it applies
        target = self._model_table_for(dataset)
        if not target:
            return True
        from src.services.validation.filter_reach import reachable_tables

        reach = reachable_tables(self._metadata, filter_table)
        known = {(r.from_table or "").casefold() for r in self._metadata.relationships}
        known |= {(r.to_table or "").casefold() for r in self._metadata.relationships}
        if filter_table.casefold() not in known or target.casefold() not in known:
            return True                      # outside the graph — do not guess
        return target.casefold() in reach

    def _filter_clauses(self, item: ValidationPlanItem, dataset: str):
        """(clauses, joins, described) for the item's filters.

        A filter on the measure's own dataset becomes a WHERE clause. One on a
        different dataset needs a join, which is only safe when the model
        declares a relationship — otherwise the filter is reported as
        unapplied rather than silently ignored.
        """
        bundle = self._source()
        clauses: list[str] = []
        joins: list[str] = []
        described: list[str] = []
        unapplied: list[str] = []
        # Filters the model does not propagate to this dataset. Recorded so the
        # evidence can say so, but never an error.
        not_applicable: list[str] = []

        for raw in item.filters or []:
            parsed = _FILTER.match(str(raw))
            if not parsed:
                unapplied.append(str(raw))
                continue
            f_table, f_column, _, f_value = parsed.groups()
            # A filter the model would not propagate here leaves this dataset
            # unchanged — the dashboard's own behaviour, not a failure.
            if not self._filter_reaches(f_table, dataset):
                not_applicable.append(f"{f_table}[{f_column}]")
                continue
            found = bundle.find_column(f_column)
            if not found:
                unapplied.append(str(raw))
                continue
            described.append(f"{f_table}[{f_column}] = {f_value}")

            if found.dataset == dataset:
                clauses.append(
                    f"base.{_quote(found.column)} = {_sql_literal(f_value)}")
                continue

            join = self._join_condition(dataset, found.dataset)
            if not join:
                # Reachable per the relationships but more than one hop away;
                # multi-hop joins are not built yet, so say so rather than
                # returning a number computed without the filter.
                unapplied.append(f"{raw} (needs a multi-step join)")
                described.pop()
                continue
            joins.append(
                f'JOIN {_quote(found.dataset)} AS f{len(joins)} '
                f'ON base.{_quote(join[0])} = f{len(joins)}.{_quote(join[1])}'
            )
            clauses.append(
                f'f{len(joins) - 1}.{_quote(found.column)} = {_sql_literal(f_value)}'
            )
        return clauses, joins, described, unapplied, not_applicable

    def _join_condition(self, left_dataset: str, right_dataset: str):
        """(left_key, right_key) from the model's relationships, or None.

        Each relationship names *model* tables, which are resolved to their
        datasets before comparing. Matching the model name against the file
        name directly only worked when a warehouse happened to name its files
        after the model — a sheet called "RawConsignments" backing a table
        called "Consignment" found no join at all.
        """
        if not self._metadata:
            return None
        bundle = self._source()

        def dataset_of(model_table: str) -> str:
            return bundle.dataset_for(model_table, self._model_columns(model_table))

        for rel in self._metadata.relationships or []:
            pairs = [
                (rel.from_table, rel.from_column, rel.to_table, rel.to_column),
                (rel.to_table, rel.to_column, rel.from_table, rel.from_column),
            ]
            for a_tbl, a_col, b_tbl, b_col in pairs:
                if dataset_of(a_tbl) != left_dataset or dataset_of(b_tbl) != right_dataset:
                    continue
                left = bundle.columns_for(left_dataset, [a_col])[0]
                right = bundle.columns_for(right_dataset, [b_col])[0]
                if left.usable and right.usable:
                    return left.source_field, right.source_field
        return None

    # --- execution --------------------------------------------------------
    def _run_sql(self, sql: str, frames: dict):
        import duckdb

        connection = duckdb.connect()
        try:
            for name, frame in frames.items():
                connection.register(name, frame)
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def execute_scalar(self, item: ValidationPlanItem) -> ExecutionOutcome:
        outcome = ExecutionOutcome()
        ready, why = self.is_ready()
        if not ready:
            outcome.error = why
            return outcome

        # Trust the plan only where it provably matches the measure's own DAX.
        # Where it does not, compile the DAX itself rather than refusing —
        # Python does the arithmetic, so nothing is delegated to a model.
        mismatch = self._verify_against_dax(item)
        if mismatch:
            compiled = self._compile(item)
            if compiled is None:
                outcome.error = mismatch
                return outcome
            return self._run_compiled(item, compiled)

        bundle = self._source()
        field = self._resolve_measure(item)
        if field is None:
            # The measure looks simple but its column is calculated, so it does
            # not exist in the source. The compiler can expand the formula —
            # including RELATED() lookups — so try that before giving up.
            compiled = self._compile(item)
            if compiled is not None:
                return self._run_compiled(item, compiled)
            outcome.error = (
                f"Could not find a source column for {item.table}[{item.column}]. "
                "Check the uploaded file contains that data."
            )
            return outcome

        aggregation = self._aggregation_sql(item, field.column)
        if aggregation is None:
            outcome.error = (
                f"Aggregation '{item.aggregation}' is not supported for file "
                "sources. Supported: SUM, AVG, MIN, MAX, COUNT, DISTINCTCOUNT."
            )
            return outcome

        clauses, joins, described, unapplied, skipped = self._filter_clauses(
            item, field.dataset)
        sql = f'SELECT {aggregation} FROM {_quote(field.dataset)} AS base'
        if joins:
            sql += " " + " ".join(joins)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)

        outcome.evidence = self._evidence(item, field, described, unapplied, skipped)
        started = time.perf_counter()
        try:
            rows = self._run_sql(sql, bundle.frames)
        except Exception as exc:  # noqa: BLE001 - surfaced, never raised
            outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            outcome.error = f"Calculation failed: {exc}"
            return outcome
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        if unapplied:
            outcome.error = (
                "Filter(s) could not be applied to the source: "
                + "; ".join(unapplied)
                + ". The result would ignore them, so it is not compared."
            )
            return outcome

        value = rows[0][0] if rows and rows[0] else None
        outcome.value = "" if value is None else str(value)
        return outcome

    def _resolve_dimension(self, item: ValidationPlanItem):
        """Locate the chart's category column.

        ``dimension_column`` may arrive bare ("Category"), dotted
        ("Product.Category") or bracketed ("Product[Category]"), so the table
        part is stripped before looking the column up.
        """
        raw = (item.dimension_column or "").strip()
        if not raw:
            return None
        column = raw
        if "[" in raw:
            column = raw[raw.index("[") + 1:].rstrip("]")
        elif "." in raw:
            column = raw.rsplit(".", 1)[-1]

        bundle = self._source()
        # Prefer the table the plan named; fall back to searching every dataset,
        # since a chart's dimension often lives on a dimension table.
        if item.table:
            found = bundle.resolve(item.table, column, self._model_columns(item.table))
            if found:
                return found
        found = bundle.find_column(column)
        if found:
            return found
        # Charts often group by a *calculated* column — an age band, a
        # length-of-stay bucket — which exists only in the model. Compile the
        # formula so the chart can still be grouped and compared.
        return self._compiled_dimension(item.table, column)

    def _compiled_dimension(self, table: str, column: str):
        """A calculated column expressed as SQL, packaged like a resolved field."""
        if not self._metadata or not table:
            return None
        from src.services.execution.dax_compiler import _Compiler
        from src.services.execution.source_bundle import ResolvedField

        bundle = self._source()
        dataset = bundle.dataset_for(table, self._model_columns(table)) if bundle else ""
        if not dataset:
            return None
        compiler = _Compiler(
            self._metadata,
            lambda t, c: (bundle.resolve(t, c, self._model_columns(t))
                          if c is not None else None),
            related_for=self._related_resolver(),
        )
        formula = compiler._calculated_column(table, column)
        if not formula:
            return None
        expression, joins = compiler._column_formula(table, formula)
        if expression is None:
            return None
        field = ResolvedField(dataset, column, 1.0, "calculated")
        # Carried alongside so execute_grouped can select and group by the
        # expression instead of a column that does not exist in the file.
        field.expression = expression          # type: ignore[attr-defined]
        field.joins = joins                    # type: ignore[attr-defined]
        return field

    def execute_grouped(
        self, item: ValidationPlanItem, *, max_rows: int = 500
    ) -> ExecutionOutcome:
        """Category set (structural) or category totals (grouped).

        A structural item carries no measure — the chart's numbers were never
        rendered — so only the distinct categories can be checked. A grouped
        item aggregates the measure per category.
        """
        outcome = ExecutionOutcome()
        ready, why = self.is_ready()
        if not ready:
            outcome.error = why
            return outcome

        bundle = self._source()
        dimension = self._resolve_dimension(item)
        if dimension is None:
            outcome.error = (
                f"Could not find a source column for the chart's dimension "
                f"'{item.dimension_column or '(unspecified)'}'."
            )
            return outcome

        # A measure column is optional: without one this is a category-set check.
        measure = None
        aggregation = None
        if item.column and item.aggregation:
            mismatch = self._verify_against_dax(item)
            if mismatch:
                outcome.error = mismatch
                return outcome
            measure = self._resolve_measure(item)
            if measure is None:
                outcome.error = (
                    f"Could not find a source column for {item.table}[{item.column}]."
                )
                return outcome
            aggregation = self._aggregation_sql(item, measure.column)
            if aggregation is None:
                outcome.error = (
                    f"Aggregation '{item.aggregation}' is not supported for file sources."
                )
                return outcome

        base = measure.dataset if measure else dimension.dataset
        clauses, joins, described, unapplied, skipped = self._filter_clauses(item, base)

        if measure is None:
            dim_expr = getattr(dimension, "expression", None)                 or f"base.{_quote(dimension.column)}"
            sql = (f'SELECT DISTINCT {dim_expr} '
                   f'FROM {_quote(dimension.dataset)} AS base')
            for extra in getattr(dimension, "joins", None) or []:
                sql += " " + extra.format(alias=f"c{joins and len(joins) or 0}")
        else:
            dim_ref = getattr(dimension, "expression", None)                 or f"base.{_quote(dimension.column)}"
            # A compiled dimension is an expression over the base table, so it
            # brings its own joins and needs no relationship hop.
            for index, extra in enumerate(getattr(dimension, "joins", None) or []):
                joins.append(extra.format(alias=f"c{index}"))
            if getattr(dimension, "expression", None):
                pass
            elif dimension.dataset != base:
                join = self._join_condition(base, dimension.dataset)
                if not join:
                    outcome.error = (
                        f"The chart groups by {dimension.dataset}.{dimension.column} "
                        f"but no relationship links it to {base}."
                    )
                    return outcome
                alias = f"d{len(joins)}"
                joins.append(
                    f'JOIN {_quote(dimension.dataset)} AS {alias} '
                    f'ON base.{_quote(join[0])} = {alias}.{_quote(join[1])}'
                )
                dim_ref = f"{alias}.{_quote(dimension.column)}"
            sql = (
                f'SELECT {dim_ref} AS dim, {aggregation} AS val '
                f'FROM {_quote(base)} AS base'
            )

        if joins:
            sql += " " + " ".join(joins)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if measure is not None:
            sql += f" GROUP BY {dim_ref} ORDER BY 1"
        else:
            sql += " ORDER BY 1"
        sql += f" LIMIT {int(max_rows)}"

        outcome.evidence = self._chart_evidence(
            item, dimension, measure, described, unapplied, skipped)
        started = time.perf_counter()
        try:
            rows = self._run_sql(sql, bundle.frames)
        except Exception as exc:  # noqa: BLE001 - surfaced, never raised
            outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            outcome.error = f"Calculation failed: {exc}"
            return outcome
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        if unapplied:
            outcome.error = (
                "Filter(s) could not be applied to the source: "
                + "; ".join(unapplied)
                + ". The result would ignore them, so it is not compared."
            )
            return outcome

        # The engine reads row[0] as the category and row[1] as the value.
        outcome.rows = [
            ["" if cell is None else str(cell) for cell in row] for row in rows
        ]
        return outcome

    def _chart_evidence(self, item, dimension, measure, described,
                        unapplied, skipped=()) -> str:
        parts = [
            f"Source: {self.describe_source()}",
            f"Dataset: {(measure or dimension).dataset}",
            f"Group by: {dimension.column}",
        ]
        if measure is not None:
            parts.append(f"Operation: {(item.aggregation or '').strip()}")
            parts.append(f"Column: {measure.column}")
        else:
            parts.append("Operation: DISTINCT (categories only — chart values "
                         "were not rendered)")
        if described:
            parts.append("Filters: " + "; ".join(described))
        if skipped:
            parts.append(
                "Not applicable here (no relationship path): " + "; ".join(skipped))
        if unapplied:
            parts.append("Unapplied filters: " + "; ".join(unapplied))
        return " · ".join(parts)

    # --- reporting --------------------------------------------------------
    def _evidence(self, item, field, described, unapplied, skipped=()) -> str:
        """Human-reproducible proof — never the internal SQL."""
        parts = [
            f"Source: {self.describe_source()}",
            f"Dataset: {field.dataset}",
            f"Operation: {(item.aggregation or '').strip() or 'SUM'}",
            f"Column: {field.column}",
        ]
        if described:
            parts.append("Filters: " + "; ".join(described))
        if field.confidence < 1.0:
            parts.append(f"Column match: {field.method} ({field.confidence:.0%})")
        if skipped:
            parts.append(
                "Not applicable here (no relationship path): " + "; ".join(skipped))
        if unapplied:
            parts.append("Unapplied filters: " + "; ".join(unapplied))
        return " · ".join(parts)
