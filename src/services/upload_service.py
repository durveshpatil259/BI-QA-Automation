"""Upload service — dashboard files and screenshots.

Handles persisting uploaded assets into the correct project sub-folders,
validating file types (dashboard extensions are platform-specific; screenshots
use a fixed image set), listing/removing assets, and — critically — the
**automatic analysis-mode determination**:

* dashboard file(s) only            -> METADATA
* screenshot(s) only                -> VISUAL
* dashboard file(s) + screenshot(s) -> COMPLETE
* neither                           -> None (nothing to analyse yet)

Disk is the source of truth: the project manifest's file lists and analysis mode
are reconciled from what actually exists on disk after every change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.constants import (
    DASHBOARD_EXTENSIONS,
    SCREENSHOT_EXTENSIONS,
    AnalysisMode,
)
from src.core.exceptions import UploadError
from src.core.logger import get_logger
from src.domain.models import Project
from src.storage import file_manager as fm
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

# Soft per-file guard (Streamlit enforces its own upload cap too).
_MAX_FILE_BYTES = 512 * 1024 * 1024  # 512 MB


@dataclass
class SaveResult:
    """Outcome of attempting to save one uploaded file."""

    file_name: str
    ok: bool
    message: str = ""


@dataclass
class AssetInfo:
    """A stored asset on disk (for listing in the UI)."""

    name: str
    size_bytes: int

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


class UploadService:
    """Persists and manages uploaded dashboard files and screenshots."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    # --- allowed extensions ----------------------------------------------
    @staticmethod
    def allowed_dashboard_extensions(project: Project) -> tuple[str, ...]:
        return DASHBOARD_EXTENSIONS.get(project.bi_platform, ())

    @staticmethod
    def allowed_screenshot_extensions() -> tuple[str, ...]:
        return SCREENSHOT_EXTENSIONS

    # --- save -------------------------------------------------------------
    def save_dashboard_files(
        self, project: Project, files: list[tuple[str, bytes]]
    ) -> list[SaveResult]:
        allowed = self.allowed_dashboard_extensions(project)
        paths = self._repo.paths_for(project)
        results = self._save_all(files, paths.dashboard_dir, allowed)
        self._reconcile(project)
        return results

    def _save_all(
        self, files: list[tuple[str, bytes]], target_dir: Path, allowed: tuple[str, ...]
    ) -> list[SaveResult]:
        results: list[SaveResult] = []
        fm.ensure_dir(target_dir)
        allowed_lower = tuple(e.lower() for e in allowed)
        for raw_name, data in files:
            name = fm.sanitize_name(Path(raw_name).name, fallback="upload")
            suffix = Path(name).suffix.lower()
            if allowed_lower and suffix not in allowed_lower:
                results.append(SaveResult(
                    name, False,
                    f"Unsupported type '{suffix or '(none)'}'. "
                    f"Allowed: {', '.join(allowed)}",
                ))
                continue
            if len(data) > _MAX_FILE_BYTES:
                results.append(SaveResult(name, False, "File exceeds 512 MB limit."))
                continue
            try:
                fm.save_bytes(target_dir / name, data)
                results.append(SaveResult(name, True, "Saved."))
                _logger.info("Saved upload '%s' to %s", name, target_dir)
            except UploadError as exc:  # pragma: no cover - defensive
                results.append(SaveResult(name, False, str(exc)))
            except Exception as exc:  # noqa: BLE001 - surface storage errors
                results.append(SaveResult(name, False, str(exc)))
        return results

    # --- list -------------------------------------------------------------
    def list_dashboard_files(self, project: Project) -> list[AssetInfo]:
        paths = self._repo.paths_for(project)
        return [
            AssetInfo(p.name, p.stat().st_size)
            for p in fm.list_dir(paths.dashboard_dir)
        ]

    def list_screenshots(self, project: Project) -> list[AssetInfo]:
        paths = self._repo.paths_for(project)
        return [
            AssetInfo(p.name, p.stat().st_size)
            for p in fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS)
        ]

    # --- remove -----------------------------------------------------------
    def remove_dashboard_file(self, project: Project, file_name: str) -> None:
        paths = self._repo.paths_for(project)
        fm.delete_path(paths.dashboard_dir / Path(file_name).name)
        self._reconcile(project)

    def remove_screenshot(self, project: Project, file_name: str) -> None:
        paths = self._repo.paths_for(project)
        fm.delete_path(paths.screenshots_dir / Path(file_name).name)
        self._reconcile(project)

    # --- analysis mode ----------------------------------------------------
    @staticmethod
    def determine_analysis_mode(
        has_dashboard: bool, has_screenshots: bool
    ) -> AnalysisMode | None:
        if has_dashboard and has_screenshots:
            return AnalysisMode.COMPLETE
        if has_dashboard:
            return AnalysisMode.METADATA
        if has_screenshots:
            return AnalysisMode.VISUAL
        return None

    def _reconcile(self, project: Project) -> None:
        """Sync the project manifest's file lists and analysis mode from disk."""
        dashboards = [a.name for a in self.list_dashboard_files(project)]
        screenshots = [a.name for a in self.list_screenshots(project)]
        project.dashboard_files = dashboards
        project.screenshot_files = screenshots
        project.analysis_mode = self.determine_analysis_mode(
            bool(dashboards), bool(screenshots)
        )
        self._repo.save(project)
        _logger.info(
            "Reconciled assets for %s: %d dashboard(s), %d screenshot(s), mode=%s",
            project.id, len(dashboards), len(screenshots), project.analysis_mode,
        )
