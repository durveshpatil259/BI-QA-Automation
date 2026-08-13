"""Data carried between pipeline stages.

Threading one mutable context through the run avoids re-reading artifacts from
disk at every stage and makes each stage independently testable: hand it a
context, assert on what it added.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from src.core.usage import UsageAccumulator
from src.domain.models import (
    AnalysisContext,
    AnalysisReport,
    DashboardMetadata,
    DataValidationRun,
    DatasourceConfig,
    DbSchema,
    LLMSettings,
    Project,
    TestCase,
    ValidationPlan,
)


@dataclass
class PipelineContext:
    """Inputs, intermediate artifacts and outputs of one pipeline run."""

    project: Project
    datasource: DatasourceConfig | None = None
    llm_settings: LLMSettings | None = None
    tolerance_pct: float = 1.0

    # --- artifacts produced by stages, in order ---------------------------
    metadata: DashboardMetadata | None = None
    db_schema: DbSchema | None = None
    #: measure name -> true value evaluated from the model (DAX evaluation).
    dax_values: dict[str, str] = field(default_factory=dict)
    analysis_context: AnalysisContext | None = None
    validation_plan: ValidationPlan | None = None
    results: DataValidationRun | None = None
    test_cases: list[TestCase] = field(default_factory=list)
    report: AnalysisReport | None = None

    #: Non-fatal problems worth surfacing to the user (degraded stages).
    warnings: list[str] = field(default_factory=list)

    #: Tokens consumed by this run, broken down by stage.
    usage: UsageAccumulator = field(default_factory=UsageAccumulator, repr=False)

    #: The key's daily budget as it stood when the run started.
    budget_status: object | None = None
    #: Set when the daily budget ran out mid-run. Later AI stages are skipped
    #: rather than attempted: they would each fail identically, and on
    #: providers that charge rejected calls, expensively.
    budget_exhausted: bool = False

    # --- cooperative cancellation ----------------------------------------
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_event(self) -> threading.Event:
        """Shared with :mod:`src.core.cancellation` so services can abort too."""
        return self._cancel

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # --- summary for the results screen -----------------------------------
    def summary(self) -> dict[str, int]:
        counts = {"tests": 0, "passed": 0, "failed": 0, "warnings": len(self.warnings)}
        if self.results:
            s = self.results.summary()
            counts["tests"] = s["total"]
            counts["passed"] = s["passed"]
            counts["failed"] = s["failed"]
        return counts
