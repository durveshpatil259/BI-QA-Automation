"""Pydantic request/response models for the HTTP API.

Kept separate from the domain dataclasses on purpose: the wire format can evolve
(field renames, omissions, added summaries) without touching the models the
pipeline reasons about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.core.constants import BIPlatform, DatasourceType, SqlAuthMode


# --- projects --------------------------------------------------------------
class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    bi_platform: BIPlatform = BIPlatform.POWER_BI


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    bi_platform: str
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
    provider: str = "Grok"
    #: Blank means "keep the stored key" — the UI never receives the secret.
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    temperature: float = Field(0.2, ge=0.0, le=1.0)
    max_tokens: int = Field(2048, ge=256, le=32000)


class LLMSettingsResponse(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048
    is_configured: bool = False
    has_api_key: bool = False
    providers: list[str] = []
    presets: dict[str, dict] = {}


class ModelListResponse(BaseModel):
    models: list[str] = []


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
    stage: str | None = None
    job_id: str | None = None
