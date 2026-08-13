"""SQL execution + comparison engine (redesign V5).

Executes each validation-plan item's generated SQL (read-only, with timing and
status), parses the scalar result, and compares it to the dashboard value using
the deterministic tolerance engine (V1) — producing PASS/FAIL. This is ALL
Python: the AI never executes SQL. The AI may afterwards *explain* failures.
"""

from __future__ import annotations

import time

from src.core import cancellation
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
from src.services.execution import SqlServerAdapter
from src.services.validation import compare_display_values, compare_values, parse_value
from src.services.validation.identifier_guard import check_identifiers
from src.services.validation.sql_guard import double_percent_scaling, is_read_only
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class SqlValidationEngine:
    """Runs the validation plan against the datasource and compares values."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load(self, project: Project) -> DataValidationRun | None:
        return self._repo.load_data_validation(project)

    # --- execution + comparison ------------------------------------------
    def _adapter_for(self, config: DatasourceConfig, db_schema, project=None):
        """Pick the execution engine for this datasource.

        File datasources have no SQL engine, so they get an adapter that
        executes the plan's structured intent directly. Everything else keeps
        the existing SQL Server path unchanged.
        """
        if config.type in (DatasourceType.EXCEL, DatasourceType.CSV):
            from src.services.execution.file_adapter import build_file_adapter

            # Metadata supplies the relationships a cross-dataset filter needs.
            # Optional: without a project the adapter still runs, but a filter
            # that would need a join is reported as unapplied rather than
            # quietly dropped.
            metadata = self._repo.load_metadata(project) if project else None
            return build_file_adapter(config, metadata)
        return SqlServerAdapter(create_connector(config), db_schema)

    def run(
        self,
        project: Project,
        config: DatasourceConfig | None = None,
        *,
        tolerance_pct: float = 1.0,
        adapter=None,
    ) -> DataValidationRun:
        plan = self._repo.load_validation_plan(project)
        if not plan or not plan.items:
            raise ValidationError(
                "No validation plan found. Generate it in Analysis Step 5 first."
            )
        config = config or self._repo.load_datasource(project)
        if config is None or not config.is_configured:
            raise ValidationError("No datasource configured.")


        # Read once: every generated query is checked against it before it
        # reaches the database.
        db_schema = self._repo.load_db_schema(project)
        # The adapter is the only part that varies per datasource; every plan
        # item, comparison and verdict below is shared across all of them.
        adapter = adapter or self._adapter_for(config, db_schema, project)

        run = DataValidationRun()
        for i, item in enumerate(plan.items, start=1):
            # A plan can hold 200+ queries; stop between them on cancel.
            cancellation.raise_if_cancelled()
            if item.item_type == "grouped":
                run.results.extend(
                    self._validate_grouped(f"CH_{i:03d}", item, adapter, tolerance_pct)
                )
            elif item.item_type == "structural":
                run.results.append(
                    self._validate_structural(f"ST_{i:03d}", item, adapter)
                )
            else:
                run.results.append(
                    self._validate_item(f"QA_{i:03d}", item, adapter, tolerance_pct)
                )

        # DAX-driven checks: when there is no screenshot value, a measure defined
        # from other measures can still be verified arithmetically.
        self._apply_consistency_checks(project, run, tolerance_pct)

        self._repo.save_data_validation(project, run)
        _logger.info("SQL validation for %s: %s", project.id, run.summary())
        return run

    def _validate_item(
        self, test_id, item: ValidationPlanItem, adapter, tolerance_pct,
    ) -> SqlValidationResult:
        dashboard_numeric, _ = parse_value(item.dashboard_value)
        result = SqlValidationResult(
            test_id=test_id,
            kpi_name=item.kpi_name,
            scenario=item.scenario,
            dashboard_value=item.dashboard_value,
            dashboard_numeric=dashboard_numeric,
            generated_sql=item.generated_sql,
            tolerance_pct=tolerance_pct,
            confidence=item.confidence,
        )

        # Execution — including every guard — belongs to the adapter, so each
        # datasource enforces the rules that actually apply to it.
        outcome = adapter.execute_scalar(item)
        result.execution_time_ms = outcome.elapsed_ms
        result.source_evidence = outcome.evidence
        if not outcome.ok:
            result.execution_status = "error"
            result.status = TestStatus.FAIL
            result.reason = outcome.error
            return result

        result.execution_status = "ok"
        db_value = outcome.value or ""
        result.database_value = str(db_value)
        result.database_numeric, _ = parse_value(db_value)

        # Compare (Python): exact displayed-format match first, numeric fallback.
        if not item.dashboard_value:
            # PBIX/PBIT-only mode — no rendered value to compare against, so the
            # verdict comes from executability and sanity of the result instead.
            blank = result.database_value.strip().lower() in ("", "none", "null")
            if blank or result.database_numeric is None:
                result.status = TestStatus.FAIL
                result.match_type = "executability"
                result.reason = (
                    "Query executed but returned no usable value "
                    f"(got '{result.database_value}') — the KPI mapping is likely wrong."
                )
            else:
                result.status = TestStatus.PASS
                result.match_type = "executability"
                result.reason = (
                    f"Query executed successfully and returned {result.database_value}. "
                    "No dashboard screenshot was supplied, so this validates the "
                    "DAX→SQL mapping, not the rendered value."
                )
            return result

        # An empty/NULL scalar is a matched-no-rows query, not an unparseable
        # number. Saying "could not parse" sent people looking at the comparison
        # logic when the real answer is that the WHERE clause excluded everything.
        if result.database_value.strip().lower() in ("", "none", "null"):
            result.status = TestStatus.FAIL
            result.match_type = "no-rows"
            result.reason = (
                "Query ran but matched no rows (returned NULL), so there is "
                f"nothing to compare with the dashboard's {item.dashboard_value}. "
                "Usually the filter values or the joined table are wrong."
            )
            return result

        outcome = compare_display_values(
            item.dashboard_value, result.database_value, tolerance_pct=tolerance_pct
        )
        result.difference = outcome.difference_display
        result.difference_pct = outcome.difference_pct
        result.match_type = outcome.match_type
        result.status = TestStatus.PASS if outcome.passed else TestStatus.FAIL
        result.reason = outcome.reason
        return result

    # --- chart / table / matrix validation --------------------------------
    def _run_multirow(self, item: ValidationPlanItem, adapter, max_rows=500):
        """Execute a chart item, returning (rows, elapsed_ms, error, evidence)."""
        outcome = adapter.execute_grouped(item, max_rows=max_rows)
        return outcome.rows, outcome.elapsed_ms, outcome.error, outcome.evidence

    def _validate_grouped(
        self, base_id, item: ValidationPlanItem, adapter, tolerance_pct,
    ) -> list[SqlValidationResult]:
        """Compare each category's displayed value against its database value.

        The query returns (dimension, value) rows; each dashboard data point
        becomes its own PASS/FAIL row so a single wrong bar is pinpointed.
        """
        rows, elapsed, error, evidence = self._run_multirow(item, adapter)
        if error:
            return [SqlValidationResult(
                test_id=base_id, kpi_name=item.visual_title or item.kpi_name,
                visual_title=item.visual_title, scenario=item.scenario,
                generated_sql=item.generated_sql, execution_time_ms=elapsed,
                source_evidence=evidence,
                execution_status="error", status=TestStatus.FAIL, reason=error,
                confidence=item.confidence, match_type="chart-grouped",
            )]

        # dimension -> displayed value from the database
        db_by_dim = {
            str(r[0]).strip().casefold(): str(r[1])
            for r in rows if r and len(r) >= 2
        }

        # No screenshot data points (PBIX/PBIT-only mode): the chart's values
        # were never rendered, so validate that its field bindings produce a
        # real, non-empty grouped result set.
        if not item.expected_points:
            sample = ", ".join(
                f"{r[0]}={r[1]}" for r in rows[:5] if r and len(r) >= 2
            )
            ok = bool(db_by_dim)
            return [SqlValidationResult(
                test_id=base_id,
                kpi_name=item.visual_title or item.kpi_name,
                visual_title=item.visual_title, scenario=item.scenario,
                generated_sql=item.generated_sql, execution_time_ms=elapsed,
                execution_status="ok",
                status=TestStatus.PASS if ok else TestStatus.FAIL,
                match_type="chart-executability", confidence=item.confidence,
                database_value=f"{len(db_by_dim)} categories",
                reason=(
                    f"Chart query executed and returned {len(db_by_dim)} grouped "
                    f"row(s): {sample}. No screenshot was supplied, so this validates "
                    "the visual's field bindings and query, not the plotted values."
                ) if ok else (
                    "Chart query executed but returned no rows — the visual's field "
                    "bindings or filters are likely wrong."
                ),
            )]

        results: list[SqlValidationResult] = []
        for n, point in enumerate(item.expected_points, start=1):
            key = point.dimension.strip().casefold()
            db_value = db_by_dim.get(key)
            res = SqlValidationResult(
                test_id=f"{base_id}_{n:02d}",
                kpi_name=f"{item.visual_title or item.kpi_name} · {point.dimension}",
                visual_title=item.visual_title, dimension_value=point.dimension,
                scenario=item.scenario, dashboard_value=point.raw_value,
                dashboard_numeric=point.numeric_value,
                generated_sql=item.generated_sql, execution_time_ms=elapsed,
                execution_status="ok", tolerance_pct=tolerance_pct,
                confidence=item.confidence, match_type="chart-grouped",
            )
            if db_value is None:
                res.status = TestStatus.FAIL
                res.reason = (
                    f"Category '{point.dimension}' is shown on the chart but the "
                    "database query returned no such row."
                )
                results.append(res)
                continue

            res.database_value = db_value
            res.database_numeric, _ = parse_value(db_value)
            if not point.raw_value:
                # Category matched but no readable number on the chart.
                res.status = TestStatus.PASS
                res.match_type = "chart-category"
                res.reason = (
                    f"Category '{point.dimension}' exists in the database "
                    f"(value {db_value}); the chart showed no readable number to compare."
                )
            else:
                outcome = compare_display_values(
                    point.raw_value, db_value, tolerance_pct=tolerance_pct
                )
                res.difference = outcome.difference_display
                res.difference_pct = outcome.difference_pct
                res.match_type = outcome.match_type or "chart-grouped"
                res.status = TestStatus.PASS if outcome.passed else TestStatus.FAIL
                res.reason = outcome.reason
            results.append(res)

        # Categories present in the database but missing from the chart.
        shown = {p.dimension.strip().casefold() for p in item.expected_points}
        missing = [d for d in db_by_dim if d not in shown]
        if missing:
            results.append(SqlValidationResult(
                test_id=f"{base_id}_COV",
                kpi_name=f"{item.visual_title or item.kpi_name} · coverage",
                visual_title=item.visual_title, scenario=item.scenario,
                generated_sql=item.generated_sql, execution_time_ms=elapsed,
                execution_status="ok", status=TestStatus.FAIL,
                match_type="chart-coverage", confidence=item.confidence,
                dashboard_value=f"{len(shown)} categories shown",
                database_value=f"{len(db_by_dim)} categories in database",
                reason=(
                    "Database has categories not displayed on the chart: "
                    + ", ".join(sorted(missing)[:10])
                    + ". This may be correct (Top-N visual) or a missing-data defect."
                ),
            ))
        return results

    def _validate_structural(
        self, test_id, item: ValidationPlanItem, adapter
    ) -> SqlValidationResult:
        """Compare the chart's category SET against the database.

        Used when the chart shows shapes/colours but no readable numbers, so the
        values cannot be checked — but the categories still can.
        """
        result = SqlValidationResult(
            test_id=test_id, kpi_name=item.visual_title or item.kpi_name,
            visual_title=item.visual_title, scenario=item.scenario,
            generated_sql=item.generated_sql, confidence=item.confidence,
            match_type="chart-structural",
        )
        rows, elapsed, error, evidence = self._run_multirow(item, adapter)
        result.execution_time_ms = elapsed
        result.source_evidence = evidence
        if error:
            result.execution_status = "error"
            result.status = TestStatus.FAIL
            result.reason = error
            return result

        db_cats = {str(r[0]).strip().casefold() for r in rows if r}
        shown = {p.dimension.strip().casefold() for p in item.expected_points}
        result.execution_status = "ok"
        result.dashboard_value = f"{len(shown)} categories shown"
        result.database_value = f"{len(db_cats)} categories in database"

        invalid = sorted(shown - db_cats)
        if invalid:
            result.status = TestStatus.FAIL
            result.reason = (
                "Chart displays categories that do not exist in the database: "
                + ", ".join(invalid[:10])
                + ". Values could not be compared (chart shows no printed numbers)."
            )
            return result

        missing = sorted(db_cats - shown)
        result.status = TestStatus.PASS
        result.reason = (
            f"All {len(shown)} displayed categories exist in the database. "
            + (f"Database also has {len(missing)} not shown ("
               + ", ".join(missing[:6]) + ") — expected for a Top-N or filtered visual. "
               if missing else "")
            + "Values were not compared because the chart shows no printed numbers."
        )
        return result

    # --- DAX cross-measure consistency (screenshot-free verdicts) --------
    def _apply_consistency_checks(
        self, project: Project, run: DataValidationRun, tolerance_pct: float
    ) -> None:
        """Verify measures defined from other measures actually agree.

        ``Total Profit = [Total Sales] - [Total Cost]`` is checkable purely from
        executed SQL results, giving a real PASS/FAIL with no dashboard value.
        """
        from src.services.validation.dax_analyzer import extract_consistency_rules

        metadata = self._repo.load_metadata(project)
        if not metadata:
            return
        rules = extract_consistency_rules(metadata.all_measures)
        if not rules:
            return

        # Executed numeric results per KPI (per scenario, so filters line up).
        values: dict[tuple[str, str], float] = {}
        for r in run.results:
            if r.execution_status == "ok" and r.database_numeric is not None:
                values[(r.scenario, r.kpi_name.casefold())] = r.database_numeric

        scenarios = {r.scenario for r in run.results}
        next_id = len(run.results) + 1
        for scenario in sorted(scenarios):
            for rule in rules:
                left = values.get((scenario, rule.left.casefold()))
                right = values.get((scenario, rule.right.casefold()))
                actual = values.get((scenario, rule.target.casefold()))
                if left is None or right is None or actual is None:
                    continue  # not all three were computed
                expected = rule.apply(left, right)
                if expected is None:
                    continue

                outcome = compare_values(expected, actual, tolerance_pct=tolerance_pct)
                result = SqlValidationResult(
                    test_id=f"DAX_{next_id:03d}",
                    kpi_name=rule.target,
                    scenario=scenario,
                    dashboard_value=f"{expected:,.4f} (from DAX)",
                    dashboard_numeric=expected,
                    generated_sql=f"-- DAX consistency: {rule.describe()}",
                    database_value=f"{actual:,.4f}",
                    database_numeric=actual,
                    difference=outcome.difference_display,
                    difference_pct=outcome.difference_pct,
                    tolerance_pct=tolerance_pct,
                    execution_status="ok",
                    match_type="dax-consistency",
                    status=TestStatus.PASS if outcome.passed else TestStatus.FAIL,
                    confidence=1.0,
                    reason=(
                        f"DAX rule {rule.describe()} — computed {expected:,.4f} from "
                        f"components, measure returned {actual:,.4f}. {outcome.reason}"
                    ),
                )
                run.results.append(result)
                next_id += 1
        _logger.info("Applied %d DAX consistency rule(s)", len(rules))

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
                EXPLAIN_SYSTEM_PROMPT, build_explain_user_prompt(failures),
                max_tokens=200 * len(failures) + 300,
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
