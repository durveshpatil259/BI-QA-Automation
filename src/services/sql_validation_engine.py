"""SQL execution + comparison engine (redesign V5).

Executes each validation-plan item's generated SQL (read-only, with timing and
status), parses the scalar result, and compares it to the dashboard value using
the deterministic tolerance engine (V1) — producing PASS/FAIL. This is ALL
Python: the AI never executes SQL. The AI may afterwards *explain* failures.
"""

from __future__ import annotations

import time

from src.core.constants import DatasourceType, TestStatus
from src.core.exceptions import LLMError, ValidationError
from src.core.logger import get_logger
from src.domain.models import (
    DatasourceConfig,
    DataValidationRun,
    LLMSettings,
    Project,
    SqlValidationResult,
    ValidationPlanItem,
)
from src.services.datasources import create_connector
from src.services.validation import compare_values, parse_value
from src.services.validation.sql_guard import is_read_only
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class SqlValidationEngine:
    """Runs the validation plan against the datasource and compares values."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load(self, project: Project) -> DataValidationRun | None:
        return self._repo.load_data_validation(project)

    # --- execution + comparison ------------------------------------------
    def run(
        self,
        project: Project,
        config: DatasourceConfig | None = None,
        *,
        tolerance_pct: float = 1.0,
    ) -> DataValidationRun:
        plan = self._repo.load_validation_plan(project)
        if not plan or not plan.items:
            raise ValidationError(
                "No validation plan found. Generate it in Analysis Step 5 first."
            )
        config = config or self._repo.load_datasource(project)
        if config is None or not config.is_configured:
            raise ValidationError("No datasource configured.")

        excel = config.type == DatasourceType.EXCEL
        connector = None if excel else create_connector(config)

        run = DataValidationRun()
        for i, item in enumerate(plan.items, start=1):
            run.results.append(
                self._validate_item(f"QA_{i:03d}", item, connector, tolerance_pct, excel)
            )

        self._repo.save_data_validation(project, run)
        _logger.info("SQL validation for %s: %s", project.id, run.summary())
        return run

    def _validate_item(
        self, test_id, item: ValidationPlanItem, connector, tolerance_pct, excel
    ) -> SqlValidationResult:
        dashboard_numeric, _ = parse_value(item.dashboard_value)
        result = SqlValidationResult(
            test_id=test_id,
            kpi_name=item.kpi_name,
            dashboard_value=item.dashboard_value,
            dashboard_numeric=dashboard_numeric,
            generated_sql=item.generated_sql,
            tolerance_pct=tolerance_pct,
            confidence=item.confidence,
        )

        # Guard rails before touching the datasource.
        if excel:
            result.execution_status = "error"
            result.status = TestStatus.FAIL
            result.reason = "SQL execution is not supported for an Excel datasource."
            return result
        if not item.generated_sql or not is_read_only(item.generated_sql):
            result.execution_status = "error"
            result.status = TestStatus.FAIL
            result.reason = "Generated SQL is missing or not a single read-only SELECT."
            return result

        # Execute (Python), timed.
        t0 = time.perf_counter()
        query_result = connector.run_query(item.generated_sql, sample_rows=1)
        result.execution_time_ms = round((time.perf_counter() - t0) * 1000, 2)

        if query_result.error:
            result.execution_status = "error"
            result.status = TestStatus.FAIL
            result.reason = f"SQL execution error: {query_result.error}"
            return result

        result.execution_status = "ok"
        db_value = query_result.scalar_value or (
            query_result.sample_rows[0][0]
            if query_result.sample_rows and query_result.sample_rows[0] else ""
        )
        result.database_value = str(db_value)
        result.database_numeric, _ = parse_value(db_value)

        # Compare (Python).
        if dashboard_numeric is None:
            result.status = TestStatus.NOT_EXECUTED
            result.reason = (
                "No dashboard value available to compare; database value recorded."
            )
            return result

        outcome = compare_values(
            dashboard_numeric, result.database_numeric, tolerance_pct=tolerance_pct
        )
        result.difference = outcome.difference_display
        result.difference_pct = outcome.difference_pct
        result.status = TestStatus.PASS if outcome.passed else TestStatus.FAIL
        result.reason = outcome.reason
        return result

    # --- optional AI failure explanations --------------------------------
    def explain_failures(
        self, project: Project, settings: LLMSettings | None = None
    ) -> DataValidationRun:
        """Ask the AI to explain failing results and fill in recommendations."""
        run = self._repo.load_data_validation(project)
        if not run:
            raise ValidationError("Run SQL validation first.")
        failures = [
            r for r in run.results
            if r.status == TestStatus.FAIL and not r.recommendation
        ]
        if not failures:
            return run

        settings = settings or (self._repo.load_llm_settings(project) or LLMSettings())
        from src.services.llm import create_client
        from src.services.llm.json_utils import extract_json
        from src.services.llm.prompt_builder import (
            EXPLAIN_SYSTEM_PROMPT,
            build_explain_user_prompt,
        )

        client = create_client(settings)
        try:
            response = client.complete(
                EXPLAIN_SYSTEM_PROMPT, build_explain_user_prompt(failures)
            )
        except LLMError as exc:
            _logger.warning("Failure explanation call failed: %s", exc)
            return run

        data = extract_json(response.content)
        explanations = data.get("explanations", data) if isinstance(data, dict) else data
        by_id = {r.test_id: r for r in run.results}
        if isinstance(explanations, list):
            for e in explanations:
                if not isinstance(e, dict):
                    continue
                target = by_id.get(str(e.get("test_id", "")))
                if target:
                    target.recommendation = str(e.get("recommendation", "")).strip()

        self._repo.save_data_validation(project, run)
        return run
