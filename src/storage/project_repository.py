"""Project repository — the on-disk home of every project.

Owns the canonical project folder layout and all persistence of the project
manifest, datasource config, LLM settings, extracted metadata, analysis context
and reports. Higher layers never touch the filesystem directly; they call these
methods.

On-disk layout for one project::

    <projects_root>/<safe-name>__<id>/
        project.json
        Dashboard/                 raw uploaded dashboard files
        Screenshots/               raw uploaded screenshots
        Metadata/                  dashboard_metadata.json, analysis_context.json
        Reports/                   <report-id>.json
        Logs/                      analysis.log
        Settings/                  llm_settings.json
        Configuration/             datasource.json
        Generated Test Cases/      exported test-case files
"""

from __future__ import annotations

from pathlib import Path

from src.core.constants import (
    AI_REASONING_FILE,
    ANALYSIS_CONTEXT_FILE,
    DASHBOARD_EXTRACTION_FILE,
    DATA_VALIDATION_FILE,
    DATASOURCE_FILE,
    DB_SCHEMA_FILE,
    LLM_SETTINGS_FILE,
    METADATA_FILE,
    PROJECT_FILE,
    TEST_CASES_FILE,
    VALIDATION_PLAN_FILE,
    VISUAL_ANALYSIS_FILE,
    ProjectFolder,
)
from src.core.exceptions import ProjectNotFoundError, StorageError
from src.core.logger import get_logger
from src.domain.models import (
    AIReasoning,
    AnalysisContext,
    AnalysisReport,
    DashboardExtraction,
    DashboardMetadata,
    DataValidationRun,
    DatasourceConfig,
    DbSchema,
    LLMSettings,
    Project,
    TestCase,
    ValidationPlan,
    VisualAnalysis,
)
from src.storage import file_manager as fm

_logger = get_logger()


