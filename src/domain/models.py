"""Core domain models for BI TestPilot AI.

All models are dataclasses mixing in :class:`SerializableMixin`, so each one can
round-trip to the JSON files that live inside a project folder. They are grouped
into five families:

1. Project & configuration        — :class:`Project`, :class:`DatasourceConfig`,
                                     :class:`LLMSettings`
2. Dashboard metadata             — :class:`Table`, :class:`Column`, ...,
                                     :class:`DashboardMetadata`
3. Visual (screenshot) assets     — :class:`Screenshot`, :class:`VisualAnalysis`
4. Deterministic results          — :class:`ValidationFinding`,
                                     :class:`ComparisonResult`, :class:`DataQueryResult`
5. AI-facing artifacts            — :class:`AnalysisContext`, :class:`TestCase`,
                                     :class:`AnalysisReport`

The domain layer holds NO I/O and NO business rules beyond serialization; it is
the neutral contract every other layer agrees on.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field

from src.core.constants import (
    AnalysisMode,
    AnalysisStatus,
    BIPlatform,
    DatasourceType,
    LLMProvider,
    Priority,
    Severity,
    SqlAuthMode,
    TestCaseKind,
    TestStatus,
)
from src.domain.serialization import SerializableMixin


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ===========================================================================
# 1. Project & configuration
# ===========================================================================
@dataclass
class DatasourceConfig(SerializableMixin):
    """Datasource configuration owned by a project.

    A project has at most one active datasource in the MVP. Fields not relevant
    to the chosen :class:`DatasourceType` are simply left blank.
    """

    type: DatasourceType = DatasourceType.SQL_SERVER
    name: str = "Primary Datasource"

    # --- SQL Server fields ---
    server: str = ""
    database: str = ""
    auth_mode: SqlAuthMode = SqlAuthMode.SQL_LOGIN
    username: str = ""
    password: str = ""          # stored in the project's Configuration folder
    driver: str = "ODBC Driver 17 for SQL Server"
    port: int = 1433
    encrypt: bool = True
    trust_server_certificate: bool = True

    # --- Excel fields ---
    excel_path: str = ""        # relative path within the project or absolute
    sheet_name: str = ""        # optional; blank = first sheet

    # bookkeeping
    is_configured: bool = False
    last_tested_at: _dt.datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str = ""


@dataclass
class LLMSettings(SerializableMixin):
    """Per-project LLM configuration. Users supply their own API keys."""

    provider: LLMProvider = LLMProvider.GROK
    api_key: str = ""
    model: str = ""             # provider-specific default resolved by the engine
    base_url: str = ""          # optional override for self-hosted/proxy endpoints
    temperature: float = 0.2
    # Output token reservation. Kept modest so requests fit free-tier
    # per-minute token limits (e.g. Groq's 12k TPM counts prompt + max_tokens).
    max_tokens: int = 2048
    is_configured: bool = False


@dataclass
class Project(SerializableMixin):
    """A unit of work: one dashboard (and/or screenshots) under QA analysis."""

    id: str = field(default_factory=lambda: _new_id("PRJ"))
    name: str = ""
    description: str = ""
    bi_platform: BIPlatform = BIPlatform.POWER_BI

    created_at: _dt.datetime = field(default_factory=_now)
    updated_at: _dt.datetime = field(default_factory=_now)

    # Auto-determined at run time from which assets exist. Persisted so the UI
    # can display the last-known mode.
    analysis_mode: AnalysisMode | None = None
    status: AnalysisStatus = AnalysisStatus.NOT_STARTED

    # Names (not full paths) of uploaded assets, relative to the project folders.
    dashboard_files: list[str] = field(default_factory=list)
    screenshot_files: list[str] = field(default_factory=list)

    last_analysis_at: _dt.datetime | None = None

    def touch(self) -> None:
        self.updated_at = _now()


# ===========================================================================
# 2. Dashboard metadata (deterministically extracted by Python)
# ===========================================================================
@dataclass
class Column(SerializableMixin):
    name: str = ""
    data_type: str = ""
    is_hidden: bool = False
    is_calculated: bool = False
    dax_expression: str = ""     # populated for calculated columns
    description: str = ""
    format_string: str = ""


@dataclass
class Measure(SerializableMixin):
    name: str = ""
    table: str = ""
    dax_expression: str = ""
    data_type: str = ""
    format_string: str = ""
    is_hidden: bool = False
    description: str = ""
    display_folder: str = ""


@dataclass
class Table(SerializableMixin):
    name: str = ""
    is_hidden: bool = False
    is_calculated: bool = False        # calculated table
    dax_expression: str = ""           # populated for calculated tables
    row_count: int | None = None
    source_query: str = ""             # M / SQL partition query if available
    columns: list[Column] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    description: str = ""


@dataclass
class Relationship(SerializableMixin):
    from_table: str = ""
    from_column: str = ""
    to_table: str = ""
    to_column: str = ""
    cardinality: str = ""              # e.g. "many-to-one"
    cross_filter_direction: str = ""   # e.g. "single" / "both"
    is_active: bool = True


@dataclass
class Filter(SerializableMixin):
    name: str = ""
    scope: str = ""                    # e.g. "page", "visual", "report"
    target_table: str = ""
    target_column: str = ""
    filter_type: str = ""              # e.g. "categorical", "range"
    expression: str = ""


@dataclass
class Visual(SerializableMixin):
    id: str = ""
    title: str = ""
    visual_type: str = ""              # e.g. "barChart", "table", "card"
    page: str = ""
    fields: list[str] = field(default_factory=list)     # bound columns/measures
    filters: list[Filter] = field(default_factory=list)


@dataclass
class Bookmark(SerializableMixin):
    name: str = ""
    display_name: str = ""
    page: str = ""


@dataclass
class Page(SerializableMixin):
    name: str = ""
    display_name: str = ""
    ordinal: int = 0
    width: int | None = None
    height: int | None = None
    visuals: list[Visual] = field(default_factory=list)


@dataclass
class DashboardMetadata(SerializableMixin):
    """Aggregated, deterministically-extracted metadata for one dashboard."""

    platform: BIPlatform = BIPlatform.POWER_BI
    source_file: str = ""
    model_name: str = ""

    tables: list[Table] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    bookmarks: list[Bookmark] = field(default_factory=list)
    report_level_filters: list[Filter] = field(default_factory=list)

    # Free-form extraction notes / warnings (e.g. "encrypted PBIX, DAX omitted").
    extraction_warnings: list[str] = field(default_factory=list)
    extracted_at: _dt.datetime = field(default_factory=_now)

    # --- Convenience roll-ups (computed, not persisted authoritative source) --
    @property
    def all_measures(self) -> list[Measure]:
        return [m for t in self.tables for m in t.measures]

    @property
    def all_visuals(self) -> list[Visual]:
        return [v for p in self.pages for v in p.visuals]

    def summary_counts(self) -> dict[str, int]:
        return {
            "tables": len(self.tables),
            "columns": sum(len(t.columns) for t in self.tables),
            "measures": len(self.all_measures),
            "calculated_columns": sum(
                1 for t in self.tables for c in t.columns if c.is_calculated
            ),
            "calculated_tables": sum(1 for t in self.tables if t.is_calculated),
            "relationships": len(self.relationships),
            "pages": len(self.pages),
            "visuals": len(self.all_visuals),
            "bookmarks": len(self.bookmarks),
        }


# ===========================================================================
# 3. Visual (screenshot) assets
# ===========================================================================
@dataclass
class Screenshot(SerializableMixin):
    file_name: str = ""
    width: int | None = None
    height: int | None = None
    format: str = ""
    size_bytes: int | None = None
    # Populated by the screenshot-processing module (later).
    detected_text: str = ""
    notes: str = ""


@dataclass
class VisualAnalysis(SerializableMixin):
    """Deterministic facts extracted from screenshots (OCR, dimensions, etc.).
    AI reasoning over visuals happens later using this as input."""

    screenshots: list[Screenshot] = field(default_factory=list)
    total_screenshots: int = 0
    warnings: list[str] = field(default_factory=list)


# ===========================================================================
# 4. Deterministic results (Python-produced; never AI-produced)
# ===========================================================================
@dataclass
class ValidationFinding(SerializableMixin):
    """A single deterministic validation result."""

    id: str = field(default_factory=lambda: _new_id("VF"))
    rule_id: str = ""
    category: str = ""                 # e.g. "metadata", "data", "visual"
    title: str = ""
    description: str = ""
    severity: Severity = Severity.INFO
    passed: bool = True
    entity: str = ""                   # what it refers to (table/measure/visual)
    expected: str = ""
    actual: str = ""


@dataclass
class DataQueryResult(SerializableMixin):
    """Result of one datasource query executed by Python (never by the LLM)."""

    label: str = ""
    query: str = ""                    # SQL text or Excel range description
    row_count: int | None = None
    columns: list[str] = field(default_factory=list)
    sample_rows: list[list[str]] = field(default_factory=list)  # stringified
    scalar_value: str = ""             # for single-value checks
    error: str = ""


@dataclass
class ComparisonResult(SerializableMixin):
    """Outcome of comparing dashboard metadata against the datasource."""

    label: str = ""
    dashboard_value: str = ""
    datasource_value: str = ""
    matched: bool = True
    difference: str = ""
    severity: Severity = Severity.INFO


# ===========================================================================
# 5. AI-facing artifacts
# ===========================================================================
@dataclass
class TestCase(SerializableMixin):
    """Enterprise-format test case. Python populates actual_result / status /
    remarks after deterministic validation; the AI generates the rest."""

    test_case_id: str = field(default_factory=lambda: _new_id("TC"))
    kind: TestCaseKind = TestCaseKind.QA
    module: str = ""
    test_scenario: str = ""
    test_steps: str = ""
    test_data: str = ""
    expected_result: str = ""
    actual_result: str = ""
    status: TestStatus = TestStatus.NOT_EXECUTED
    priority: Priority = Priority.MEDIUM
    remarks: str = ""

    # --- data-validation columns (populated for SQL validation tests) ------
    generated_sql: str = ""
    dashboard_value: str = ""
    database_value: str = ""
    difference: str = ""
    execution_time_ms: float | None = None
    confidence_score: float | None = None


# ===========================================================================
# 6. Datasource schema (redesign — read by Python, mapped by AI)
# ===========================================================================
@dataclass
class DbColumn(SerializableMixin):
    name: str = ""
    data_type: str = ""
    nullable: bool = True
    is_primary_key: bool = False
    # A few real distinct values, profiled for low-cardinality text columns.
    # Critical for AI SQL generation: it must know a fiscal year literal looks
    # like 'FY2020' (not 2020) before it can write a correct WHERE clause.
    sample_values: list[str] = field(default_factory=list)


@dataclass
class JoinHint(SerializableMixin):
    """An inferred join path between two tables.

    Real foreign keys are often absent (e.g. tables imported from CSV), so the
    schema reader infers likely joins from key-column naming. These hints are
    given to the AI so it can write correct multi-table queries.
    """

    from_table: str = ""
    from_column: str = ""
    to_table: str = ""
    to_column: str = ""
    inferred: bool = True        # False when it came from a declared FK


@dataclass
class DbForeignKey(SerializableMixin):
    column: str = ""
    ref_table: str = ""              # schema-qualified referenced table
    ref_column: str = ""
    constraint_name: str = ""


@dataclass
class DbTable(SerializableMixin):
    schema: str = ""                 # e.g. "dbo" (empty for Excel sheets)
    name: str = ""
    kind: str = "table"             # "table" | "view" | "sheet"
    columns: list[DbColumn] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    foreign_keys: list[DbForeignKey] = field(default_factory=list)
    row_count: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class DbSchema(SerializableMixin):
    """Deterministically-read datasource schema. Feeds the AI semantic mapping."""

    datasource_type: DatasourceType | None = None
    database: str = ""
    tables: list[DbTable] = field(default_factory=list)
    join_hints: list[JoinHint] = field(default_factory=list)
    generated_at: _dt.datetime = field(default_factory=_now)
    warnings: list[str] = field(default_factory=list)

    def summary_counts(self) -> dict[str, int]:
        return {
            "tables": len(self.tables),
            "columns": sum(len(t.columns) for t in self.tables),
            "primary_keys": sum(len(t.primary_keys) for t in self.tables),
            "foreign_keys": sum(len(t.foreign_keys) for t in self.tables),
        }

    def compact_text(self, max_tables: int = 60) -> str:
        """A compact textual rendering for inclusion in an AI prompt.

        Includes real sample values and inferred join paths — without these the
        model cannot write correct WHERE clauses or multi-table joins.
        """
        lines: list[str] = ["TABLES:"]
        for t in self.tables[:max_tables]:
            lines.append(f"  {t.full_name} [{t.kind}]"
                         + (f" ~{t.row_count} rows" if t.row_count is not None else ""))
            for c in t.columns:
                marks = " (PK)" if c.is_primary_key else ""
                samples = ""
                if c.sample_values:
                    shown = ", ".join(repr(v) for v in c.sample_values[:6])
                    samples = f"  e.g. {shown}"
                lines.append(f"      {c.name}: {c.data_type}{marks}{samples}")
            for fk in t.foreign_keys:
                lines.append(
                    f"      FK {t.full_name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}"
                )
        if len(self.tables) > max_tables:
            lines.append(f"  … and {len(self.tables) - max_tables} more tables")

        if self.join_hints:
            lines.append("")
            lines.append("JOIN PATHS (use these to join fact and dimension tables):")
            for j in self.join_hints:
                tag = " [inferred from naming]" if j.inferred else " [declared FK]"
                lines.append(
                    f"  {j.from_table}.{j.from_column} = {j.to_table}.{j.to_column}{tag}"
                )
        return "\n".join(lines)


# ===========================================================================
# 7. Dashboard understanding & data validation (redesign)
# ===========================================================================
@dataclass
class DashboardKPI(SerializableMixin):
    """A single KPI/metric read off the dashboard (screenshot or model)."""

    name: str = ""
    raw_value: str = ""              # as displayed, e.g. "109.81M", "11.4%"
    numeric_value: float | None = None   # parsed by Python, e.g. 109810000.0
    unit: str = ""                   # "", "%", "currency"
    source: str = ""                 # "screenshot" | "metadata"


@dataclass
class DetectedVisual(SerializableMixin):
    """A visual detected on the dashboard (screenshot or report layout)."""

    visual_type: str = ""            # kpi_card, bar_chart, line_chart, table,
                                     # matrix, slicer, gauge, donut, treemap, map…
    title: str = ""
    fields: list[str] = field(default_factory=list)
    text: str = ""                   # visible labels / values
    page: str = ""
    source: str = ""                 # "screenshot" | "metadata"


@dataclass
class DashboardFilter(SerializableMixin):
    """A slicer/filter and its currently-selected value(s).

    The selection is essential context: a KPI reading 51.88M under
    ``Fiscal Year = FY2020`` must be validated with that same WHERE clause.
    """

    name: str = ""
    selected: str = ""               # e.g. "FY2020", "All"

    @property
    def is_active(self) -> bool:
        """True when the slicer narrows the data (i.e. not 'All'/blank)."""
        s = self.selected.strip().casefold()
        return bool(s) and s not in ("all", "(all)", "none", "-")


@dataclass
class DashboardView(SerializableMixin):
    """One screenshot = one filter scenario.

    A dashboard shows different numbers under different slicer selections, so
    each screenshot is captured as its own view: the filters that were applied
    when it was taken, and the KPI values displayed under them. Uploading one
    screenshot per fiscal year therefore yields one validation scenario per year.
    """

    name: str = ""                   # source screenshot file name
    kpis: list[DashboardKPI] = field(default_factory=list)
    visuals: list[DetectedVisual] = field(default_factory=list)
    filter_selections: list[DashboardFilter] = field(default_factory=list)
    visible_text: str = ""

    def active_filters(self) -> list[DashboardFilter]:
        return [f for f in self.filter_selections if f.is_active]

    def scenario_label(self) -> str:
        """Human label for this filter scenario, e.g. 'Fiscal Year=FY2020'."""
        active = self.active_filters()
        if not active:
            return "No filters"
        return ", ".join(f"{f.name}={f.selected}" for f in active)


@dataclass
class DashboardExtraction(SerializableMixin):
    """Structured understanding of a dashboard, from AI vision and/or metadata."""

    source: str = ""                 # "screenshot" | "pbix" | "combined"
    views: list[DashboardView] = field(default_factory=list)
    # Flattened across all views (kept for back-compat and simple displays).
    kpis: list[DashboardKPI] = field(default_factory=list)
    visuals: list[DetectedVisual] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    filter_selections: list[DashboardFilter] = field(default_factory=list)

    def active_filters(self) -> list[DashboardFilter]:
        return [f for f in self.filter_selections if f.is_active]
    visible_text: str = ""
    generated_at: _dt.datetime = field(default_factory=_now)
    provider: LLMProvider | None = None
    model: str = ""
    raw_response: str = ""


@dataclass
class ValidationPlanItem(SerializableMixin):
    """AI-produced mapping of one KPI to its datasource query (no execution)."""

    id: str = field(default_factory=lambda: _new_id("VP"))
    kpi_name: str = ""
    dashboard_value: str = ""
    table: str = ""
    column: str = ""
    aggregation: str = ""            # SUM / AVG / COUNT / DISTINCTCOUNT…
    business_meaning: str = ""
    filters: list[str] = field(default_factory=list)
    generated_sql: str = ""          # the SQL Python will execute
    confidence: float = 0.0
    # Which filter scenario (screenshot/view) this item validates, e.g.
    # "Fiscal Year=FY2020" — so the same KPI can be validated per year.
    scenario: str = ""
    view_name: str = ""


@dataclass
class ValidationPlan(SerializableMixin):
    """A full validation plan: one item per KPI, with generated SQL."""

    items: list[ValidationPlanItem] = field(default_factory=list)
    generated_at: _dt.datetime = field(default_factory=_now)
    provider: LLMProvider | None = None
    model: str = ""
    raw_response: str = ""


@dataclass
class SqlValidationResult(SerializableMixin):
    """Outcome of executing one plan item's SQL and comparing to the dashboard.

    Produced entirely by Python (execution + comparison); the AI only supplies
    the optional recommendation text when a failure is explained later.
    """

    test_id: str = field(default_factory=lambda: _new_id("QA"))
    kpi_name: str = ""
    dashboard_value: str = ""
    dashboard_numeric: float | None = None
    generated_sql: str = ""
    database_value: str = ""
    database_numeric: float | None = None
    difference: str = ""
    difference_pct: float | None = None
    tolerance_pct: float = 1.0
    execution_time_ms: float | None = None
    execution_status: str = ""       # "ok" | "error"
    status: TestStatus = TestStatus.NOT_EXECUTED
    reason: str = ""
    recommendation: str = ""
    confidence: float = 0.0
    # "exact" when the database returned the dashboard's displayed string
    # verbatim; "numeric" when it matched only after numeric normalisation.
    match_type: str = ""
    scenario: str = ""               # filter scenario validated, e.g. FY2020


@dataclass
class DataValidationRun(SerializableMixin):
    """Persisted collection of SQL validation results for a project."""

    results: list[SqlValidationResult] = field(default_factory=list)
    generated_at: _dt.datetime = field(default_factory=_now)

    def summary(self) -> dict[str, int]:
        passed = sum(1 for r in self.results if r.status == TestStatus.PASS)
        failed = sum(1 for r in self.results if r.status == TestStatus.FAIL)
        errored = sum(1 for r in self.results if r.execution_status == "error")
        return {
            "total": len(self.results),
            "passed": passed,
            "failed": failed,
            "errors": errored,
        }


@dataclass
class AnalysisContext(SerializableMixin):
    """The single, complete, deterministic context assembled by Python before
    ANY LLM call. This is the only thing the LLM is given to reason over.

    It bundles metadata, visual facts, datasource query results, comparison
    outcomes and validation findings — everything the AI needs and nothing it
    must compute itself.
    """

    project_id: str = ""
    project_name: str = ""
    platform: BIPlatform = BIPlatform.POWER_BI
    analysis_mode: AnalysisMode = AnalysisMode.METADATA
    generated_at: _dt.datetime = field(default_factory=_now)

    metadata: DashboardMetadata | None = None
    visual_analysis: VisualAnalysis | None = None
    datasource_type: DatasourceType | None = None
    data_results: list[DataQueryResult] = field(default_factory=list)
    comparisons: list[ComparisonResult] = field(default_factory=list)
    validations: list[ValidationFinding] = field(default_factory=list)

    def validation_summary(self) -> dict[str, int]:
        passed = sum(1 for v in self.validations if v.passed)
        return {
            "total": len(self.validations),
            "passed": passed,
            "failed": len(self.validations) - passed,
            "critical": sum(
                1 for v in self.validations
                if not v.passed and v.severity == Severity.CRITICAL
            ),
        }


@dataclass
class AIReasoning(SerializableMixin):
    """AI-generated narrative produced by the LLM engine (Module 8).

    Stored separately from the deterministic :class:`AnalysisContext` so the
    boundary is explicit: everything here is model output, reasoning strictly
    over the deterministic context it was given — never new data.
    """

    provider: LLMProvider | None = None
    model: str = ""
    generated_at: _dt.datetime = field(default_factory=_now)

    executive_summary: str = ""
    root_cause_analysis: str = ""
    recommendations: list[str] = field(default_factory=list)

    # Raw model text kept for traceability/debugging.
    raw_response: str = ""


@dataclass
class AnalysisReport(SerializableMixin):
    """Final report combining deterministic results with AI-generated content."""

    id: str = field(default_factory=lambda: _new_id("RPT"))
    project_id: str = ""
    project_name: str = ""
    platform: BIPlatform = BIPlatform.POWER_BI
    analysis_mode: AnalysisMode = AnalysisMode.METADATA
    status: AnalysisStatus = AnalysisStatus.COMPLETED
    created_at: _dt.datetime = field(default_factory=_now)

    llm_provider: LLMProvider | None = None
    llm_model: str = ""

    # AI-generated narrative sections.
    executive_summary: str = ""
    root_cause_analysis: str = ""
    recommendations: list[str] = field(default_factory=list)

    # Generated + auto-populated test cases.
    test_cases: list[TestCase] = field(default_factory=list)

    # Deterministic evidence carried into the report for traceability.
    validation_summary: dict[str, int] = field(default_factory=dict)
    findings: list[ValidationFinding] = field(default_factory=list)
    comparisons: list[ComparisonResult] = field(default_factory=list)

    # Data-validation results (dashboard value vs executed SQL).
    sql_validations: list[SqlValidationResult] = field(default_factory=list)
    data_validation_summary: dict[str, int] = field(default_factory=dict)
