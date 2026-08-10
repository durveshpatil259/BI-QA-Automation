"""PipelineRunner — the deterministic orchestrator.

Executes :data:`STAGE_ORDER` top to bottom. Every stage delegates to an existing
single-responsibility service; the runner owns only sequencing, failure policy,
cancellation and progress reporting.

Services are injected, so the whole pipeline is testable with fakes and the
Streamlit UI, the FastAPI layer and the test suite all drive the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.exceptions import BITestPilotError
from src.core.logger import get_logger
from src.domain.models import LLMSettings
from src.pipeline.context import PipelineContext
from src.pipeline.progress import ProgressReporter
from src.pipeline.stages import STAGE_ORDER, STAGE_POLICY, FailurePolicy, Stage

_logger = get_logger()


class PipelineCancelled(BITestPilotError):
    """Raised internally when a run is cancelled between stages."""


@dataclass
class PipelineServices:
    """Everything the pipeline needs, injected in one object."""

    datasource_service: object
    metadata_service: object
    schema_service: object
    analysis_service: object
    llm_service: object
    validation_plan_service: object
    sql_validation_engine: object
    test_expansion_service: object
    report_service: object
    dax_evaluation_service: object | None = None   # added in P4


class PipelineRunner:
    """Runs the full analysis for one project."""

    def __init__(self, services: PipelineServices):
        self._s = services

    # --- public -----------------------------------------------------------
    def run(
        self,
        ctx: PipelineContext,
        reporter: ProgressReporter | None = None,
    ) -> PipelineContext:
        """Execute every stage. Returns the populated context."""
        reporter = reporter or ProgressReporter("local", len(STAGE_ORDER))
        handlers = {
            Stage.EXTRACT_METADATA: self._extract_metadata,
            Stage.READ_SCHEMA: self._read_schema,
            Stage.EVALUATE_DAX: self._evaluate_dax,
            Stage.BUILD_CONTEXT: self._build_context,
            Stage.LLM_ANALYSIS: self._llm_analysis,
            Stage.GENERATE_SQL: self._generate_sql,
            Stage.EXECUTE_SQL: self._execute_sql,
            Stage.GENERATE_TESTS: self._generate_tests,
            Stage.BUILD_REPORT: self._build_report,
        }

        for index, stage in enumerate(STAGE_ORDER, start=1):
            if ctx.cancelled:
                reporter.emit(stage, index, "skipped", "Cancelled by user.")
                raise PipelineCancelled("Run cancelled.")

            reporter.emit(stage, index, "running")
            try:
                message = handlers[stage](ctx) or stage.value
                reporter.emit(stage, index, "done", message)
                _logger.info("stage=%s status=done | %s", stage.name, message)
            except Exception as exc:  # noqa: BLE001 - policy decides what happens
                policy = STAGE_POLICY[stage]
                detail = f"{stage.value} failed: {exc}"
                _logger.warning("stage=%s status=failed policy=%s | %s",
                                stage.name, policy.value, exc)
                if policy is FailurePolicy.FATAL:
                    reporter.emit(stage, index, "failed", detail)
                    raise
                ctx.warn(detail)
                reporter.emit(stage, index, "skipped", detail)

        return ctx

    # --- stages -----------------------------------------------------------
    # Each returns a short human message for the progress feed.

    def _extract_metadata(self, ctx: PipelineContext) -> str:
        ctx.metadata = self._s.metadata_service.extract(ctx.project)
        counts = ctx.metadata.summary_counts()
        return (
            f"{counts['tables']} tables, {counts['measures']} measures, "
            f"{counts['visuals']} visuals"
        )

    def _read_schema(self, ctx: PipelineContext) -> str:
        if ctx.datasource is None:
            ctx.datasource = self._s.datasource_service.load(ctx.project)
        if not ctx.datasource or not ctx.datasource.is_configured:
            raise BITestPilotError("No datasource configured.")
        ctx.db_schema = self._s.schema_service.read_schema(ctx.project, ctx.datasource)
        c = ctx.db_schema.summary_counts()
        return f"{c['tables']} tables, {c['columns']} columns, {c['foreign_keys']} FKs"

    def _evaluate_dax(self, ctx: PipelineContext) -> str:
        service = self._s.dax_evaluation_service
        if service is None:
            raise BITestPilotError(
                "DAX evaluation unavailable; comparison will use executability."
            )
        ctx.dax_values = service.evaluate(ctx.project, ctx.metadata)
        return f"{len(ctx.dax_values)} measure value(s) evaluated"

    def _build_context(self, ctx: PipelineContext) -> str:
        ctx.analysis_context = self._s.analysis_service.build_context(ctx.project)
        s = ctx.analysis_context.validation_summary()
        return f"{s['total']} checks — {s['passed']} passed, {s['failed']} failed"

    def _llm_analysis(self, ctx: PipelineContext) -> str:
        settings = self._settings(ctx)
        if not settings.is_configured:
            raise BITestPilotError("No LLM configured.")
        reasoning = self._s.llm_service.generate(
            ctx.project, ctx.analysis_context, settings
        )
        return f"Summary and {len(reasoning.recommendations)} recommendation(s)"

    def _generate_sql(self, ctx: PipelineContext) -> str:
        settings = self._settings(ctx)
        if not settings.is_configured:
            raise BITestPilotError("No LLM configured; cannot generate SQL.")
        ctx.validation_plan = self._s.validation_plan_service.generate(
            ctx.project, settings
        )
        return f"{len(ctx.validation_plan.items)} query/queries generated"

    def _execute_sql(self, ctx: PipelineContext) -> str:
        ctx.results = self._s.sql_validation_engine.run(
            ctx.project, ctx.datasource, tolerance_pct=ctx.tolerance_pct
        )
        s = ctx.results.summary()
        return f"{s['total']} tests — {s['passed']} passed, {s['failed']} failed"

    def _generate_tests(self, ctx: PipelineContext) -> str:
        ctx.test_cases = self._s.test_expansion_service.expand(ctx.project)
        return f"{len(ctx.test_cases)} test case(s)"

    def _build_report(self, ctx: PipelineContext) -> str:
        ctx.report = self._s.report_service.build_report(ctx.project)
        return f"Report {ctx.report.id}"

    # --- helpers ----------------------------------------------------------
    def _settings(self, ctx: PipelineContext) -> LLMSettings:
        if ctx.llm_settings is None:
            ctx.llm_settings = self._s.llm_service.load_settings(ctx.project)
        return ctx.llm_settings
