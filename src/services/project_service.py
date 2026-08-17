"""Project management service.

Application-layer use cases for projects: create, list, open, rename/update and
delete. It wraps :class:`ProjectRepository` with business rules (name required,
name uniqueness, platform validation) so the UI never applies rules ad hoc and
never touches storage directly.
"""

from __future__ import annotations

from src.core.constants import BIPlatform
from src.core.exceptions import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    ValidationError,
)
from src.core.logger import get_logger
from src.domain.models import Project
from src.storage.project_repository import ProjectPaths, ProjectRepository

_logger = get_logger()

_MAX_NAME_LEN = 100
_MAX_DESC_LEN = 1000


class ProjectService:
    """Business logic for the project lifecycle."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    # --- queries ----------------------------------------------------------
    def list_projects(self) -> list[Project]:
        return self._repo.list_projects()

    def get_project(self, project_id: str) -> Project:
        return self._repo.load(project_id)

    def paths_for(self, project: Project) -> ProjectPaths:
        return self._repo.paths_for(project)

    def name_exists(self, name: str, exclude_id: str | None = None) -> bool:
        target = (name or "").strip().casefold()
        for p in self._repo.list_projects():
            if p.id == exclude_id:
                continue
            if p.name.strip().casefold() == target:
                return True
        return False

    # --- commands ---------------------------------------------------------
    def create_project(
        self,
        name: str,
        bi_platform: BIPlatform | str,
        description: str = "",
        environment: str = "",
    ) -> Project:
        """Validate inputs and create a new project on disk."""
        name = self._validate_name(name)
        description = self._validate_description(description)
        platform = self._coerce_platform(bi_platform)

        if self.name_exists(name):
            raise ProjectAlreadyExistsError(
                f"A project named '{name}' already exists. Choose a different name."
            )

        project = Project(name=name, description=description, bi_platform=platform,
                          environment=(environment or '').strip())
        self._repo.create(project)
        _logger.info("Project created: %s (%s)", project.name, project.id)
        return project

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        bi_platform: BIPlatform | str | None = None,
    ) -> Project:
        """Update mutable project fields with validation, then persist."""
        project = self._repo.load(project_id)

        if name is not None:
            new_name = self._validate_name(name)
            if new_name.casefold() != project.name.casefold() and self.name_exists(
                new_name, exclude_id=project_id
            ):
                raise ProjectAlreadyExistsError(
                    f"A project named '{new_name}' already exists."
                )
            project.name = new_name
        if description is not None:
            project.description = self._validate_description(description)
        if bi_platform is not None:
            project.bi_platform = self._coerce_platform(bi_platform)

        self._repo.save(project)
        _logger.info("Project updated: %s (%s)", project.name, project.id)
        return project

    def delete_project(self, project_id: str) -> None:
        self._repo.delete(project_id)

    # --- validation helpers ----------------------------------------------
    @staticmethod
    def _validate_name(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ValidationError("Project name is required.")
        if len(name) > _MAX_NAME_LEN:
            raise ValidationError(f"Project name must be <= {_MAX_NAME_LEN} characters.")
        return name

    @staticmethod
    def _validate_description(description: str) -> str:
        description = (description or "").strip()
        if len(description) > _MAX_DESC_LEN:
            raise ValidationError(
                f"Description must be <= {_MAX_DESC_LEN} characters."
            )
        return description

    @staticmethod
    def _coerce_platform(value: BIPlatform | str) -> BIPlatform:
        if isinstance(value, BIPlatform):
            return value
        try:
            return BIPlatform.from_value(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
