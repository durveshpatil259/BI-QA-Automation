"""Rule-based test expansion (redesign V6).

Deterministically expands every detected KPI, chart, filter, page and model
object into the full enterprise test matrix (Developer + QA suites), and merges
the V5 SQL-validation results (with Generated SQL + PASS/FAIL) as first-class
test cases. This is pure Python — it guarantees coverage/scale regardless of the
LLM, and reuses the executed data-validation verdicts.

Test-type matrix (per the spec):
* KPI card  → Value, SQL, Format, Filter, Null, Refresh, Performance
* Chart     → Chart, Filter, Cross-filter, Tooltip, Formatting, Export, Performance
* Filter    → Filter, Selection
* Page      → Navigation, Export
* Model     → Dataset, Measure, DAX, Relationship, Visual-binding, Formatting, Performance
"""

from __future__ import annotations

from src.core.constants import Priority, TestCaseKind, TestStatus
from src.core.logger import get_logger
from src.domain.models import (
    DashboardExtraction,
    DashboardMetadata,
    DataValidationRun,
    Project,
    TestCase,
)
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class _Evidence:
    """Executed SQL validations for one KPI or chart, split by what they prove.

    A template test may only inherit a verdict when the SQL run actually
    exercised its assertion. "Value Validation" is proven by a scalar
    comparison; "Refresh Validation" is not proven by anything we can run, so
    it stays NOT_EXECUTED rather than being marked passed on adjacency.
    """

    #: A scenario label meaning "no slicer applied".
    _BASELINE = ("all data", "model measures", "")

    def __init__(self) -> None:
        self.baseline: list = []
        self.filtered: list = []
        self.chart: list = []

    def add(self, result) -> None:
        if (result.match_type or "").startswith("chart"):
            self.chart.append(result)
            return
        label = (result.scenario or "").casefold()
        if any(label.startswith(b) for b in self._BASELINE if b):
            self.baseline.append(result)
        elif not label:
            self.baseline.append(result)
        else:
            self.filtered.append(result)

    @staticmethod
    def _verdict(results: list) -> tuple[TestStatus, str]:
        failed = [r for r in results if r.status is TestStatus.FAIL]
        ids = ", ".join(r.test_id for r in results[:6])
        more = f" (+{len(results) - 6} more)" if len(results) > 6 else ""
        if failed:
            return TestStatus.FAIL, (
                f"{len(failed)} of {len(results)} executed SQL validation(s) failed: "
                f"{', '.join(r.test_id for r in failed[:6])}"
            )
        return TestStatus.PASS, f"Proven by executed SQL validation(s): {ids}{more}"

    # --- what each template type may inherit ------------------------------
    def resolve(self, test_type: str):
        """(status, remark) if the SQL run proves this test, else None."""
        if test_type == "Value Validation":
            # A scalar comparison of the KPI against the database is exactly
            # this assertion.
            return self._verdict(self.baseline) if self.baseline else None

        if test_type == "Filter Validation":
            # Each slicer value was validated as its own scenario, so this is
            # proven only when filtered scenarios actually ran.
            return self._verdict(self.filtered) if self.filtered else None

        if test_type == "Format Validation":
            # Only an *exact* match compares the rendered string character for
            # character. A numeric match proves the number, not the format, so
            # it must not satisfy a formatting test.
            exact = [r for r in self.baseline + self.filtered
                     if (r.match_type or "") == "exact"]
            return self._verdict(exact) if exact else None

        if test_type == "Chart Validation":
            return self._verdict(self.chart) if self.chart else None

        # Null/Refresh/Performance/Tooltip/Cross-filter/Export/Binding: nothing
        # we execute touches these, so they stay manual.
        return None

# (test_type, scenario, steps, test_data, expected, priority)
_KPI_QA = [
    ("Value Validation", "Verify KPI '{n}' shows the correct value",
     "1. Open the report.\n2. Read KPI '{n}'.\n3. Compare to source of truth.",
     "Dashboard value: {v}", "KPI value matches the validated source value.", Priority.HIGH),
    ("Format Validation", "Verify KPI '{n}' number format",
     "1. Inspect KPI '{n}' formatting (decimals, currency, %, thousands).",
     "Displayed: {v}", "Format matches the business-defined display format.", Priority.MEDIUM),
    ("Filter Validation", "Verify KPI '{n}' responds to slicers",
     "1. Apply each relevant slicer.\n2. Confirm KPI '{n}' updates correctly.",
     "All page slicers", "KPI recalculates correctly for every filter selection.", Priority.HIGH),
    ("Null/Blank Validation", "Verify KPI '{n}' handles empty data",
     "1. Filter to a combination with no data.\n2. Observe KPI '{n}'.",
     "Empty selection", "KPI shows blank/0 gracefully (no error).", Priority.MEDIUM),
    ("Refresh Validation", "Verify KPI '{n}' after data refresh",
     "1. Refresh the dataset.\n2. Re-read KPI '{n}'.",
     "Latest data", "KPI reflects the refreshed data.", Priority.MEDIUM),
    ("Performance Validation", "Verify KPI '{n}' renders quickly",
     "1. Load the page.\n2. Measure KPI '{n}' render time.",
     "Cold + warm cache", "KPI renders within the agreed performance budget.", Priority.LOW),
]

