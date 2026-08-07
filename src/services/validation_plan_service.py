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
    def _collect_targets(self, project: Project) -> list[tuple[str, str, str]]:
        """Return [(kpi_name, displayed_value, dax)] for the AI to map.

        Screenshot KPIs supply the displayed value; the model's measures supply
        the DAX formula. When both exist they are merged per KPI so the AI knows
        *what the number is* and *how it is calculated*.
        """
        metadata = self._repo.load_metadata(project)
        dax_by_name: dict[str, str] = {}
        if metadata:
            for m in metadata.all_measures:
                if m.name:
                    dax_by_name.setdefault(m.name.casefold(), m.dax_expression or "")

        targets: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        extraction = self._repo.load_dashboard_extraction(project)
        if extraction:
            for k in extraction.kpis:
                if k.name and k.name.casefold() not in seen:
                    targets.append((k.name, k.raw_value, dax_by_name.get(k.name.casefold(), "")))
                    seen.add(k.name.casefold())

        if not targets and metadata:
            for m in metadata.all_measures:
                if m.name and m.name.casefold() not in seen:
                    targets.append((m.name, "", m.dax_expression or ""))
                    seen.add(m.name.casefold())
        return targets

    # Visuals that carry no validatable dimension of their own.
    _SKIP_VISUALS = {
        "slicer", "card", "kpi_card", "image", "textbox", "shape",
        "actionbutton", "multirowcard",
    }

    def _visuals_from_metadata(self, metadata) -> list[dict]:
        """Build chart descriptors from the PBIX/PBIT report layout.

        The report layout binds each visual to fields like
        ``product_data.Category`` (a dimension) and ``Sales_data.Total Sales``
        (a measure). That is enough to generate a GROUP BY query per chart even
        when no screenshot exists — the values cannot be compared, but the
        query's correctness and the category set still can.
        """
        if not metadata:
            return []
        measure_names = {m.name.casefold() for m in metadata.all_measures if m.name}

        out: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for visual in metadata.all_visuals:
            vtype = (visual.visual_type or "").strip()
            if vtype.casefold() in self._SKIP_VISUALS or not visual.fields:
                continue

            dimension, measure = "", ""
            for ref in visual.fields:
                field = ref.rsplit(".", 1)[-1].strip()
                if field.casefold() in measure_names:
                    measure = measure or ref
                elif not dimension:
                    dimension = ref
            # A visual with no dimension (e.g. a gauge on a single measure) is
            # already covered by that measure's scalar KPI test.
            if not dimension:
                continue

            key = (vtype.casefold(), dimension.casefold(), measure.casefold())
            if key in seen:
                continue          # same chart repeated across pages
            seen.add(key)

            out.append({
                "title": visual.title or f"{vtype} by {dimension.rsplit('.', 1)[-1]}",
                "visual_type": vtype,
                "dimension_field": dimension,
                "measure_field": measure,
                "values_visible": False,   # no screenshot -> no readable numbers
                "categories": [],
                "points": [],
                "page": visual.page,
            })
        return out

    def _describe_visuals(self, visuals) -> list[dict]:
        """Summarise charts/tables for the AI prompt, skipping non-data visuals."""
        out: list[dict] = []
        for v in visuals or []:
            vtype = (v.visual_type or "").strip().casefold()
            if vtype in self._SKIP_VISUALS:
                continue
            # Need something to group by, otherwise there is nothing to compare.
            dimension = v.dimension_field or ""
            categories = [p.dimension for p in v.data_points if p.dimension]
            if not dimension and not categories:
                continue
            out.append({
                "title": v.title or v.visual_type or "visual",
                "visual_type": v.visual_type,
                "dimension_field": dimension,
                "measure_field": v.measure_field,
                "values_visible": bool(v.values_visible),
                "categories": categories,
                # Kept out of the prompt text; used by the engine to compare.
                "points": list(v.data_points),
            })
        return out

    def _build_scenarios(self, project: Project) -> list[dict]:
        """One scenario per screenshot view (each has its own slicer selection).

        Falls back to a single unfiltered scenario built from model measures when
        no screenshots were analysed.
        """
        from src.services.validation.dax_analyzer import describe_format

        metadata = self._repo.load_metadata(project)
        dax_by_name: dict[str, str] = {}
        fmt_by_name: dict[str, str] = {}
        if metadata:
            for m in metadata.all_measures:
                if m.name:
                    dax_by_name.setdefault(m.name.casefold(), m.dax_expression or "")
                    fmt_by_name.setdefault(
                        m.name.casefold(), describe_format(m.format_string)
                    )

        extraction = self._repo.load_dashboard_extraction(project)
        scenarios: list[dict] = []

        if extraction and extraction.views:
            for i, view in enumerate(extraction.views, start=1):
                kpis = [
                    (k.name, k.raw_value, dax_by_name.get(k.name.casefold(), ""))
                    for k in view.kpis if k.name
                ]
                visuals = self._describe_visuals(view.visuals)
                if not kpis and not visuals:
                    continue
                scenarios.append({
                    "id": f"S{i}",
                    "label": view.scenario_label(),
                    "view_name": view.name,
                    "filters": [(f.name, f.selected) for f in view.active_filters()],
                    "kpis": kpis,
                    "visuals": visuals,
                })

        if not scenarios and extraction and extraction.kpis:
            # Older extraction without per-view data.
            scenarios.append({
                "id": "S1",
                "label": ", ".join(
                    f"{f.name}={f.selected}" for f in extraction.active_filters()
                ) or "No filters",
                "view_name": "",
                "filters": [(f.name, f.selected) for f in extraction.active_filters()],
                "kpis": [
                    (k.name, k.raw_value, dax_by_name.get(k.name.casefold(), ""))
                    for k in extraction.kpis if k.name
                ],
            })

        if not scenarios and metadata:
            # PBIX/PBIT-only mode: no screenshot, so there is no displayed value.
            # The measure's DAX and its Power BI format string drive the SQL.
            measures = [
                (m.name, "", m.dax_expression or "",
                 describe_format(m.format_string))
                for m in metadata.all_measures if m.name
            ]
            # Charts come from the report layout's field bindings, so every
            # visual is validated even with no screenshot uploaded.
            layout_visuals = self._visuals_from_metadata(metadata)
            if measures or layout_visuals:
                scenarios.append({
                    "id": "S1", "label": "Model measures (no filters)",
                    "view_name": "", "filters": [], "kpis": measures,
                    "visuals": layout_visuals,
                })
        else:
            # Attach format guidance to screenshot-derived KPIs too.
            layout_visuals = self._visuals_from_metadata(metadata)
            for sc in scenarios:
                sc["kpis"] = [
                    (k[0], k[1], k[2], fmt_by_name.get(k[0].casefold(), ""))
                    for k in sc["kpis"]
                ]
                # Add layout-only charts the screenshot did not capture (e.g.
                # visuals on other pages), matching on title.
                have = {str(v.get("title", "")).casefold() for v in sc.get("visuals", [])}
                sc.setdefault("visuals", []).extend(
                    v for v in layout_visuals
                    if str(v.get("title", "")).casefold() not in have
                )
        return scenarios

    # --- generation -------------------------------------------------------
    def generate(
        self,
        project: Project,
        settings: LLMSettings | None = None,
        *,
        client: LLMClient | None = None,
    ) -> ValidationPlan:
        settings = settings or (self._repo.load_llm_settings(project) or LLMSettings())

        scenarios = self._build_scenarios(project)
        if not scenarios:
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
            build_plan_user_prompt(scenarios, schema.compact_text(), dialect),
        )

        items = self._parse(response.content, scenarios)
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
    def _parse(content: str, scenarios: list[dict]) -> list[ValidationPlanItem]:
        data = extract_json(content)
        if isinstance(data, dict):
            data = data.get("validation_plan", data.get("plan", data.get("items")))
        if not isinstance(data, list):
            raise LLMResponseError(
                "The LLM did not return a JSON validation plan (array of items)."
            )

        # (scenario_id, kpi_name) -> displayed value, so each item is linked to
        # the value shown in ITS scenario.
        by_scenario = {sc["id"]: sc for sc in scenarios}
        value_lookup: dict[tuple[str, str], str] = {}
        visual_points: dict[tuple[str, str], list] = {}
        for sc in scenarios:
            for kpi in sc.get("kpis", []):
                value_lookup[(sc["id"], kpi[0].casefold())] = kpi[1]
            for vis in sc.get("visuals", []):
                visual_points[(sc["id"], str(vis.get("title", "")).casefold())] = \
                    vis.get("points", [])
        default_sid = scenarios[0]["id"] if scenarios else ""

        items: list[ValidationPlanItem] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("kpi_name", raw.get("kpi", ""))).strip()
            sql = str(raw.get("generated_sql", raw.get("sql", ""))).strip()
            sid = str(raw.get("scenario", "")).strip() or default_sid
            sc = by_scenario.get(sid, {})
            filters = raw.get("filters", []) or []
            try:
                confidence = float(raw.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            item_type = str(raw.get("item_type", "scalar")).strip().lower()
            if item_type not in ("scalar", "grouped", "structural"):
                item_type = "scalar"
            item = ValidationPlanItem(
                kpi_name=name,
                scenario=sc.get("label", ""),
                view_name=sc.get("view_name", ""),
                dashboard_value=value_lookup.get((sid, name.casefold()), ""),
                table=str(raw.get("table", "")).strip(),
                column=str(raw.get("column", "")).strip(),
                aggregation=str(raw.get("aggregation", "")).strip(),
                business_meaning=str(raw.get("business_meaning", "")).strip(),
                filters=[str(f).strip() for f in filters if str(f).strip()],
                generated_sql=sql,
                confidence=confidence,
                item_type=item_type,
                dimension_column=str(raw.get("dimension_column", "")).strip(),
            )
            # For chart items, attach what the dashboard actually showed so the
            # engine can compare per-category (or as a category set).
            if item_type in ("grouped", "structural"):
                item.visual_title = name
                item.expected_points = visual_points.get((sid, name.casefold()), [])
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
