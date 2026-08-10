"""Dependency injection for the API.

Builds every service **once** at import time and hands them out via FastAPI
``Depends``. Deliberately independent of :mod:`src.ui.state` — that module
imports Streamlit, which must never be pulled into an HTTP server process.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from src.core.config import AppConfig, load_config
from src.pipeline.jobs import JobManager
from src.pipeline.runner import PipelineRunner, PipelineServices
from src.services.analysis_service import AnalysisService
from src.services.datasource_service import DatasourceService
from src.services.llm_service import LLMService
from src.services.metadata_service import MetadataService
from src.services.pbix_data_service import PbixDataService
from src.services.project_service import ProjectService
from src.services.reporting import ReportService
from src.services.schema_service import SchemaService
from src.services.sql_validation_engine import SqlValidationEngine
from src.services.test_expansion_service import TestExpansionService
from src.services.upload_service import UploadService
from src.services.validation_plan_service import ValidationPlanService
from src.storage.project_repository import ProjectRepository


@dataclass
class Container:
    """All long-lived application dependencies."""

    config: AppConfig
    repository: ProjectRepository
    project_service: ProjectService
    upload_service: UploadService
    datasource_service: DatasourceService
    metadata_service: MetadataService
    schema_service: SchemaService
    pbix_data_service: PbixDataService
    analysis_service: AnalysisService
    llm_service: LLMService
    validation_plan_service: ValidationPlanService
    sql_validation_engine: SqlValidationEngine
    test_expansion_service: TestExpansionService
    report_service: ReportService
    runner: PipelineRunner
    jobs: JobManager


@lru_cache(maxsize=1)
def get_container() -> Container:
    config = load_config()
    config.ensure_projects_root()
    repo = ProjectRepository(config.projects_root_path)

    services = dict(
        datasource_service=DatasourceService(repo),
        metadata_service=MetadataService(repo),
        schema_service=SchemaService(repo),
        analysis_service=AnalysisService(repo),
        llm_service=LLMService(repo),
        validation_plan_service=ValidationPlanService(repo),
        sql_validation_engine=SqlValidationEngine(repo),
        test_expansion_service=TestExpansionService(repo),
        report_service=ReportService(repo),
    )
    pbix_data_service = PbixDataService(repo)
    runner = PipelineRunner(PipelineServices(
        dax_evaluation_service=pbix_data_service, **services
    ))

    return Container(
        config=config,
        repository=repo,
        project_service=ProjectService(repo),
        upload_service=UploadService(repo),
        pbix_data_service=pbix_data_service,
        runner=runner,
        jobs=JobManager(runner, max_concurrent=2),
        **services,
    )


# --- FastAPI dependency shims ---------------------------------------------
def container() -> Container:
    return get_container()


def jobs() -> JobManager:
    return get_container().jobs
