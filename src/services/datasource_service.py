"""Datasource configuration service.

Application-layer use cases for a project's datasource: validate and persist the
configuration to ``Configuration/datasource.json``, load it, test the
connection, and enumerate datasets — always via the connector abstraction so
the LLM is never involved in any data access.
"""

from __future__ import annotations

import datetime as _dt

from src.core.constants import DatasourceType, SqlAuthMode
from src.core.exceptions import ValidationError
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DataQueryResult, Project
from src.services.datasources import ConnectionTestResult, create_connector
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class DatasourceService:
    """Configure, persist and test a project's datasource."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    # --- persistence ------------------------------------------------------
    def load(self, project: Project) -> DatasourceConfig:
        """Return the saved config, or a fresh default if none exists yet."""
        return self._repo.load_datasource(project) or DatasourceConfig()

    def save(self, project: Project, config: DatasourceConfig) -> DatasourceConfig:
        """Validate and persist a datasource configuration."""
        self._validate(config)
        config.is_configured = True
        self._repo.save_datasource(project, config)
        _logger.info("Saved datasource (%s) for project %s", config.type, project.id)
        return config

    # --- connection test --------------------------------------------------
    def test_connection(
        self, project: Project, config: DatasourceConfig
    ) -> ConnectionTestResult:
        """Test connectivity and record the outcome on the config."""
        try:
            self._validate(config)
        except ValidationError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))

        connector = create_connector(config)
        result = connector.test_connection()

        config.last_tested_at = _dt.datetime.now()
        config.last_test_ok = result.ok
        config.last_test_message = result.message
        # Persist the test outcome (and config) so the UI reflects last status.
        config.is_configured = True
        self._repo.save_datasource(project, config)
        return result

    def list_datasets(self, config: DatasourceConfig) -> list[str]:
        """Enumerate tables/views (SQL) or sheets (Excel)."""
        return create_connector(config).list_datasets()

    def excel_workbook_info(self, config: DatasourceConfig) -> list[dict]:
        """Return per-worksheet metadata (name/rows/cols) for an Excel workbook."""
        from src.services.datasources.excel import ExcelConnector

        return ExcelConnector(config).sheet_summaries()

    def preview(
        self, config: DatasourceConfig, dataset: str, sample_rows: int = 50
    ) -> DataQueryResult:
        """Preview a dataset (SQL table / Excel sheet) by name, read-only."""
        return create_connector(config).preview_dataset(dataset, sample_rows=sample_rows)

    # --- validation -------------------------------------------------------
    @staticmethod
    def _validate(config: DatasourceConfig) -> None:
        if config.type == DatasourceType.SQL_SERVER:
            if not config.server.strip():
                raise ValidationError("Server is required for SQL Server.")
            if not config.database.strip():
                raise ValidationError("Database is required for SQL Server.")
            if config.auth_mode == SqlAuthMode.SQL_LOGIN and not config.username.strip():
                raise ValidationError("Username is required for SQL Login.")
        elif config.type in (DatasourceType.EXCEL, DatasourceType.CSV):
            if not config.excel_path.strip():
                raise ValidationError(f"{config.type} file path is required.")
        else:  # pragma: no cover - guarded by enum
            raise ValidationError(f"Unsupported datasource type: {config.type}")
