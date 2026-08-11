"""Report service — assemble, persist and export the final analysis report."""

from __future__ import annotations

import datetime as _dt

from src.core.constants import AnalysisStatus
from src.core.exceptions import ValidationError
from src.core.logger import get_logger
from src.domain.models import AnalysisReport, Project
from src.services.reporting.html_renderer import render_html
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class ReportService:
    """Builds :class:`AnalysisReport` objects from the persisted artifacts."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def build_report(
        self, project: Project, token_usage: dict | None = None
    ) -> AnalysisReport:
        """Combine context + AI reasoning + test cases into a saved report."""
        context = self._repo.load_analysis_context(project)
        if context is None:
            raise ValidationError(
                "No Analysis Context found. Run Analysis Step 3 "
                "(Comparison & Validation) before generating a report."
            )
        reasoning = self._repo.load_ai_reasoning(project)
        test_cases = self._repo.load_test_cases(project)
        data_validation = self._repo.load_data_validation(project)

        report = AnalysisReport(
            project_id=project.id,
            project_name=project.name,
            platform=project.bi_platform,
            analysis_mode=context.analysis_mode,
            status=AnalysisStatus.COMPLETED,
            llm_provider=reasoning.provider if reasoning else None,
            llm_model=reasoning.model if reasoning else "",
            executive_summary=reasoning.executive_summary if reasoning else "",
            root_cause_analysis=reasoning.root_cause_analysis if reasoning else "",
            recommendations=reasoning.recommendations if reasoning else [],
            test_cases=test_cases,
            validation_summary=context.validation_summary(),
            findings=context.validations,
            comparisons=context.comparisons,
            sql_validations=data_validation.results if data_validation else [],
            data_validation_summary=data_validation.summary() if data_validation else {},
            token_usage=token_usage or {},
        )
        self._repo.save_report(project, report)

        # Reflect completion on the project.
        project.status = AnalysisStatus.COMPLETED
        project.last_analysis_at = _dt.datetime.now()
        self._repo.save(project)

        _logger.info("Report %s built for project %s", report.id, project.id)
        return report

    def list_reports(self, project: Project) -> list[AnalysisReport]:
        return self._repo.list_reports(project)

    def latest(self, project: Project) -> AnalysisReport | None:
        reports = self._repo.list_reports(project)
        return reports[0] if reports else None

    def to_html(self, report: AnalysisReport) -> str:
        return render_html(report)
