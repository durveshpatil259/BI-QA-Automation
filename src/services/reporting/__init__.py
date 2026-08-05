"""Report generation.

Combines the deterministic :class:`AnalysisContext`, the AI-generated
:class:`AIReasoning` and the generated test cases into a single
:class:`AnalysisReport`, persists it, and renders exportable HTML.
"""

from src.services.reporting.report_service import ReportService

__all__ = ["ReportService"]