_CHART_QA = [
    ("Chart Validation", "Verify chart '{n}' plots correct data",
     "1. Inspect chart '{n}'.\n2. Cross-check series/axis values with source.",
     "Chart: {n}", "Chart values match the source data.", Priority.HIGH),
    ("Filter Validation", "Verify chart '{n}' responds to slicers",
     "1. Apply slicers.\n2. Confirm chart '{n}' updates.",
     "All slicers", "Chart updates correctly for each filter.", Priority.HIGH),
    ("Cross-Filter Validation", "Verify chart '{n}' cross-filters other visuals",
     "1. Click a data point in chart '{n}'.\n2. Confirm other visuals cross-filter.",
     "Data point selection", "Cross-filtering behaves as designed.", Priority.MEDIUM),
    ("Tooltip Validation", "Verify chart '{n}' tooltips",
     "1. Hover data points in chart '{n}'.\n2. Inspect tooltip fields/values.",
     "Hover", "Tooltips show correct fields and values.", Priority.LOW),
    ("Formatting Validation", "Verify chart '{n}' formatting",
     "1. Inspect axis, legend, colours, data labels of chart '{n}'.",
     "Visual formatting", "Formatting matches the design standard.", Priority.LOW),
    ("Export Validation", "Verify chart '{n}' export",
     "1. Export chart '{n}' data to Excel/CSV.\n2. Compare exported values.",
     "Export to data", "Exported data matches the displayed chart.", Priority.MEDIUM),
    ("Performance Validation", "Verify chart '{n}' render performance",
     "1. Load page.\n2. Measure chart '{n}' render time.",
     "Cold + warm cache", "Chart renders within the performance budget.", Priority.LOW),
]

_FILTER_QA = [
    ("Filter Validation", "Verify slicer '{n}' filters the report",
     "1. Select values in slicer '{n}'.\n2. Confirm visuals filter accordingly.",
     "Slicer: {n}", "Report filters correctly for slicer '{n}'.", Priority.HIGH),
    ("Selection Validation", "Verify slicer '{n}' multi/single-select + clear",
     "1. Test single, multi and clear-all on slicer '{n}'.",
     "Selection modes", "Slicer selection behaviour is correct.", Priority.MEDIUM),
]

_PAGE_QA = [
    ("Navigation Validation", "Verify navigation to page '{n}'",
     "1. Navigate to page '{n}' via buttons/bookmarks/tabs.",
     "Page: {n}", "Navigation to page '{n}' works with correct state.", Priority.MEDIUM),
    ("Export Validation", "Verify export of page '{n}'",
     "1. Export page '{n}' to PDF/PowerPoint.",
     "Export page", "Exported page matches the on-screen layout.", Priority.LOW),
]

_KPI_DEV = [
    ("Measure Test", "Unit-test the measure behind KPI '{n}'",
     "1. Evaluate the measure in isolation for known inputs.",
     "Known dataset", "Measure returns the expected value.", Priority.HIGH),
    ("DAX Test", "Validate DAX logic for KPI '{n}'",
     "1. Review the DAX (filters, context transition, division-by-zero).",
     "DAX review", "DAX is correct and safe for edge cases.", Priority.HIGH),
    ("Visual Binding Test", "Verify KPI '{n}' field binding",
     "1. Confirm the KPI card binds to the intended measure.",
     "Binding", "KPI is bound to the correct measure.", Priority.MEDIUM),
]

_CHART_DEV = [
    ("Visual Binding Test", "Verify chart '{n}' field bindings",
     "1. Confirm axis/legend/values bind to intended columns/measures.",
     "Binding", "Chart bindings are correct.", Priority.MEDIUM),
    ("Formatting Test", "Verify chart '{n}' conditional formatting rules",
     "1. Review conditional formatting/data colours of chart '{n}'.",
     "Formatting rules", "Formatting rules behave as intended.", Priority.LOW),
]


