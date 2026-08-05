"""Rule engine contract and shared input bundle."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from src.core.constants import Severity
from src.domain.models import (
    ComparisonResult,
    DashboardMetadata,
    ValidationFinding,
    VisualAnalysis,
)


@dataclass
class RuleInput:
    """Everything a rule may inspect. Any field may be ``None``/empty depending
    on the analysis mode (e.g. no metadata in Visual-only mode)."""

    metadata: DashboardMetadata | None = None
    visual: VisualAnalysis | None = None
    comparisons: list[ComparisonResult] = field(default_factory=list)


class ValidationRule(abc.ABC):
    """A single deterministic check producing zero or more findings."""

    #: Stable identifier, e.g. "MD-001". Used in reports and test-case traceability.
    rule_id: str = ""
    #: Grouping category, e.g. "metadata", "visual", "data".
    category: str = ""
    #: Human-readable rule name.
    title: str = ""

    @abc.abstractmethod
    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        """Return findings for this rule (empty list == nothing to report)."""

    # --- convenience finding builders ------------------------------------
    def _finding(
        self,
        *,
        title: str,
        description: str,
        severity: Severity,
        passed: bool,
        entity: str = "",
        expected: str = "",
        actual: str = "",
    ) -> ValidationFinding:
        return ValidationFinding(
            rule_id=self.rule_id,
            category=self.category,
            title=title or self.title,
            description=description,
            severity=severity,
            passed=passed,
            entity=entity,
            expected=expected,
            actual=actual,
        )

    def _pass(self, description: str, entity: str = "") -> ValidationFinding:
        return self._finding(
            title=self.title, description=description,
            severity=Severity.INFO, passed=True, entity=entity,
        )
