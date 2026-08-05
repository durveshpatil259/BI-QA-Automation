"""Application context and Streamlit session-state helpers.

:class:`AppContext` is the composition root for the UI: it wires the global
config to the storage repository and (later) the service objects, so pages get
their dependencies from one place instead of constructing them ad hoc.

It is cached in ``st.session_state`` so a single instance survives Streamlit's
re-run-on-every-interaction model.
"""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from src.core.config import AppConfig, load_config
from src.core.constants import APP_NAME
from src.services.analysis_service import AnalysisService
from src.services.datasource_service import DatasourceService
from src.services.llm_service import LLMService
from src.services.metadata_service import MetadataService
from src.services.project_service import ProjectService
from src.services.reporting import ReportService
from src.services.schema_service import SchemaService
from src.services.screenshot_service import ScreenshotService
from src.services.sql_validation_engine import SqlValidationEngine
from src.services.test_case_service import TestCaseService
from src.services.test_expansion_service import TestExpansionService
from src.services.upload_service import UploadService
from src.services.validation_plan_service import ValidationPlanService
from src.services.vision_service import VisionService
from src.storage.project_repository import ProjectRepository

# session-state keys (centralised to avoid typos across pages)
KEY_CONTEXT = "_app_context"
KEY_ACTIVE_PROJECT_ID = "active_project_id"
KEY_NAV = "nav_selection"


@dataclass
class AppContext:
    """Holds shared, long-lived dependencies for the UI."""

    config: AppConfig
    projects: ProjectRepository
    project_service: ProjectService
    upload_service: UploadService
    datasource_service: DatasourceService
    metadata_service: MetadataService
    screenshot_service: ScreenshotService
    analysis_service: AnalysisService
    llm_service: LLMService
    test_case_service: TestCaseService
    report_service: ReportService
    schema_service: SchemaService
    vision_service: VisionService
    validation_plan_service: ValidationPlanService
    sql_validation_engine: SqlValidationEngine
    test_expansion_service: TestExpansionService

    @classmethod
    def create(cls) -> "AppContext":
        config = load_config()
        config.ensure_projects_root()
        repo = ProjectRepository(config.projects_root_path)
        return cls(
            config=config,
            projects=repo,
            project_service=ProjectService(repo),
            upload_service=UploadService(repo),
            datasource_service=DatasourceService(repo),
            metadata_service=MetadataService(repo),
            screenshot_service=ScreenshotService(repo),
            analysis_service=AnalysisService(repo),
            llm_service=LLMService(repo),
            test_case_service=TestCaseService(repo),
            report_service=ReportService(repo),
            schema_service=SchemaService(repo),
            vision_service=VisionService(repo),
            validation_plan_service=ValidationPlanService(repo),
            sql_validation_engine=SqlValidationEngine(repo),
            test_expansion_service=TestExpansionService(repo),
        )


def get_context() -> AppContext:
    """Return the cached :class:`AppContext`, creating it once per session."""
    if KEY_CONTEXT not in st.session_state:
        st.session_state[KEY_CONTEXT] = AppContext.create()
    return st.session_state[KEY_CONTEXT]


def set_active_project(project_id: str | None) -> None:
    st.session_state[KEY_ACTIVE_PROJECT_ID] = project_id


def get_active_project_id() -> str | None:
    return st.session_state.get(KEY_ACTIVE_PROJECT_ID)


def get_active_project():
    """Load the currently-active project, or None if none selected/found."""
    pid = get_active_project_id()
    if not pid:
        return None
    ctx = get_context()
    try:
        return ctx.projects.load(pid)
    except Exception:  # noqa: BLE001 - stale id after deletion, etc.
        set_active_project(None)
        return None


def page_title(title: str) -> str:
    return f"{title} · {APP_NAME}"