class TestExpansionService:
    """Builds the full enterprise test suite deterministically."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load(self, project: Project) -> list[TestCase]:
        return self._repo.load_test_cases(project)

    # --- inputs -----------------------------------------------------------
    def _kpis(self, ext: DashboardExtraction | None, md: DashboardMetadata | None):
        if ext and ext.kpis:
            return [(k.name, k.raw_value) for k in ext.kpis if k.name]
        if md:
            return [(m.name, "") for m in md.all_measures[:20] if m.name]
        return []

    def _charts(self, ext: DashboardExtraction | None, md: DashboardMetadata | None):
        if ext and ext.visuals:
            return [(v.title or v.visual_type or "visual") for v in ext.visuals]
        if md:
            return [(v.title or v.id or "visual") for v in md.all_visuals]
        return []

    def _filters(self, ext: DashboardExtraction | None, md: DashboardMetadata | None):
        if ext and ext.filters:
            return list(ext.filters)
        if md:
            return [f.name or f"{f.target_table}[{f.target_column}]"
                    for f in md.report_level_filters]
        return []

    # --- expansion --------------------------------------------------------
    def expand(self, project: Project) -> list[TestCase]:
        ext = self._repo.load_dashboard_extraction(project)
        md = self._repo.load_metadata(project)
        run = self._repo.load_data_validation(project)

        cases: list[TestCase] = []
        # Executed evidence, keyed by KPI / chart name, so a template test can
        # inherit a real verdict instead of sitting at NOT_EXECUTED beside a
        # SQL validation that already proved it.
        evidence = self._index_evidence(run)

        # 1) SQL validation tests (executed) — carry Generated SQL + PASS/FAIL.
        cases.extend(self._sql_validation_cases(run))

        # 2) KPI tests (QA + Dev)
        for name, value in self._kpis(ext, md):
            ev = evidence.get(name.casefold())
            cases.extend(self._from_templates(
                _KPI_QA, TestCaseKind.QA, f"KPI: {name}", name, value, ev))
            cases.extend(self._from_templates(
                _KPI_DEV, TestCaseKind.UNIT, f"KPI: {name}", name, value, ev))

        # 3) Chart tests (QA + Dev)
        for name in self._charts(ext, md):
            ev = evidence.get(name.casefold())
            cases.extend(self._from_templates(
                _CHART_QA, TestCaseKind.QA, f"Chart: {name}", name, "", ev))
            cases.extend(self._from_templates(
                _CHART_DEV, TestCaseKind.UNIT, f"Chart: {name}", name, "", ev))

        # 4) Filter tests (QA)
        for name in self._filters(ext, md):
            cases.extend(self._from_templates(
                _FILTER_QA, TestCaseKind.QA, "Filters", name, ""))

        # 5) Model developer tests (from metadata)
        cases.extend(self._model_dev_cases(md))

        # 6) Report-level QA (pages + security)
        cases.extend(self._report_qa_cases(md))

        self._repo.save_test_cases(project, cases)
        _logger.info(
            "Expanded %d test cases for %s (QA=%d, Dev=%d)",
            len(cases), project.id,
            sum(1 for c in cases if c.kind == TestCaseKind.QA),
            sum(1 for c in cases if c.kind == TestCaseKind.UNIT),
        )
        return cases

    # --- builders ---------------------------------------------------------
    @staticmethod
    def _index_evidence(run: DataValidationRun | None) -> dict[str, _Evidence]:
        """Executed validations grouped by the KPI / chart they exercised."""
        index: dict[str, _Evidence] = {}
        if not run:
            return index
        for result in run.results:
            key = (result.visual_title or result.kpi_name or "").casefold()
            if not key:
                continue
            index.setdefault(key, _Evidence()).add(result)
        return index

    @staticmethod
    def _from_templates(
        templates, kind, module, name, value, evidence: _Evidence | None = None
    ) -> list[TestCase]:
        out = []
        for ttype, scenario, steps, tdata, expected, priority in templates:
            resolved = evidence.resolve(ttype) if evidence else None
            status, remarks = (
                resolved if resolved
                else (TestStatus.NOT_EXECUTED,
                      "Auto-generated; execute manually or via automation.")
            )
            out.append(TestCase(
                kind=kind, module=module,
                test_scenario=f"[{ttype}] " + scenario.format(n=name, v=value or "—"),
                test_steps=steps.format(n=name, v=value or "—"),
                test_data=tdata.format(n=name, v=value or "—"),
                expected_result=expected.format(n=name, v=value or "—"),
                status=status, priority=priority,
                remarks=remarks,
                dashboard_value=value or "",
            ))
        return out

    @staticmethod
    def _sql_validation_cases(run: DataValidationRun | None) -> list[TestCase]:
        if not run:
            return []
        out = []
        for r in run.results:
            remarks = r.reason
            if r.recommendation:
                remarks = (remarks + " | AI: " + r.recommendation).strip(" |")
            out.append(TestCase(
                test_case_id=r.test_id,
                kind=TestCaseKind.QA, module=f"SQL Validation: {r.kpi_name}",
                test_scenario=f"[SQL Validation] Validate KPI '{r.kpi_name}' against the database",
                test_steps=(
                    "1. Read the dashboard KPI value.\n"
                    "2. Execute the generated SQL against the datasource.\n"
                    "3. Compare within tolerance."
                ),
                test_data=f"Tolerance {r.tolerance_pct}%",
                expected_result="Dashboard value equals the database value (within tolerance).",
                actual_result=r.database_value or r.reason,
                status=r.status, priority=Priority.HIGH, remarks=remarks,
                generated_sql=r.generated_sql, dashboard_value=r.dashboard_value,
                database_value=r.database_value, difference=r.difference,
                execution_time_ms=r.execution_time_ms, confidence_score=r.confidence,
            ))
        return out

    def _model_dev_cases(self, md: DashboardMetadata | None) -> list[TestCase]:
        if not md:
            return []
        out: list[TestCase] = []
        for t in md.tables:
            out.append(self._dev(f"Dataset: {t.name}", "Dataset Test",
                f"Validate dataset '{t.name}' loads with expected columns/rows",
                f"1. Refresh '{t.name}'.\n2. Verify column count and row count.",
                "Dataset refresh", "Dataset loads with expected schema and rows.",
                Priority.HIGH))
        for m in md.all_measures:
            out.append(self._dev(f"Measure: {m.table}[{m.name}]", "Measure Test",
                f"Validate measure '{m.name}' returns correct results",
                "1. Evaluate the measure for known inputs.",
                "Known dataset", "Measure returns the expected value.", Priority.HIGH))
            if m.dax_expression:
                out.append(self._dev(f"Measure: {m.table}[{m.name}]", "DAX Test",
                    f"Validate DAX for measure '{m.name}'",
                    "1. Review DAX for context, filters, div-by-zero.",
                    m.dax_expression[:120], "DAX is correct and edge-case safe.",
                    Priority.MEDIUM))
        for r in md.relationships:
            out.append(self._dev("Relationships", "Relationship Test",
                f"Validate relationship {r.from_table}->{r.to_table}",
                "1. Verify cardinality, cross-filter direction and referential integrity.",
                f"{r.cardinality}, {r.cross_filter_direction}, active={r.is_active}",
                "Relationship is correct and keys are consistent.", Priority.HIGH))
        out.append(self._dev("Performance", "Performance Test",
            "Validate overall model/report performance",
            "1. Measure page load and visual render times under load.",
            "Performance run", "Report meets the performance budget.", Priority.MEDIUM))
        return out

    def _report_qa_cases(self, md: DashboardMetadata | None) -> list[TestCase]:
        out: list[TestCase] = []
        pages = md.pages if md else []
        for p in pages:
            out.extend(self._from_templates(
                _PAGE_QA, TestCaseKind.QA, "Navigation", p.display_name, ""))
        out.append(TestCase(
            kind=TestCaseKind.QA, module="Security",
            test_scenario="[Security Validation] Verify row-level security (RLS)",
            test_steps="1. View as each RLS role.\n2. Confirm data is correctly restricted.",
            test_data="RLS roles", expected_result="Each role sees only permitted data.",
            status=TestStatus.NOT_EXECUTED, priority=Priority.HIGH,
            remarks="Auto-generated; requires RLS roles configured.",
        ))
        return out

    @staticmethod
    def _dev(module, ttype, scenario, steps, tdata, expected, priority) -> TestCase:
        return TestCase(
            kind=TestCaseKind.UNIT, module=module,
            test_scenario=f"[{ttype}] {scenario}", test_steps=steps,
            test_data=tdata, expected_result=expected,
            status=TestStatus.NOT_EXECUTED, priority=priority,
            remarks="Auto-generated developer test.",
        )
