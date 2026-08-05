"""Validation Plan service (redesign V4).

Feeds the extracted dashboard KPIs and the datasource schema to the AI, which
maps each KPI to a table/column/aggregation/filters and **generates the SQL**
that computes it. The result is a persisted :class:`ValidationPlan`.

The AI only maps and writes SQL — it never executes anything. Python validates
that every generated statement is read-only before it is stored for execution
(V5).
"""

from __future__ import annotations

from src.core.constants import DatasourceType
from src.core.exceptions import LLMResponseError, ValidationError
from src.core.logger import get_logger
from src.domain.models import (
    LLMSettings,
    Project,
    ValidationPlan,
    ValidationPlanItem,
)
from src.services.llm import create_client
from src.services.llm.base import LLMClient
from src.services.llm.json_utils import extract_json
from src.services.llm.prompt_builder import PLAN_SYSTEM_PROMPT, build_plan_user_prompt
from src.services.validation.sql_guard import is_read_only
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

_DIALECTS = {
    DatasourceType.SQL_SERVER: "Microsoft SQL Server (T-SQL)",
    DatasourceType.EXCEL: "ANSI SQL (Excel workbook — treat each sheet as a table)",
}


class ValidationPlanService:
    """Generates and persists the KPI→SQL validation plan."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    def load(self, project: Project) -> ValidationPlan | None:
        return self._repo.load_validation_plan(project)

    # --- inputs -----------------------------------------------------------
    def _collect_targets(self, project: Project) -> list[tuple[str, str]]:
        """Return [(kpi_name, displayed_value)] from vision extraction, falling
        back to model measures if no screenshot extraction exists."""
        targets: list[tuple[str, str]] = []
        seen: set[str] = set()

        extraction = self._repo.load_dashboard_extraction(project)
        if extraction:
            for k in extraction.kpis:
                if k.name and k.name.casefold() not in seen:
                    targets.append((k.name, k.raw_value))
                    seen.add(k.name.casefold())

        if not targets:
            metadata = self._repo.load_metadata(project)
            if metadata:
                for m in metadata.all_measures:
                    if m.name and m.name.casefold() not in seen:
                        targets.append((m.name, ""))
                        seen.add(m.name.casefold())
        return targets

    # --- generation -------------------------------------------------------
    def generate(
        self,
        project: Project,
        settings: LLMSettings | None = None,
        *,
        client: LLMClient | None = None,
    ) -> ValidationPlan:
        settings = settings or (self._repo.load_llm_settings(project) or LLMSettings())

        targets = self._collect_targets(project)
        if not targets:
            raise ValidationError(
                "No KPIs or measures to map. Run AI vision extraction (Step 2) or "
                "extract metadata (Step 1) first."
            )

        schema = self._repo.load_db_schema(project)
        if not schema or not schema.tables:
            raise ValidationError(
                "No datasource schema found. Read the database schema on the "
                "Datasource page first."
            )

        dialect = _DIALECTS.get(schema.datasource_type, "ANSI SQL")
        client = client or create_client(settings)
        response = client.complete(
            PLAN_SYSTEM_PROMPT.replace("{dialect}", dialect),
            build_plan_user_prompt(targets, schema.compact_text(), dialect),
        )

        items = self._parse(response.content, dict(targets))
        plan = ValidationPlan(
            items=items,
            provider=settings.provider,
            model=response.model or client.model,
            raw_response=response.content,
        )
        self._repo.save_validation_plan(project, plan)
        _logger.info("Validation plan for %s: %d item(s)", project.id, len(items))
        return plan

    # --- parsing ----------------------------------------------------------
    @staticmethod
    def _parse(content: str, target_values: dict[str, str]) -> list[ValidationPlanItem]:
        data = extract_json(content)
        if isinstance(data, dict):
            data = data.get("validation_plan", data.get("plan", data.get("items")))
        if not isinstance(data, list):
            raise LLMResponseError(
                "The LLM did not return a JSON validation plan (array of items)."
            )

        # Case-insensitive lookup of the displayed dashboard value per KPI.
        value_lookup = {k.casefold(): v for k, v in target_values.items()}
        items: list[ValidationPlanItem] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("kpi_name", raw.get("kpi", ""))).strip()
            sql = str(raw.get("generated_sql", raw.get("sql", ""))).strip()
            filters = raw.get("filters", []) or []
            try:
                confidence = float(raw.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            item = ValidationPlanItem(
                kpi_name=name,
                dashboard_value=value_lookup.get(name.casefold(), ""),
                table=str(raw.get("table", "")).strip(),
                column=str(raw.get("column", "")).strip(),
                aggregation=str(raw.get("aggregation", "")).strip(),
                business_meaning=str(raw.get("business_meaning", "")).strip(),
                filters=[str(f).strip() for f in filters if str(f).strip()],
                generated_sql=sql,
                confidence=confidence,
            )
            # Flag non-read-only SQL so execution (V5) can skip it safely.
            if sql and not is_read_only(sql):
                item.business_meaning = (
                    (item.business_meaning + " ").strip()
                    + "[WARNING: generated SQL is not a single read-only SELECT and "
                    "will be skipped at execution.]"
                )
                item.confidence = min(item.confidence, 0.1)
            items.append(item)

        if not items:
            raise LLMResponseError("The validation plan contained no usable items.")
        return items
