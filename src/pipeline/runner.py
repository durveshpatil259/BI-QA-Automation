"""PipelineRunner — the deterministic orchestrator.

Executes :data:`STAGE_ORDER` top to bottom. Every stage delegates to an existing
single-responsibility service; the runner owns only sequencing, failure policy,
cancellation and progress reporting.

Services are injected, so the whole pipeline is testable with fakes and the
Streamlit UI, the FastAPI layer and the test suite all drive the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core import cancellation, usage
from src.core.exceptions import (BITestPilotError, OperationCancelled,
                                 TokenBudgetExhausted)
from src.core.logger import get_logger
from src.domain.models import LLMSettings
from src.pipeline.context import PipelineContext
from src.pipeline.progress import ProgressReporter
from src.pipeline.stages import (AI_STAGES, STAGE_ORDER, STAGE_POLICY,
                                 FailurePolicy, Stage)

_logger = get_logger()


#: Cancellation is raised from deep inside services (the LLM back-off), so the
#: pipeline name is an alias rather than a subclass — ``except PipelineCancelled``
#: must catch the very same type that ``core.cancellation`` raises.
PipelineCancelled = OperationCancelled


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

        # Publish the token so the LLM back-off and per-item loops inside the
        # services can abort mid-stage, not just at these stage boundaries.
        with cancellation.use_token(cancellation.CancelToken(ctx.cancel_event)), \
                usage.use_collector(ctx.usage):
            self._check_budget(ctx)
            self._run_stages(ctx, reporter, handlers)
        return ctx

    def _check_budget(self, ctx: PipelineContext) -> None:
        """Refuse to start on a key that cannot pay for the run.

        Better to say so up front than to extract metadata, read the schema and
        evaluate DAX — minutes of work — only to produce a report with no
        validations in it because the first LLM call was rejected.
        """
        from src.core import token_budget
        from src.core.config import load_config
        from src.core.exceptions import TokenBudgetExhausted

        try:
            settings = self._settings(ctx)
        except Exception:  # noqa: BLE001 - a missing key is reported by its stage
            return
        if not settings or not settings.is_configured:
            return

        status = token_budget.status_for(
            settings.provider, settings.model, settings.api_key
        )
        ctx.budget_status = status
        _logger.info("Token budget before run | %s", status.describe())
        if not status.enforced:
            return

        floor = int(getattr(load_config(), "llm_min_tokens_to_start", 0) or 0)
        if status.remaining <= 0:
            raise TokenBudgetExhausted(
                f"Daily token budget already used up for {status.provider} / "
                f"{status.model}: {status.used:,} of {status.limit:,}. It resets "
                f"at {status.resets_at[:16].replace('T', ' ')}. Nothing was run."
            )
        if floor and status.remaining < floor:
            # Enough for a call or two, not enough for a report worth reading.
            raise TokenBudgetExhausted(
                f"Only {status.remaining:,} tokens left today for {status.provider} / "
                f"{status.model}, and a run needs about {floor:,} to produce a "
                f"usable report. It resets at "
                f"{status.resets_at[:16].replace('T', ' ')}. Nothing was run."
            )

    def _run_stages(self, ctx, reporter, handlers) -> None:
        for index, stage in enumerate(STAGE_ORDER, start=1):
            if ctx.cancelled:
                reporter.emit(stage, index, "skipped", "Cancelled by user.")
                raise PipelineCancelled("Run cancelled.")

            if ctx.budget_exhausted and stage in AI_STAGES:
                detail = "Skipped — the daily token budget was used up earlier in this run."
                ctx.warn(f"{stage.value}: {detail}")
                reporter.emit(stage, index, "skipped", detail)
                _logger.info("stage=%s status=skipped-no-budget", stage.name)
                continue

            reporter.emit(stage, index, "running")
            try:
                # Attribute any LLM calls this stage makes to the stage itself.
                with ctx.usage.stage(stage.value):
                    message = handlers[stage](ctx) or stage.value
                reporter.emit(stage, index, "done", message)
                _logger.info("stage=%s status=done | %s", stage.name, message)
            except OperationCancelled:
                # A user decision, never a stage failure: no policy applies.
                reporter.emit(stage, index, "skipped", "Cancelled by user.")
                _logger.info("stage=%s status=cancelled", stage.name)
                raise
            except TokenBudgetExhausted as exc:
                # Not a failure of this stage and not worth retrying: the key
                # is spent until the reset. Every later AI stage is skipped,
                # but the deterministic ones still run so the user gets a
                # report covering everything that did complete.
                ctx.budget_exhausted = True
                ctx.warn(f"{stage.value} stopped: {exc}")
                reporter.emit(stage, index, "skipped", str(exc))
                _logger.warning("stage=%s status=budget-exhausted | %s",
                                stage.name, exc)
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
        # Generation may have stopped mid-way on a spent key. The plan it did
        # produce is still worth executing, so this is a flag rather than a
        # raise — but the later AI stages must not try again.
        if getattr(ctx.validation_plan, "budget_exhausted", False):
            ctx.budget_exhausted = True
        # A partial plan silently shrinks the whole report, so it must reach the
        # user as a warning rather than just a smaller number of validations.
        note = getattr(ctx.validation_plan, "coverage_note", lambda: "")()
        if note:
            ctx.warn(note)
            return (
                f"{len(ctx.validation_plan.items)} query/queries generated "
                f"({ctx.validation_plan.batches_ok}/"
                f"{ctx.validation_plan.batches_total} batches OK — INCOMPLETE)"
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
        # Kept for the report's optimisation section: how many candidates were
        # generated and what was removed is the evidence that the suite is
        # deliberately compact rather than accidentally thin.
        ctx.dedup_stats = getattr(self._s.test_expansion_service, "last_stats", None)
        stats = ctx.dedup_stats
        suffix = f" ({stats.describe()})" if stats else ""
        return f"{len(ctx.test_cases)} test case(s){suffix}"

    def _build_report(self, ctx: PipelineContext) -> str:
        token_usage = ctx.usage.to_dict()
        # The reader needs to know a short report was short *because the key
        # ran out*, not because the dashboard had little to check.
        token_usage["budget_exhausted"] = ctx.budget_exhausted
        token_usage["optimization"] = self._optimization(ctx)
        if ctx.budget_status is not None:
            token_usage["daily_budget"] = ctx.budget_status.to_dict()
        ctx.report = self._s.report_service.build_report(
            ctx.project, token_usage=token_usage
        )
        self._save_summary(ctx)
        total = ctx.usage.total_tokens
        suffix = f" · {total:,} tokens across {ctx.usage.total_calls} call(s)" if total else ""
        if ctx.budget_exhausted:
            suffix += " · STOPPED EARLY: daily token budget exhausted"
        _logger.info("Run token cost | %s tokens over %s call(s)%s",
                     f"{total:,}", ctx.usage.total_calls,
                     " | budget exhausted" if ctx.budget_exhausted else "")
        for entry in ctx.usage.stages:
            _logger.info("  %-28s %6s tokens over %d call(s)",
                         entry.stage, f"{entry.total_tokens:,}", entry.calls)
        return f"Report {ctx.report.id}{suffix}"

    def _save_summary(self, ctx: PipelineContext) -> None:
        """Write the flat run record the dashboard aggregates over.

        Best-effort: a summary that cannot be written must not fail a run that
        otherwise succeeded, so this logs and moves on. The full artifacts are
        still on disk and remain the source of truth.
        """
        from src.domain.models import RunSummary

        try:
            counts = ctx.metadata.summary_counts() if ctx.metadata else {}
            results = ctx.results.summary() if ctx.results else {}
            stats = getattr(ctx, "dedup_stats", None)
            plan = ctx.validation_plan
            # Severity comes from the same rule the Visual Bugs view applies,
            # so the dashboard count and that page can never disagree.
            from src.services.validation.issue_severity import classify

            severity = {"High": 0, "Medium": 0, "Low": 0}
            for row in (ctx.results.results if ctx.results else []):
                if str(getattr(row, "status", "")).casefold().startswith("pass"):
                    continue
                _, level = classify(getattr(row, "match_type", ""),
                                    getattr(row, "database_value", ""))
                severity[level] = severity.get(level, 0) + 1
            summary = RunSummary(
                project_id=ctx.project.id,
                project_name=ctx.project.name,
                status="Completed",
                processing_time_ms=ctx.project.processing_time_ms,
                pages=int(counts.get("pages", 0) or 0),
                visuals=int(counts.get("visuals", 0) or 0),
                measures=int(counts.get("measures", 0) or 0),
                tables=int(counts.get("tables", 0) or 0),
                relationships=len(ctx.metadata.relationships or []) if ctx.metadata else 0,
                tests_total=int(results.get("total", 0) or 0),
                tests_passed=int(results.get("passed", 0) or 0),
                tests_failed=int(results.get("failed", 0) or 0),
                tests_warning=int(results.get("warnings", 0) or 0),
                tests_skipped=int(results.get("skipped", 0) or 0),
                test_cases=len(ctx.test_cases or []),
                candidates=getattr(stats, "original", 0),
                duplicates_removed=getattr(stats, "duplicates_removed", 0),
                low_value_skipped=getattr(stats, "low_value_skipped", 0),
                issues_high=severity["High"],
                issues_medium=severity["Medium"],
                issues_low=severity["Low"],
                tokens=ctx.usage.total_tokens,
                llm_calls=ctx.usage.total_calls,
                compiled_without_llm=getattr(plan, "compiled_items", 0),
            )
            self._repo_of(ctx).save_run_summary(ctx.project, summary)
            _logger.info("Run summary saved for %s: %d test(s), %d issue(s)",
                         ctx.project.id, summary.tests_total, summary.issues)
        except Exception as exc:  # noqa: BLE001 - never fail a completed run
            _logger.warning("Could not write the run summary: %s", exc)

    @staticmethod
    def _repo_of(ctx: PipelineContext):
        """The repository the services were built with."""
        from src.storage.project_repository import ProjectRepository
        from src.core.config import load_config

        return ProjectRepository(load_config().projects_root_path)

    @staticmethod
    def _optimization(ctx: PipelineContext) -> dict:
        """What the run chose not to do, and why that was cheaper.

        Every figure here is counted during the run rather than estimated:
        compiled KPIs never reached a prompt, duplicates were fingerprinted
        away, and low-value tests were capped per subject.
        """
        stats = getattr(ctx, "dedup_stats", None)
        plan = ctx.validation_plan
        data = {
            "candidate_tests": getattr(stats, "original", 0),
            "selected_tests": getattr(stats, "kept", 0),
            "duplicates_removed": getattr(stats, "duplicates_removed", 0),
            "low_value_skipped": getattr(stats, "low_value_skipped", 0),
            "by_priority": dict(getattr(stats, "by_priority", {}) or {}),
            "compiled_without_llm": getattr(plan, "compiled_items", 0),
            "llm_calls": getattr(plan, "llm_calls", 0),
            "plan_items": len(plan.items) if plan else 0,
        }
        total = data["compiled_without_llm"] + data["plan_items"]
        data["compiled_pct"] = (
            round(100 * data["compiled_without_llm"] / data["plan_items"], 0)
            if data["plan_items"] else 0
        )
        return data

    # --- helpers ----------------------------------------------------------
    def _settings(self, ctx: PipelineContext) -> LLMSettings:
        if ctx.llm_settings is None:
            ctx.llm_settings = self._s.llm_service.load_settings(ctx.project)
        return ctx.llm_settings
