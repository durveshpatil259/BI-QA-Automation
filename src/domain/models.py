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
import re
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


#: Affixes ETL tools add when landing a dimension/fact table in a warehouse.
_TABLE_AFFIXES = ("_data", "_dim", "_fact", "_tbl", "_table", "_dimension")
_TABLE_PREFIXES = ("dim_", "fact_", "tbl_", "stg_")


def normalise_table_name(name: str) -> str:
    """Compare table names across naming conventions.

    ``Sales Territory``, ``Sales_Territory_data`` and ``dim_salesterritory``
    all collapse to ``salesterritory`` so a model table can be matched to the
    warehouse table that actually loads it.
    """
    bare = (name or "").rsplit(".", 1)[-1].casefold().strip()
    for affix in _TABLE_AFFIXES:
        if bare.endswith(affix):
            bare = bare[: -len(affix)]
            break
    for prefix in _TABLE_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    return re.sub(r"[^a-z0-9]", "", bare)


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

    def relevant_tables(self, wanted: set[str]) -> list[DbTable]:
        """Tables matching *wanted* names, plus anything they join to.

        A warehouse may hold 50 tables while the dashboard touches 11. Sending
        all of them wastes the model's token budget and adds noise that hurts
        mapping accuracy, so the prompt is narrowed to the relevant subgraph.
        """
        if not wanted:
            return list(self.tables)

        target = {normalise_table_name(w) for w in wanted if w}
        by_name = {t.full_name.casefold(): t for t in self.tables}

        # Warehouses rarely name a table exactly as the model does: a model
        # table "Customer" is loaded from "customer_data" / "dim_customer".
        # Matching on the bare name alone kept SalesLT.Customer (an unrelated
        # sample table that happens to match exactly) and dropped the real
        # customer_data, so the AI never saw the columns it needed.
        keep = {
            t.full_name.casefold() for t in self.tables
            if normalise_table_name(t.name) in target
        }
        if not keep:
            return list(self.tables)

        # Pull in direct join partners — a filter often lives one hop away.
        for hint in self.join_hints:
            a, b = hint.from_table.casefold(), hint.to_table.casefold()
            if a in keep and b in by_name:
                keep.add(b)
            elif b in keep and a in by_name:
                keep.add(a)

        return [t for t in self.tables if t.full_name.casefold() in keep]

    def compact_text(
        self,
        max_tables: int = 60,
        *,
        wanted: set[str] | None = None,
        max_columns: int = 40,
        max_samples: int = 4,
        include_samples: bool = False,
        include_row_counts: bool = False,
    ) -> str:
        """Render the schema for an AI prompt — **identifiers only by default**.

        Security posture: an LLM needs table and column *names* to write SQL at
        all, but it never needs the *contents* of those columns. Sample values
        are real production data (customer names, emails, account numbers) and
        are therefore **off by default**; ``include_samples=True`` is an
        explicit, informed opt-in. Row counts are likewise withheld, as volumes
        can be commercially sensitive.

        Pass *wanted* (the dashboard's table names) to send only the tables the
        report actually uses — least privilege, and less noise for the model.
        """
        tables = self.relevant_tables(wanted or set())
        lines: list[str] = ["TABLES:"]
        for t in tables[:max_tables]:
            counts = (
                f" ~{t.row_count} rows"
                if include_row_counts and t.row_count is not None else ""
            )
            lines.append(f"  {t.full_name} [{t.kind}]{counts}")
            for c in t.columns[:max_columns]:
                marks = " (PK)" if c.is_primary_key else ""
                samples = ""
                if include_samples and c.sample_values:
                    shown = ", ".join(repr(v) for v in c.sample_values[:max_samples])
                    samples = f"  e.g. {shown}"
                lines.append(f"      {c.name}: {c.data_type}{marks}{samples}")
            if len(t.columns) > max_columns:
                lines.append(f"      … and {len(t.columns) - max_columns} more columns")
            for fk in t.foreign_keys:
                lines.append(
                    f"      FK {t.full_name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}"
                )
        if len(tables) > max_tables:
            lines.append(f"  … and {len(tables) - max_tables} more tables")
        if len(tables) < len(self.tables):
            lines.append(
                f"  (showing {len(tables)} of {len(self.tables)} database tables — "
                "those referenced by the dashboard and their join partners)"
            )

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

    # --- multi-value chart/table/matrix support -----------------------
    dimension_field: str = ""        # the category/axis being grouped, e.g. "Category"
    measure_field: str = ""          # what's plotted, e.g. "Sales Amount"
    data_points: list["ChartDataPoint"] = field(default_factory=list)
    # True when exact per-category numbers were readable (data labels, table
    # cells, tooltips); False when only shapes/proportions are visible
    # (unlabeled bars, an unlabeled donut, a colour-shaded map) — in that case
    # only the category SET can be validated, not the values.
    values_visible: bool = False


@dataclass
class ChartDataPoint(SerializableMixin):
    """One (category, value) pair read off a chart, table or matrix cell."""

    dimension: str = ""               # e.g. "Accessories", "Southwest", "2020"
    raw_value: str = ""               # as displayed; "" if not readable
    numeric_value: float | None = None


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

    # --- chart/table/matrix support ---------------------------------------
    # "scalar"     — one KPI card, one number (the original behaviour).
    # "grouped"    — a chart/table with a GROUP BY; generated_sql returns
    #                (dimension, value) rows compared one-by-one.
    # "structural" — chart shows categories but no readable numbers;
    #                generated_sql returns DISTINCT dimension members only,
    #                compared as a set against what the chart displays.
    item_type: str = "scalar"
    visual_title: str = ""           # e.g. "Sales by Category"; blank for KPIs
    dimension_column: str = ""       # the GROUP BY / DISTINCT column used
    expected_points: list["ChartDataPoint"] = field(default_factory=list)


@dataclass
class ValidationPlan(SerializableMixin):
    """A full validation plan: one item per KPI, with generated SQL."""

    items: list[ValidationPlanItem] = field(default_factory=list)
    generated_at: _dt.datetime = field(default_factory=_now)
    provider: LLMProvider | None = None
    model: str = ""
    raw_response: str = ""

    # Partial generation used to be invisible: if 8 of 9 batches failed the
    # plan was still saved, the run still "succeeded", and the report simply
    # showed fewer validations with no hint that most were never generated.
    batches_total: int = 0
    batches_ok: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.batches_total == 0 or self.batches_ok == self.batches_total

    def coverage_note(self) -> str:
        """Human-readable warning when the plan is partial. Empty if complete."""
        if self.is_complete:
            return ""
        failed = self.batches_total - self.batches_ok
        return (
            f"{failed} of {self.batches_total} SQL-generation batches failed, so "
            f"this plan is incomplete — only {len(self.items)} validation(s) were "
            f"generated. First error: {self.errors[0] if self.errors else 'unknown'}"
        )


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
    # Which chart this row belongs to, and which category within it — blank
    # for a plain KPI card. "kpi_name" holds "Sales by Category" for these.
    visual_title: str = ""
    dimension_value: str = ""
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

    # What the run cost, by stage. Free tiers meter tokens per minute AND per
    # day, so knowing which stage spent them is what makes a run tunable.
    token_usage: dict = field(default_factory=dict)
