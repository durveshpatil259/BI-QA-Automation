"""Rule engine: runs a registry of rules and aggregates their findings."""

from __future__ import annotations

from src.core.logger import get_logger
from src.domain.models import ValidationFinding
from src.services.rules.base import RuleInput, ValidationRule
from src.services.rules.comparison_rules import ComparisonFindingsRule
from src.services.rules.metadata_rules import (
    BookmarkTargetRule,
    DuplicateMeasureNameRule,
    InactiveRelationshipRule,
    MeasuresHaveDaxRule,
    PagesHaveVisualsRule,
    RelationshipIntegrityRule,
    TablesHaveColumnsRule,
    VisualsHaveFieldsRule,
)
from src.services.rules.visual_rules import (
    ScreenshotResolutionRule,
    ScreenshotsReadableRule,
)

_logger = get_logger()


def default_rules() -> list[ValidationRule]:
    """The rule set applied by default. Order defines report ordering."""
    return [
        MeasuresHaveDaxRule(),
        TablesHaveColumnsRule(),
        RelationshipIntegrityRule(),
        InactiveRelationshipRule(),
        DuplicateMeasureNameRule(),
        VisualsHaveFieldsRule(),
        PagesHaveVisualsRule(),
        BookmarkTargetRule(),
        ScreenshotsReadableRule(),
        ScreenshotResolutionRule(),
        ComparisonFindingsRule(),
    ]


class RuleEngine:
    """Executes validation rules over a :class:`RuleInput`."""

    def __init__(self, rules: list[ValidationRule] | None = None):
        self._rules = rules if rules is not None else default_rules()

    @property
    def rules(self) -> list[ValidationRule]:
        return list(self._rules)

    def run(self, data: RuleInput) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        for rule in self._rules:
            try:
                findings.extend(rule.evaluate(data))
            except Exception as exc:  # noqa: BLE001 - never let one rule break the run
                _logger.warning("Rule %s failed: %s", rule.rule_id, exc)
        _logger.info(
            "Rule engine produced %d finding(s) from %d rule(s).",
            len(findings), len(self._rules),
        )
        return findings
