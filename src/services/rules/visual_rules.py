"""Deterministic validation rules over screenshot (visual) analysis."""

from __future__ import annotations

from src.core.constants import Severity
from src.domain.models import ValidationFinding
from src.services.rules.base import RuleInput, ValidationRule

# Below this width a dashboard screenshot is likely too low-resolution for
# reliable visual QA / OCR.
_MIN_WIDTH = 640


class ScreenshotsReadableRule(ValidationRule):
    rule_id = "VS-001"
    category = "visual"
    title = "Screenshots must be readable images"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.visual:
            return []
        offenders = [s for s in data.visual.screenshots if s.notes]
        if not offenders:
            if data.visual.screenshots:
                return [self._pass("All screenshots are readable.")]
            return []
        return [self._finding(
            title=self.title,
            description=f"{s.file_name}: {s.notes}",
            severity=Severity.ERROR, passed=False, entity=s.file_name,
            expected="Readable image", actual="Unreadable",
        ) for s in offenders]


class ScreenshotResolutionRule(ValidationRule):
    rule_id = "VS-002"
    category = "visual"
    title = "Screenshots should be high enough resolution"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.visual:
            return []
        offenders = [
            s for s in data.visual.screenshots
            if s.width is not None and s.width < _MIN_WIDTH
        ]
        if not offenders:
            return []
        return [self._finding(
            title=self.title,
            description=(
                f"{s.file_name} is {s.width}px wide (< {_MIN_WIDTH}px); low resolution "
                "may reduce visual-QA accuracy."
            ),
            severity=Severity.INFO, passed=True, entity=s.file_name,
            expected=f">= {_MIN_WIDTH}px wide", actual=f"{s.width}px",
        ) for s in offenders]
