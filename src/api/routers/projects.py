"""Project, upload and datasource endpoints — screen 1 of the UI."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from src.api.deps import Container, container
from src.api.schemas import (
    ConnectionTestResponse,
    CreateProjectRequest,
    FileDatasourceRequest,
    ProjectResponse,
    SqlServerDatasourceRequest,
    UploadResponse,
)
from src.core.constants import DatasourceType
from src.domain.models import DatasourceConfig, Project
from src.storage import file_manager as fm

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _to_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        bi_platform=str(project.bi_platform),
        status=str(project.status),
        dashboard_files=list(project.dashboard_files),
        created_at=project.created_at.isoformat(),
        updated_at=project.updated_at.isoformat(),
    )


def _load(c: Container, project_id: str) -> Project:
    return c.project_service.get_project(project_id)


@router.post("", response_model=ProjectResponse, status_code=201)
def create_project(body: CreateProjectRequest, c: Container = Depends(container)):
    project = c.project_service.create_project(
        name=body.name, bi_platform=body.bi_platform, description=body.description
    )
    return _to_response(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(c: Container = Depends(container)):
    return [_to_response(p) for p in c.project_service.list_projects()]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, c: Container = Depends(container)):
    return _to_response(_load(c, project_id))


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, c: Container = Depends(container)):
    c.project_service.delete_project(project_id)


@router.post("/{project_id}/pbix", response_model=UploadResponse)
async def upload_pbix(
    project_id: str,
    files: list[UploadFile] = File(...),
    c: Container = Depends(container),
):
    """Upload one or more dashboard files (.pbix / .pbit / .pbip / .zip)."""
    project = _load(c, project_id)
    payload = [(f.filename or "upload", await f.read()) for f in files]
    results = c.upload_service.save_dashboard_files(project, payload)
    return UploadResponse(
        saved=[r.file_name for r in results if r.ok],
        rejected=[{"file": r.file_name, "reason": r.message}
                  for r in results if not r.ok],
    )


@router.post("/{project_id}/datasource/sql", response_model=ConnectionTestResponse)
def configure_sql_datasource(
    project_id: str,
    body: SqlServerDatasourceRequest,
    test: bool = True,
    c: Container = Depends(container),
):
    """Save a SQL Server datasource, testing the connection by default."""
    project = _load(c, project_id)
    cfg = DatasourceConfig(
        type=DatasourceType.SQL_SERVER,
        server=body.server, database=body.database, auth_mode=body.auth_mode,
        username=body.username, password=body.password, driver=body.driver,
        port=body.port, encrypt=body.encrypt,
        trust_server_certificate=body.trust_server_certificate,
    )
    if test:
        result = c.datasource_service.test_connection(project, cfg)
        return ConnectionTestResponse(
            ok=result.ok, message=result.message, details=result.details
        )
    c.datasource_service.save(project, cfg)
    return ConnectionTestResponse(ok=True, message="Datasource saved (not tested).")


@router.post("/{project_id}/datasource/file", response_model=ConnectionTestResponse)
async def configure_file_datasource(
    project_id: str,
    file: UploadFile = File(...),
    sheet_name: str = "",
    c: Container = Depends(container),
):
    """Upload an Excel/CSV file; it becomes the datasource automatically."""
    project = _load(c, project_id)
    name = fm.sanitize_name(file.filename or "data.xlsx", fallback="data.xlsx")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in ("xlsx", "xls", "csv"):
        raise HTTPException(400, f"Unsupported datasource file type '.{suffix}'.")

    paths = c.repository.paths_for(project)
    target = paths.configuration_dir / name
    fm.save_bytes(target, await file.read())

    cfg = DatasourceConfig(
        type=DatasourceType.CSV if suffix == "csv" else DatasourceType.EXCEL,
        name=name, excel_path=str(target), sheet_name=sheet_name,
    )
    result = c.datasource_service.test_connection(project, cfg)
    return ConnectionTestResponse(
        ok=result.ok, message=result.message, details=result.details
    )


@router.get("/{project_id}/datasource", response_model=dict)
def get_datasource(project_id: str, c: Container = Depends(container)):
    cfg = c.datasource_service.load(_load(c, project_id))
    return {
        "type": str(cfg.type), "is_configured": cfg.is_configured,
        "server": cfg.server, "database": cfg.database,
        "file": cfg.excel_path, "sheet_name": cfg.sheet_name,
        "last_test_ok": cfg.last_test_ok, "last_test_message": cfg.last_test_message,
    }
