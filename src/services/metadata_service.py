"""Metadata extraction service.

Selects the project's dashboard file, runs the platform-appropriate extractor,
persists the resulting :class:`DashboardMetadata` to
``Metadata/dashboard_metadata.json``, and exposes load access for the UI and the
downstream comparison/validation engines.
"""

from __future__ import annotations

from pathlib import Path

from src.core.exceptions import MetadataExtractionError
from src.core.logger import get_logger
from src.domain.models import DashboardMetadata, Project
from src.services.extractors import create_extractor
from src.storage.project_repository import ProjectRepository

_logger = get_logger()


class MetadataService:
    """Runs and persists dashboard metadata extraction for a project."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def has_dashboard_file(self, project: Project) -> bool:
        return bool(self._dashboard_files(project))

    def _dashboard_files(self, project: Project) -> list[Path]:
        paths = self._repo.paths_for(project)
        from src.storage import file_manager as fm

        return fm.list_dir(paths.dashboard_dir)

    def _select_primary_file(self, project: Project) -> Path:
        files = self._dashboard_files(project)
        if not files:
            raise MetadataExtractionError(
                "No dashboard file uploaded. Add one on the Upload page first."
            )
        # Prefer formats with a *readable* model. .pbit/.pbip/.pbir/.zip store the
        # model as text (DataModelSchema/TMDL); a native .pbix has a binary model
        # and yields no tables — so it is the last resort when several exist.
        priority = {".pbit": 0, ".pbip": 1, ".pbir": 1, ".zip": 2, ".pbix": 3}
        files.sort(key=lambda p: priority.get(p.suffix.lower(), 4))
        return files[0]

    def extract(self, project: Project) -> DashboardMetadata:
        """Extract metadata from the project's primary dashboard file and save."""
        source = self._select_primary_file(project)
        extractor = create_extractor(project.bi_platform)
        _logger.info(
            "Extracting metadata: project=%s platform=%s file=%s",
            project.id, project.bi_platform, source.name,
        )
        metadata = extractor.extract(source)
        self._repo.save_metadata(project, metadata)
        return metadata

    def load(self, project: Project) -> DashboardMetadata | None:
        return self._repo.load_metadata(project)
