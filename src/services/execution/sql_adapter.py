"""SQL Server execution — the existing behaviour, behind the adapter interface.

Deliberately a thin wrapper: the guards, timing and error strings are the ones
that were already in :mod:`src.services.sql_validation_engine`, moved rather
than rewritten, so the SQL Server path behaves identically.
"""

from __future__ import annotations

import re
import time

from src.core.logger import get_logger
from src.domain.models import ValidationPlanItem
from src.services.execution.base import ExecutionAdapter, ExecutionOutcome
from src.services.validation import filter_spec
from src.services.validation.sql_guard import double_percent_scaling, is_read_only

_logger = get_logger()

#: ``Table[Column] = 'Value'`` — how the plan states a slicer selection.
_FILTER = re.compile(
    r"^\s*'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*(=|==)\s*'?\"?(.*?)'?\"?\s*$"
)


class _CannotFilter(Exception):
    """A scenario filter that cannot be placed, so the measure is not compiled.

    Falling back to the generated SQL is right here: a compiled measure missing
    one of its filters would return a confident number for the wrong slice.
    """


class SqlServerAdapter(ExecutionAdapter):
    """Runs the AI-generated SQL read-only against the configured database."""

    name = "SQL Server"

    def __init__(self, connector, db_schema=None, metadata=None):
        self._connector = connector
        self._schema = db_schema
        self._metadata = metadata
        self._resolver = None

    # --- compile from DAX in preference to the model's SQL ----------------
    def compile(self, item: ValidationPlanItem):
        """The measure's own DAX as T-SQL, or None when out of the grammar.

        Time-intelligence and ratio measures are where generated SQL goes wrong
        most often — on a real run ``YoY%`` came back as
        ``(SUM(x) - (SELECT SUM(x) FROM same_table)) / …``, which is zero by
        construction and matched a dashboard that also read 0.0%. Compiling the
        DAX removes the guesswork for exactly those measures, and anything the
        grammar cannot express still falls back to the generated SQL.
        """
        if not self._metadata or not self._schema or not item.kpi_name:
            return None
        from src.services.execution.dax_compiler import TSQL, compile_measure
        from src.services.execution.sql_resolver import SqlResolver

        if self._resolver is None:
            self._resolver = SqlResolver(self._metadata, self._schema)
        resolver = self._resolver

        def filter_for(dataset: str):
            joins, clauses = [], []
            for raw in item.filters or []:
                parsed = filter_spec.parse(raw)
                if parsed is None:
                    # Never skip: a dropped filter turns a filtered measure
                    # into the unfiltered total, which looks like a result.
                    raise _CannotFilter(f"unrecognised filter {raw!r}")
                f_table, f_column, f_value = parsed
                located = resolver.column_filter_resolver()(
                    dataset, f_table, f_column, f"f{len(joins)}")
                if located is None:
                    raise _CannotFilter(f"{f_table}[{f_column}]")
                extra, ref, _, _ = located
                if not ref:
                    continue                 # not propagated here; a no-op
                joins.extend(extra)
                clauses.append(f"{ref} = '{str(f_value).replace(chr(39), chr(39) * 2)}'")
            return joins, clauses

        try:
            return compile_measure(
                item.kpi_name, self._metadata, resolver.resolve, filter_for,
                resolver.related_resolver(), resolver.column_filter_resolver(),
                dialect=TSQL,
            )
        except _CannotFilter as exc:
            _logger.info("Not compiling '%s': filter %s cannot be placed.",
                         item.kpi_name, exc)
            return None

    def is_ready(self) -> tuple[bool, str]:
        if self._connector is None:
            return False, "No database connection is configured."
        return True, ""

    # --- shared guards ---------------------------------------------------
    def _reject(self, item: ValidationPlanItem) -> str | None:
        """Why this SQL must not run, or None when it is safe."""
        if not item.generated_sql or not is_read_only(item.generated_sql):
            return "Generated SQL is missing or not a single read-only SELECT."
        if double_percent_scaling(item.generated_sql):
            return (
                "Generated SQL multiplies by 100 and also uses a '%' format "
                "code, which scales the result by 10,000. Rejected before "
                "execution because it would return a wrong number, not an error."
            )
        if self._schema is not None:
            from src.services.validation.identifier_guard import check_identifiers

            check = check_identifiers(item.generated_sql, self._schema)
            if not check.ok:
                return check.reason
        return None

    def _run(self, item: ValidationPlanItem, sample_rows: int) -> ExecutionOutcome:
        outcome = ExecutionOutcome(evidence=item.generated_sql)

        # Prefer the compiled measure. It is derived from the model's own DAX
        # rather than restated by a model, so it cannot disagree with what the
        # dashboard computes.
        compiled = self.compile(item) if item.item_type == "scalar" else None
        sql = item.generated_sql
        if compiled is not None:
            sql = compiled.sql
            # Show BOTH the DAX and the SQL it became. The DAX alone says where
            # the number came from but leaves a reader unable to reproduce it —
            # and "compiled from DAX" reads as though no query ran at all. The
            # query is the evidence; the DAX is why that query is the right one.
            outcome.evidence = (
                f"Compiled from the model's own DAX: {compiled.description}\n"
                f"{sql}"
            )
            _logger.info("Compiled '%s' from DAX instead of using generated SQL.",
                         item.kpi_name)

        ready, why = self.is_ready()
        if not ready:
            outcome.error = why
            return outcome
        if compiled is None:
            rejected = self._reject(item)
            if rejected:
                outcome.error = rejected
                return outcome

        started = time.perf_counter()
        result = self._connector.run_query(sql, sample_rows=sample_rows)
        outcome.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if result.error:
            outcome.error = f"SQL execution error: {result.error}"
            return outcome
        outcome.rows = result.sample_rows or []
        outcome.value = result.scalar_value or (
            outcome.rows[0][0] if outcome.rows and outcome.rows[0] else ""
        )
        return outcome

    def execute_scalar(self, item: ValidationPlanItem) -> ExecutionOutcome:
        return self._run(item, sample_rows=1)

    def execute_grouped(
        self, item: ValidationPlanItem, *, max_rows: int = 500
    ) -> ExecutionOutcome:
        return self._run(item, sample_rows=max_rows)

    def describe_source(self) -> str:
        config = getattr(self._connector, "config", None)
        if config is None:
            return self.name
        return f"{config.server} · {config.database}".strip(" ·") or self.name
