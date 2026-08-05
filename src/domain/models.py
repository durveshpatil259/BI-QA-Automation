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
