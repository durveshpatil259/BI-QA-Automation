"""Rules that turn datasource-comparison results into validation findings.

The comparison engine produces :class:`ComparisonResult` objects (deterministic
facts about how the dashboard model lines up with the datasource). This rule
elevates the failing/severe ones into first-class findings so they appear in the
validation summary and feed test-case generation.
"""

from __future__ import annotations

from src.domain.models import ValidationFinding
from src.services.rules.base import RuleInput, ValidationRule


class ComparisonFindingsRule(ValidationRule):
    rule_id = "CMP-001"
    category = "data"
    title = "Dashboard model must align with the datasource"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.comparisons:
            return []
        findings: list[ValidationFinding] = []
        for c in data.comparisons:
            if c.matched:
                continue
            findings.append(self._finding(
                title=c.label or self.title,
                description=c.difference or "Mismatch between dashboard and datasource.",
                severity=c.severity, passed=False, entity=c.label,
                expected=c.dashboard_value, actual=c.datasource_value,
            ))
        if not findings:
            return [self._pass("Dashboard model aligns with the datasource.")]
        return findings
