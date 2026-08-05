"""Datasource schema service (redesign V2).

Reads the datasource schema deterministically (tables, columns, primary keys,
foreign keys) via the read-only connector, persists it, and exposes it for the
AI semantic-mapping stage (V4). No AI is involved here.
"""

from __future__ import annotations

from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DbSchema, Project
from src.services.datasources import create_connector
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class SchemaService:
    """Reads and persists the datasource schema for a project."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def read_schema(self, project: Project, config: DatasourceConfig) -> DbSchema:
        """Introspect the datasource and persist the resulting schema."""
        connector = create_connector(config)
        schema = connector.get_schema()
        self._repo.save_db_schema(project, schema)
        _logger.info(
            "Read schema for project %s: %s", project.id, schema.summary_counts()
        )
        return schema

    def load_schema(self, project: Project) -> DbSchema | None:
        return self._repo.load_db_schema(project)
