"""Deterministic validation rules over dashboard metadata.

Each rule is small, single-purpose (SRP) and returns a summary finding plus one
finding per offending entity so reports and generated test cases can point at
exactly what failed.
"""

from __future__ import annotations

from src.core.constants import Severity
from src.domain.models import ValidationFinding
from src.services.rules.base import RuleInput, ValidationRule


class MeasuresHaveDaxRule(ValidationRule):
    rule_id = "MD-001"
    category = "metadata"
    title = "Measures must define a DAX expression"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        offenders = [m for m in data.metadata.all_measures if not m.dax_expression.strip()]
        if not offenders:
            return [self._pass("All measures define a DAX expression.")]
        return [self._finding(
            title=self.title,
            description=f"{m.name} in table '{m.table}' has no DAX expression.",
            severity=Severity.ERROR, passed=False,
            entity=f"{m.table}[{m.name}]", expected="Non-empty DAX", actual="(empty)",
        ) for m in offenders]


class TablesHaveColumnsRule(ValidationRule):
    rule_id = "MD-002"
    category = "metadata"
    title = "Tables must contain at least one column"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        offenders = [t for t in data.metadata.tables if not t.columns]
        if not offenders:
            return [self._pass("All tables contain at least one column.")]
        return [self._finding(
            title=self.title,
            description=f"Table '{t.name}' has no columns.",
            severity=Severity.WARNING, passed=False, entity=t.name,
            expected=">= 1 column", actual="0 columns",
        ) for t in offenders]


class RelationshipIntegrityRule(ValidationRule):
    rule_id = "MD-003"
    category = "metadata"
    title = "Relationships must reference existing tables and columns"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        tables = {t.name: {c.name for c in t.columns} for t in data.metadata.tables}
        findings: list[ValidationFinding] = []
        for r in data.metadata.relationships:
            problems = []
            for side, tbl, col in (
                ("from", r.from_table, r.from_column),
                ("to", r.to_table, r.to_column),
            ):
                if tbl not in tables:
                    problems.append(f"{side} table '{tbl}' not found")
                elif col and col not in tables[tbl]:
                    problems.append(f"{side} column '{tbl}[{col}]' not found")
            if problems:
                findings.append(self._finding(
                    title=self.title,
                    description="; ".join(problems),
                    severity=Severity.CRITICAL, passed=False,
                    entity=f"{r.from_table}->{r.to_table}",
                    expected="Valid table/column references", actual="Broken reference",
                ))
        if not findings and data.metadata.relationships:
            return [self._pass("All relationships reference valid tables and columns.")]
        return findings


class InactiveRelationshipRule(ValidationRule):
    rule_id = "MD-004"
    category = "metadata"
    title = "Inactive relationships present"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        inactive = [r for r in data.metadata.relationships if not r.is_active]
        return [self._finding(
            title=self.title,
            description=(
                f"Relationship {r.from_table}->{r.to_table} is inactive "
                "(requires USERELATIONSHIP to take effect)."
            ),
            severity=Severity.INFO, passed=True,
            entity=f"{r.from_table}->{r.to_table}",
        ) for r in inactive]


class DuplicateMeasureNameRule(ValidationRule):
    rule_id = "MD-005"
    category = "metadata"
    title = "Measure names should be unique across the model"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        seen: dict[str, list[str]] = {}
        for m in data.metadata.all_measures:
            seen.setdefault(m.name.casefold(), []).append(m.table)
        dups = {name: tbls for name, tbls in seen.items() if len(tbls) > 1}
        if not dups:
            return [self._pass("Measure names are unique across the model.")]
        return [self._finding(
            title=self.title,
            description=f"Measure '{name}' is defined in tables: {', '.join(tbls)}.",
            severity=Severity.WARNING, passed=False, entity=name,
            expected="Unique measure name", actual=f"{len(tbls)} definitions",
        ) for name, tbls in dups.items()]


class VisualsHaveFieldsRule(ValidationRule):
    rule_id = "MD-006"
    category = "metadata"
    title = "Visuals should bind at least one field"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        offenders = [v for v in data.metadata.all_visuals if not v.fields]
        if not offenders:
            return [self._pass("All visuals bind at least one field.")]
        return [self._finding(
            title=self.title,
            description=(
                f"Visual '{v.title or v.id}' ({v.visual_type or 'unknown'}) on page "
                f"'{v.page}' has no bound fields."
            ),
            severity=Severity.WARNING, passed=False,
            entity=v.title or v.id, expected=">= 1 field", actual="0 fields",
        ) for v in offenders]


class PagesHaveVisualsRule(ValidationRule):
    rule_id = "MD-007"
    category = "metadata"
    title = "Report pages should contain visuals"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        offenders = [p for p in data.metadata.pages if not p.visuals]
        if not offenders:
            return [self._pass("All report pages contain visuals.")]
        return [self._finding(
            title=self.title,
            description=f"Page '{p.display_name}' has no visuals.",
            severity=Severity.WARNING, passed=False,
            entity=p.display_name, expected=">= 1 visual", actual="0 visuals",
        ) for p in offenders]


class BookmarkTargetRule(ValidationRule):
    rule_id = "MD-008"
    category = "metadata"
    title = "Bookmarks should target an existing page"

    def evaluate(self, data: RuleInput) -> list[ValidationFinding]:
        if not data.metadata:
            return []
        page_names = {p.name for p in data.metadata.pages}
        offenders = [
            b for b in data.metadata.bookmarks
            if b.page and page_names and b.page not in page_names
        ]
        if not offenders:
            return []
        return [self._finding(
            title=self.title,
            description=f"Bookmark '{b.display_name}' targets missing page '{b.page}'.",
            severity=Severity.WARNING, passed=False, entity=b.display_name,
            expected="Existing page", actual=b.page,
        ) for b in offenders]
