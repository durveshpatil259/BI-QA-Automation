"""Analysis context builder — the deterministic assembly stage.

This service performs the product's central invariant: it gathers ALL
deterministic results (metadata, visual facts, datasource comparison, rule-based
validation) into a single :class:`AnalysisContext` and persists it. That context
is the one and only thing the LLM is later given to reason over (Module 8).

No LLM call happens here. If this stage has not produced a context, the AI stage
must not run.
"""

from __future__ import annotations

from src.core.constants import AnalysisMode
from src.core.logger import get_logger
from src.domain.models import AnalysisContext, Project
from src.services.comparison_engine import ComparisonEngine
from src.services.rules import RuleEngine, RuleInput
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class AnalysisService:
    """Builds and persists the deterministic :class:`AnalysisContext`."""

    def __init__(
        self,
        repository: ProjectRepository,
        comparison_engine: ComparisonEngine | None = None,
        rule_engine: RuleEngine | None = None,
    ):
        self._repo = repository
        self._comparison = comparison_engine or ComparisonEngine()
        self._rules = rule_engine or RuleEngine()

    def _needs_metadata(self, mode: AnalysisMode | None) -> bool:
        return mode in (AnalysisMode.METADATA, AnalysisMode.COMPLETE)

    def _needs_visual(self, mode: AnalysisMode | None) -> bool:
        return mode in (AnalysisMode.VISUAL, AnalysisMode.COMPLETE)

    def build_context(self, project: Project) -> AnalysisContext:
        """Assemble the deterministic context for *project* and persist it."""
        mode = project.analysis_mode or AnalysisMode.METADATA

        metadata = self._repo.load_metadata(project) if self._needs_metadata(mode) else None
        visual = self._repo.load_visual_analysis(project) if self._needs_visual(mode) else None
        datasource = self._repo.load_datasource(project)

        comparisons = []
        data_results = []
        if metadata is not None and datasource is not None and datasource.is_configured:
            comparisons, data_results = self._comparison.compare(metadata, datasource)

        findings = self._rules.run(
            RuleInput(metadata=metadata, visual=visual, comparisons=comparisons)
        )

        context = AnalysisContext(
            project_id=project.id,
            project_name=project.name,
            platform=project.bi_platform,
            analysis_mode=mode,
            metadata=metadata,
            visual_analysis=visual,
            datasource_type=datasource.type if datasource else None,
            data_results=data_results,
            comparisons=comparisons,
            validations=findings,
        )
        self._repo.save_analysis_context(project, context)
        _logger.info(
            "Built AnalysisContext for %s: %s", project.id, context.validation_summary()
        )
        return context

    def load_context(self, project: Project) -> AnalysisContext | None:
        return self._repo.load_analysis_context(project)
