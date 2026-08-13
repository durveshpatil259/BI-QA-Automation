"""SQL Server execution — the existing behaviour, behind the adapter interface.

Deliberately a thin wrapper: the guards, timing and error strings are the ones
that were already in :mod:`src.services.sql_validation_engine`, moved rather
than rewritten, so the SQL Server path behaves identically.
"""

from __future__ import annotations

import time

from src.core.logger import get_logger
from src.domain.models import ValidationPlanItem
from src.services.execution.base import ExecutionAdapter, ExecutionOutcome
from src.services.validation.sql_guard import double_percent_scaling, is_read_only

_logger = get_logger()


class SqlServerAdapter(ExecutionAdapter):
    """Runs the AI-generated SQL read-only against the configured database."""

    name = "SQL Server"

    def __init__(self, connector, db_schema=None):
        self._connector = connector
        self._schema = db_schema

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
        ready, why = self.is_ready()
        if not ready:
            outcome.error = why
            return outcome
        rejected = self._reject(item)
        if rejected:
            outcome.error = rejected
            return outcome

        started = time.perf_counter()
        result = self._connector.run_query(item.generated_sql, sample_rows=sample_rows)
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