class ProjectPaths:
    """Resolves all paths for a single project from its root folder."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # asset folders
    @property
    def dashboard_dir(self) -> Path:
        return self.root / ProjectFolder.DASHBOARD.value

    @property
    def screenshots_dir(self) -> Path:
        return self.root / ProjectFolder.SCREENSHOTS.value

    @property
    def metadata_dir(self) -> Path:
        return self.root / ProjectFolder.METADATA.value

    @property
    def reports_dir(self) -> Path:
        return self.root / ProjectFolder.REPORTS.value

    @property
    def logs_dir(self) -> Path:
        return self.root / ProjectFolder.LOGS.value

    @property
    def settings_dir(self) -> Path:
        return self.root / ProjectFolder.SETTINGS.value

    @property
    def configuration_dir(self) -> Path:
        return self.root / ProjectFolder.CONFIGURATION.value

    @property
    def test_cases_dir(self) -> Path:
        return self.root / ProjectFolder.TEST_CASES.value

    # canonical files
    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def datasource_file(self) -> Path:
        return self.configuration_dir / DATASOURCE_FILE

    @property
    def llm_settings_file(self) -> Path:
        return self.settings_dir / LLM_SETTINGS_FILE

    @property
    def metadata_file(self) -> Path:
        return self.metadata_dir / METADATA_FILE

    @property
    def analysis_context_file(self) -> Path:
        return self.metadata_dir / ANALYSIS_CONTEXT_FILE

    @property
    def visual_analysis_file(self) -> Path:
        return self.metadata_dir / VISUAL_ANALYSIS_FILE

    @property
    def ai_reasoning_file(self) -> Path:
        return self.reports_dir / AI_REASONING_FILE

    @property
    def test_cases_file(self) -> Path:
        return self.test_cases_dir / TEST_CASES_FILE

    @property
    def dashboard_extraction_file(self) -> Path:
        return self.metadata_dir / DASHBOARD_EXTRACTION_FILE

    @property
    def validation_plan_file(self) -> Path:
        return self.metadata_dir / VALIDATION_PLAN_FILE

    @property
    def data_validation_file(self) -> Path:
        return self.reports_dir / DATA_VALIDATION_FILE

    @property
    def db_schema_file(self) -> Path:
        return self.configuration_dir / DB_SCHEMA_FILE

    def all_folders(self) -> list[Path]:
        return [
            self.dashboard_dir,
            self.screenshots_dir,
            self.metadata_dir,
            self.reports_dir,
            self.logs_dir,
            self.settings_dir,
            self.configuration_dir,
            self.test_cases_dir,
        ]


class ProjectRepository:
    """CRUD + asset persistence for projects, rooted at ``projects_root``."""

    def __init__(self, projects_root: Path):
        self.projects_root = Path(projects_root)
        fm.ensure_dir(self.projects_root)

    # --- folder naming ----------------------------------------------------
    def _folder_name(self, project: Project) -> str:
        safe = fm.sanitize_name(project.name, fallback="project")
        return f"{safe}__{project.id}"

    def paths_for(self, project: Project) -> ProjectPaths:
        return ProjectPaths(self.projects_root / self._folder_name(project))

    def _find_root_by_id(self, project_id: str) -> Path | None:
        for child in self.projects_root.iterdir():
            if child.is_dir() and child.name.endswith(f"__{project_id}"):
                return child
        return None

    # --- create -----------------------------------------------------------
    def create(self, project: Project) -> ProjectPaths:
        """Create the project folder structure and persist the manifest."""
        paths = self.paths_for(project)
        if paths.project_file.exists():
            raise StorageError(f"Project already exists at {paths.root}")
        fm.ensure_dir(paths.root)
        for folder in paths.all_folders():
            fm.ensure_dir(folder)
        self.save(project)
        _logger.info("Created project '%s' (%s) at %s", project.name, project.id, paths.root)
        return paths

    # --- save / load ------------------------------------------------------
    def save(self, project: Project) -> None:
        project.touch()
        paths = self.paths_for(project)
        fm.ensure_dir(paths.root)
        fm.write_json(paths.project_file, project.to_dict())

    def load(self, project_id: str) -> Project:
        root = self._find_root_by_id(project_id)
        if root is None:
            raise ProjectNotFoundError(f"No project with id {project_id}")
        data = fm.read_json(ProjectPaths(root).project_file)
        return Project.from_dict(data)

    def list_projects(self) -> list[Project]:
        """Return all projects, newest-updated first."""
        projects: list[Project] = []
        if not self.projects_root.exists():
            return projects
        for child in sorted(self.projects_root.iterdir()):
            if not child.is_dir():
                continue
            manifest = ProjectPaths(child).project_file
            if not manifest.exists():
                continue
            try:
                projects.append(Project.from_dict(fm.read_json(manifest)))
            except StorageError as exc:
                _logger.warning("Skipping unreadable project at %s: %s", child, exc)
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def delete(self, project_id: str) -> None:
        root = self._find_root_by_id(project_id)
        if root is None:
            raise ProjectNotFoundError(f"No project with id {project_id}")
        fm.delete_path(root)
        _logger.info("Deleted project %s at %s", project_id, root)

    # --- datasource config ------------------------------------------------
    def save_datasource(self, project: Project, cfg: DatasourceConfig) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.configuration_dir)
        fm.write_json(paths.datasource_file, cfg.to_dict())

    def load_datasource(self, project: Project) -> DatasourceConfig | None:
        paths = self.paths_for(project)
        if not paths.datasource_file.exists():
            return None
        return DatasourceConfig.from_dict(fm.read_json(paths.datasource_file))

    # --- LLM settings -----------------------------------------------------
    def save_llm_settings(self, project: Project, settings: LLMSettings) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.settings_dir)
        fm.write_json(paths.llm_settings_file, settings.to_dict())

    def load_llm_settings(self, project: Project) -> LLMSettings | None:
        paths = self.paths_for(project)
        if not paths.llm_settings_file.exists():
            return None
        return LLMSettings.from_dict(fm.read_json(paths.llm_settings_file))

    # --- metadata & analysis context --------------------------------------
    def save_metadata(self, project: Project, metadata: DashboardMetadata) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.metadata_dir)
        fm.write_json(paths.metadata_file, metadata.to_dict())

    def load_metadata(self, project: Project) -> DashboardMetadata | None:
        paths = self.paths_for(project)
        if not paths.metadata_file.exists():
            return None
        return DashboardMetadata.from_dict(fm.read_json(paths.metadata_file))

    def save_visual_analysis(self, project: Project, visual: VisualAnalysis) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.metadata_dir)
        fm.write_json(paths.visual_analysis_file, visual.to_dict())

    def load_visual_analysis(self, project: Project) -> VisualAnalysis | None:
        paths = self.paths_for(project)
        if not paths.visual_analysis_file.exists():
            return None
        return VisualAnalysis.from_dict(fm.read_json(paths.visual_analysis_file))

    def save_ai_reasoning(self, project: Project, reasoning: AIReasoning) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.reports_dir)
        fm.write_json(paths.ai_reasoning_file, reasoning.to_dict())

    def load_ai_reasoning(self, project: Project) -> AIReasoning | None:
        paths = self.paths_for(project)
        if not paths.ai_reasoning_file.exists():
            return None
        return AIReasoning.from_dict(fm.read_json(paths.ai_reasoning_file))

    def save_test_cases(self, project: Project, test_cases: list[TestCase]) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.test_cases_dir)
        fm.write_json(
            paths.test_cases_file, {"test_cases": [tc.to_dict() for tc in test_cases]}
        )

    def load_test_cases(self, project: Project) -> list[TestCase]:
        paths = self.paths_for(project)
        if not paths.test_cases_file.exists():
            return []
        data = fm.read_json(paths.test_cases_file)
        return [TestCase.from_dict(tc) for tc in data.get("test_cases", [])]

    # --- datasource schema (redesign) -------------------------------------
    def save_db_schema(self, project: Project, schema: DbSchema) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.configuration_dir)
        fm.write_json(paths.db_schema_file, schema.to_dict())

    def load_db_schema(self, project: Project) -> DbSchema | None:
        paths = self.paths_for(project)
        if not paths.db_schema_file.exists():
            return None
        return DbSchema.from_dict(fm.read_json(paths.db_schema_file))

    # --- dashboard understanding & data validation (redesign) -------------
    def save_dashboard_extraction(self, project: Project, extraction: DashboardExtraction) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.metadata_dir)
        fm.write_json(paths.dashboard_extraction_file, extraction.to_dict())

    def load_dashboard_extraction(self, project: Project) -> DashboardExtraction | None:
        paths = self.paths_for(project)
        if not paths.dashboard_extraction_file.exists():
            return None
        return DashboardExtraction.from_dict(fm.read_json(paths.dashboard_extraction_file))

    def save_validation_plan(self, project: Project, plan: ValidationPlan) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.metadata_dir)
        fm.write_json(paths.validation_plan_file, plan.to_dict())

    def load_validation_plan(self, project: Project) -> ValidationPlan | None:
        paths = self.paths_for(project)
        if not paths.validation_plan_file.exists():
            return None
        return ValidationPlan.from_dict(fm.read_json(paths.validation_plan_file))

    def save_data_validation(self, project: Project, run: DataValidationRun) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.reports_dir)
        fm.write_json(paths.data_validation_file, run.to_dict())

    def load_data_validation(self, project: Project) -> DataValidationRun | None:
        paths = self.paths_for(project)
        if not paths.data_validation_file.exists():
            return None
        return DataValidationRun.from_dict(fm.read_json(paths.data_validation_file))

    def save_analysis_context(self, project: Project, context: AnalysisContext) -> None:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.metadata_dir)
        fm.write_json(paths.analysis_context_file, context.to_dict())

    def load_analysis_context(self, project: Project) -> AnalysisContext | None:
        paths = self.paths_for(project)
        if not paths.analysis_context_file.exists():
            return None
        return AnalysisContext.from_dict(fm.read_json(paths.analysis_context_file))

    # --- run summary ------------------------------------------------------
    #: One small file per project, read by the dashboard instead of the full
    #: validation and test-case artifacts.
    RUN_SUMMARY_FILE = "summary.json"

    def save_run_summary(self, project: Project, summary) -> Path:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.reports_dir)
        target = paths.reports_dir / self.RUN_SUMMARY_FILE
        fm.write_json(target, summary.to_dict())
        return target

    def load_run_summary(self, project: Project):
        from src.domain.models import RunSummary

        target = self.paths_for(project).reports_dir / self.RUN_SUMMARY_FILE
        if not target.exists():
            return None
        try:
            return RunSummary.from_dict(fm.read_json(target))
        except Exception:  # noqa: BLE001 - a bad summary must not break the list
            _logger.warning("Unreadable run summary for %s", project.id)
            return None

    def list_run_summaries(self) -> list:
        """Every project's last run, newest first. Projects never analysed are
        skipped rather than reported as zeroed runs."""
        out = []
        for project in self.list_projects():
            summary = self.load_run_summary(project)
            if summary is not None:
                out.append(summary)
        return sorted(out, key=lambda s: s.generated_at, reverse=True)

    # --- reports ----------------------------------------------------------
    def save_report(self, project: Project, report: AnalysisReport) -> Path:
        paths = self.paths_for(project)
        fm.ensure_dir(paths.reports_dir)
        target = paths.reports_dir / f"{report.id}.json"
        fm.write_json(target, report.to_dict())
        return target

    def has_report(self, project: Project) -> bool:
        """Whether any report exists, without reading one.

        The list page asks this for every project. Answering it through
        list_reports() parses every stored report to produce a boolean — 2.3
        seconds across 32 projects, against 2 milliseconds for the directory
        check, and it grows with report size rather than report count.
        """
        reports = self.paths_for(project).reports_dir
        return any(reports.glob("RPT-*.json"))

    def list_reports(self, project: Project) -> list[AnalysisReport]:
        paths = self.paths_for(project)
        reports: list[AnalysisReport] = []
        # Report files are named "<report-id>.json" (id prefixed "RPT-"); this
        # prefix filter keeps sibling artifacts (e.g. ai_reasoning.json) out.
        for f in fm.list_dir(paths.reports_dir, extensions=(".json",)):
            if not f.name.startswith("RPT-"):
                continue
            try:
                reports.append(AnalysisReport.from_dict(fm.read_json(f)))
            except StorageError as exc:
                _logger.warning("Skipping unreadable report %s: %s", f, exc)
        reports.sort(key=lambda r: r.created_at, reverse=True)
        return reports
