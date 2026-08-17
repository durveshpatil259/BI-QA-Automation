"""Pydantic request/response models for the HTTP API.

Kept separate from the domain dataclasses on purpose: the wire format can evolve
(field renames, omissions, added summaries) without touching the models the
pipeline reasons about.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.core.constants import BIPlatform, DatasourceType, SqlAuthMode


# --- projects --------------------------------------------------------------
class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    bi_platform: BIPlatform = BIPlatform.POWER_BI
    #: Free text rather than an enum: which environments an organisation runs
    #: is its own business, and rejecting "Pre-prod" would be the tool telling
    #: the user their process is wrong. Recorded for context on a result.
    environment: str = ""


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    bi_platform: str
    environment: str = ""
    processing_time_ms: int | None = None
    status: str
    dashboard_files: list[str] = []
    created_at: str
    updated_at: str


class UploadResponse(BaseModel):
    saved: list[str] = []
    rejected: list[dict] = []


# --- datasource ------------------------------------------------------------
class SqlServerDatasourceRequest(BaseModel):
    type: DatasourceType = DatasourceType.SQL_SERVER
    server: str
    database: str
    auth_mode: SqlAuthMode = SqlAuthMode.SQL_LOGIN
    username: str = ""
    password: str = ""
    driver: str = "ODBC Driver 17 for SQL Server"
    port: int = 1433
    encrypt: bool = True
    trust_server_certificate: bool = True


class FileDatasourceRequest(BaseModel):
    """Excel workbook or CSV file already uploaded to the project."""

    type: DatasourceType = DatasourceType.EXCEL
    sheet_name: str = ""


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str
    details: dict[str, str] = {}


# --- analysis / jobs -------------------------------------------------------
class AnalyzeRequest(BaseModel):
    tolerance_pct: float = Field(1.0, ge=0.0, le=50.0)


class JobSummary(BaseModel):
    tests: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0


class JobResponse(BaseModel):
    job_id: str
    project_id: str
    state: str
    pct: int = 0
    stage: str | None = None
    message: str = ""
    elapsed_ms: int = 0
    error: str = ""
    summary: JobSummary = JobSummary()
    warnings: list[str] = []


# --- results ---------------------------------------------------------------
class ValidationRow(BaseModel):
    test_id: str
    kpi: str
    scenario: str = ""
    dashboard_value: str = ""
    generated_sql: str = ""
    #: How the source value was obtained, in the source's own terms. SQL Server
    #: proves it with the query; a file proves it with sheet/operation/filters.
    source_evidence: str = ""
    database_value: str = ""
    difference: str = ""
    match_type: str = ""
    execution_time_ms: float | None = None
    status: str


class ResultsResponse(BaseModel):
    project_id: str
    summary: JobSummary
    rows: list[ValidationRow] = []
    warnings: list[str] = []


# --- settings --------------------------------------------------------------
class LLMSettingsRequest(BaseModel):
    """Everything the browser is allowed to send.

    Endpoint, credentials and token budget are resolved server-side from the
    provider registry, so no secret can be typed into — or leaked through —
    the frontend.
    """

    # "model_" is a protected Pydantic namespace; these are domain fields.
    model_config = ConfigDict(protected_namespaces=())

    provider: str = "Groq"
    model: str = ""


class LLMSettingsResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: str
    model: str = ""
    model_label: str = ""
    is_configured: bool = False
    #: Whether the *backend* holds a usable key. Never the key itself.
    has_api_key: bool = False


class ProviderOption(BaseModel):
    id: str
    label: str
    #: False when the backend has no credential for this provider.
    configured: bool = True


class ProviderListResponse(BaseModel):
    providers: list[ProviderOption] = []
    selected: str = ""


class ModelOption(BaseModel):
    id: str
    label: str


class ModelListResponse(BaseModel):
    models: list[ModelOption] = []
    default: str = ""
    #: Set when the live catalogue could not be read and the built-in list was
    #: used instead — the UI shows it as a hint, not an error.
    notice: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
    stage: str | None = None
    job_id: str | None = None
