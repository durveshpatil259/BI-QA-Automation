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
from src.core import cancellation
from src.core.exceptions import (LLMResponseError, OperationCancelled,
                                 TokenBudgetExhausted, ValidationError)
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
from src.services.validation.filter_reach import filter_applies
from src.services.validation.sql_guard import is_read_only
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

#: Hidden date tables Power BI auto-creates per date column. Model-only.
_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")

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

    #: Upper bound on filter scenarios. 4 slicers x 4 values x 30 measures would
    #: be hundreds of LLM-generated queries; this keeps a run practical while
    #: still covering every value of the most important slicers.
    MAX_SCENARIOS = 10

    def _data_backed_scenarios(self, project, metadata, fmt_by_name) -> list[dict]:
        """Build scenarios with real expected values read from the PBIX data.

        Produces an unfiltered baseline plus one scenario per slicer value, so
        e.g. Fiscal Year FY2018/FY2019/FY2020/FY2021 each become their own set
        of validations with the exact number Power BI would render.
        """
        from src.services.pbix_data_service import PbixDataService

        data = PbixDataService(self._repo)
        try:
            baseline = data.evaluate(project, metadata)
        except Exception as exc:  # noqa: BLE001 - data optional
            _logger.info("Data-backed evaluation unavailable: %s", exc)
            return []
        if not baseline:
            return []

        from src.services.validation.dax_analyzer import (
            apply_format,
            describe_format,
            infer_format_string,
        )

        dax_of, raw_fmt_of = {}, {}
        for m in metadata.all_measures:
            if not m.name:
                continue
            key = m.name.casefold()
            dax_of[key] = m.dax_expression or ""
            # A native .pbix carries no formatString, so infer one — otherwise
            # every KPI is compared as a bare float.
            raw_fmt_of[key] = m.format_string or infer_format_string(
                m.name, m.dax_expression or ""
            )
            fmt_by_name.setdefault(key, describe_format(raw_fmt_of[key]))
            if not fmt_by_name.get(key):
                fmt_by_name[key] = describe_format(raw_fmt_of[key])

        def kpis_for(values: dict[str, str]) -> list[tuple]:
            """Render each computed number as the dashboard would display it."""
            out = []
            for name, value in values.items():
                key = name.casefold()
                try:
                    displayed = apply_format(float(value), raw_fmt_of.get(key, ""))
                except (TypeError, ValueError):
                    displayed = str(value)
                out.append((name, displayed, dax_of.get(key, ""), fmt_by_name.get(key, "")))
            return out

        visuals = self._visuals_from_metadata(metadata)
        scenarios: list[dict] = [{
            "id": "S1", "label": "All data (no slicer applied)",
            "view_name": "", "filters": [], "kpis": kpis_for(baseline),
            "visuals": visuals,
        }]

        # Slicers with the fewest values first — those are the meaningful
        # analytic dimensions (fiscal year, channel) rather than long lists.
        try:
            options = sorted(
                data.detect_filters(project, metadata), key=lambda o: len(o.values)
            )
        except Exception as exc:  # noqa: BLE001
            _logger.info("Slicer detection failed: %s", exc)
            options = []

        from src.core.config import load_config as _load_cfg

        max_scenarios = int(getattr(_load_cfg(), 'max_scenarios',
                                    self.MAX_SCENARIOS) or self.MAX_SCENARIOS)
        index = 2
        for option in options:
            for value in option.values:
                if index > max_scenarios:
                    break
                try:
                    values = data.evaluate(
                        project, metadata,
                        filter_spec=(option.table, option.column, value),
                    )
                except Exception:  # noqa: BLE001
                    continue
                if not values:
                    continue
                scenarios.append({
                    "id": f"S{index}",
                    "label": f"{option.column}={value}",
                    "view_name": "",
                    "filters": [(f"{option.table}[{option.column}]", value)],
                    "kpis": kpis_for(values),
                    # Charts are validated once, on the baseline scenario.
                    "visuals": [],
                })
                index += 1
            if index > max_scenarios:
                break

        _logger.info(
            "Built %d data-backed scenario(s) with real expected values", len(scenarios)
        )
        return scenarios

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
            # Not every chart plots a *named measure*: binding a raw column
            # ("Revenue") and letting Power BI aggregate it is just as common.
            # Matching only declared measures dropped the value field entirely,
            # leaving the AI to guess what the bars represent.
            if not measure:
                measure = next(
                    (ref for ref in visual.fields if ref != dimension), ""
                )
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
            # PBIX-only mode. Evaluate the measures against the data inside the
            # file — both unfiltered and under each slicer selection — so every
            # condition the dashboard can show becomes a validated scenario
            # with a REAL expected value, no screenshot required.
            scenarios.extend(self._data_backed_scenarios(project, metadata, fmt_by_name))

        if not scenarios and metadata:
            # Fall back to names + DAX + format only (no data available).
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
                # A 5th element records whether the scenario's filter can even
                # reach this KPI. Power BI propagates along relationship
                # direction only, so a Fiscal Year slicer never reaches a
                # Customer-table measure — and the SQL must not filter it.
                filter_table = ""
                if sc.get("filters"):
                    first = sc["filters"][0][0]
                    filter_table = first.split("[", 1)[0].strip().strip("'")
                sc["kpis"] = [
                    (k[0], k[1], k[2], fmt_by_name.get(k[0].casefold(), ""),
                     filter_applies(metadata, filter_table, k[2]) if filter_table else True)
                    for k in sc["kpis"]
                ]
                # Add layout-only charts the screenshot did not capture (e.g.
                # visuals on other pages), matching on title.
                #
                # Only on an unfiltered scenario. A chart's field bindings do
                # not change when a slicer moves, so backfilling every scenario
                # asked the model to rewrite the same 21 queries once per
                # slicer value — 210 generated queries instead of 21, for no
                # extra coverage. Filtered scenarios keep whatever visuals they
                # were built with.
                if sc.get("filters"):
                    continue
                have = {str(v.get("title", "")).casefold() for v in sc.get("visuals", [])}
                sc.setdefault("visuals", []).extend(
                    v for v in layout_visuals
                    if str(v.get("title", "")).casefold() not in have
                )
        return scenarios

    # --- generation -------------------------------------------------------
    def _build_plan_without_sql(self, project, scenarios) -> ValidationPlan:
        """Plan for a file datasource — built entirely in Python.

        Every field the file adapter needs is already known deterministically:
        the KPI and its rendered value come from the DAX evaluation, the
        aggregation and column from the measure's own DAX, the dimension from
        the report layout, and the filters from the scenario. No model is asked
        to restate any of it, so nothing can be hallucinated and no tokens are
        spent on SQL that would be thrown away.
        """
        metadata = self._repo.load_metadata(project)
        dax_of = {
            (m.name or "").casefold(): (m.dax_expression or "")
            for m in (metadata.all_measures if metadata else [])
        }
        items: list[ValidationPlanItem] = []

        for scenario in scenarios:
            filters = [f"{col} = '{val}'" for col, val in (scenario.get("filters") or [])]
            for kpi in scenario.get("kpis", []):
                name, value = kpi[0], kpi[1]
                dax = " ".join((kpi[2] if len(kpi) > 2 else "").split())                     or " ".join(dax_of.get(name.casefold(), "").split())
                table, column, aggregation = self._intent_from_dax(dax)
                items.append(ValidationPlanItem(
                    kpi_name=name, dashboard_value=value,
                    table=table, column=column, aggregation=aggregation,
                    business_meaning=dax, filters=list(filters),
                    scenario=scenario.get("label", ""), item_type="scalar",
                    confidence=1.0 if table else 0.0,
                ))
            for visual in scenario.get("visuals", []):
                dimension = str(visual.get("dimension_field") or "")
                if not dimension:
                    continue
                measure_field = str(visual.get("measure_field") or "")
                measure_name = measure_field.rsplit(".", 1)[-1]
                dax = " ".join(dax_of.get(measure_name.casefold(), "").split())
                table, column, aggregation = self._intent_from_dax(dax)
                items.append(ValidationPlanItem(
                    kpi_name=measure_name or visual.get("title", ""),
                    visual_title=visual.get("title", ""),
                    table=table, column=column, aggregation=aggregation,
                    dimension_column=dimension, filters=list(filters),
                    scenario=scenario.get("label", ""),
                    item_type="grouped" if column else "structural",
                    confidence=1.0,
                ))

        plan = ValidationPlan(items=items, provider=None, model="python (no AI)",
                              raw_response="", batches_total=0, batches_ok=0)
        self._repo.save_validation_plan(project, plan)
        _logger.info(
            "Validation plan for %s: %d item(s) built without AI (file datasource)",
            project.id, len(items),
        )
        return plan

    @staticmethod
    def _intent_from_dax(dax: str) -> tuple[str, str, str]:
        """(table, column, aggregation) for a single-aggregate measure.

        Anything more complex returns blanks; the adapter then compiles the DAX
        itself rather than acting on a half-understood intent.
        """
        import re as _re

        match = _re.match(
            r"^\s*(SUM|AVERAGE|AVG|MIN|MAX|COUNT|COUNTA|DISTINCTCOUNT)\s*\(\s*"
            r"'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)\s*$",
            dax or "", _re.IGNORECASE)
        if not match:
            return "", "", ""
        function, table, column = match.groups()
        return table.strip(), column.strip(), function.upper()

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

        # A file datasource executes the plan's structured intent, compiling the
        # measure's own DAX. Asking the model for SQL would spend the largest
        # part of the token budget on text that is then discarded — and the
        # report would show a query that never ran.
        datasource = self._repo.load_datasource(project)
        if datasource and datasource.type in (DatasourceType.EXCEL, DatasourceType.CSV):
            return self._build_plan_without_sql(project, scenarios)

        schema = self._repo.load_db_schema(project)
        if not schema or not schema.tables:
            raise ValidationError(
                "No datasource schema found. Read the database schema on the "
                "Datasource page first."
            )

        dialect = _DIALECTS.get(schema.datasource_type, "ANSI SQL")
        client = client or create_client(settings)
        system = PLAN_SYSTEM_PROMPT.replace("{dialect}", dialect)
        # Send only the tables the dashboard actually touches (plus their join
        # partners). A warehouse can hold 50 tables while the model uses 11;
        # the rest is pure token cost and mapping noise.
        model_meta = self._repo.load_metadata(project)
        wanted = {t.name for t in (model_meta.tables if model_meta else [])}
        from src.core.config import load_config

        # Identifiers only unless the operator has explicitly opted in.
        allow_samples = bool(getattr(load_config(), 'send_sample_values_to_llm', False))
        schema_text = schema.compact_text(wanted=wanted, include_samples=allow_samples)

        # Resolve model table -> warehouse table deterministically. Left to the
        # AI, an exactly-named but unrelated table (SalesLT.Customer) beats the
        # real one (dbo.customer_data) and every value comparison then fails.
        from src.services.validation.table_matcher import (
            format_table_map,
            map_model_tables,
        )

        # Calculated columns exist only in the model. Without their formulas a
        # measure such as SUM(Sales[Profit]) cannot be translated faithfully.
        calc_columns = ""
        if model_meta:
            calc_lines = []
            for table in model_meta.tables:
                # Power BI's hidden per-column date tables contribute ~19
                # Year/Month/Quarter formulas that no query will ever need —
                # pure token cost, and they crowd out the ones that matter.
                if table.name.startswith(_AUTO_DATE_PREFIXES):
                    continue
                for column in table.columns:
                    if column.is_calculated and column.dax_expression:
                        calc_lines.append(
                            f"  {table.name}[{column.name}] = {column.dax_expression}"
                        )
            calc_columns = "\n".join(calc_lines[:40])

        table_map = ""
        if model_meta:
            resolved = map_model_tables(model_meta, schema)
            table_map = format_table_map(resolved)
            for m in resolved:
                if m.candidates > 1:
                    _logger.info(
                        "Table map: %s -> %s (%d shared columns, %d candidates)",
                        m.model_table, m.db_table, m.shared_columns, m.candidates,
                    )
        _logger.info(
            "Schema prompt: %d of %d tables, ~%d chars",
            len(schema.relevant_tables(wanted)), len(schema.tables), len(schema_text),
        )


        # One call per batch: asking for 30+ queries at once reliably overflows
        # the output budget and truncates the JSON mid-object.
        batch_size = int(getattr(load_config(), 'max_items_per_call',
                                 self.MAX_ITEMS_PER_CALL) or self.MAX_ITEMS_PER_CALL)
        batches = self._batch_scenarios(scenarios, batch_size)
        items: list[ValidationPlanItem] = []
        raw_parts: list[str] = []
        errors: list[str] = []
        budget_exhausted = False
        model_name = client.model

        for index, batch in enumerate(batches, start=1):
            cancellation.raise_if_cancelled()
            try:
                # ~300 tokens per generated query, plus JSON overhead.
                items_in_batch = sum(
                    len(sc.get('kpis', [])) + len(sc.get('visuals', [])) for sc in batch
                ) or batch_size
                response = client.complete(
                    system,
                    build_plan_user_prompt(batch, schema_text, dialect, table_map,
                                           calc_columns),
                    # A generated SELECT is ~60-90 tokens; 300 each reserved
                    # three times what any query has ever used, and providers
                    # bill the reservation whether or not it is consumed.
                    max_tokens=150 * items_in_batch + 300,
                )
            except OperationCancelled:
                raise  # a user decision, not a batch failure — stop immediately
            except TokenBudgetExhausted as exc:
                # The key is spent for today. Every remaining batch would fail
                # the same way, so stop here and keep what was generated: a
                # partial plan still validates the KPIs it covers, and
                # coverage_note() tells the reader exactly what is missing.
                errors.append(
                    f"stopped after batch {index - 1} of {len(batches)}: {exc}"
                )
                _logger.warning(
                    "Daily token budget reached at batch %d/%d — keeping %d "
                    "query/queries generated so far.", index, len(batches), len(items),
                )
                budget_exhausted = True
                break
            except Exception as exc:  # noqa: BLE001 - one batch must not kill all
                errors.append(f"batch {index}: {exc}")
                _logger.warning("Plan batch %d/%d failed: %s", index, len(batches), exc)
                continue

            model_name = response.model or model_name
            raw_parts.append(f"--- batch {index} ---\n{response.content}")
            try:
                items.extend(self._parse(response.content, batch))
            except LLMResponseError as exc:
                errors.append(f"batch {index}: {exc}")
                _logger.warning("Plan batch %d/%d unparseable: %s", index, len(batches), exc)

        if not items:
            if budget_exhausted:
                # Nothing was generated *and* the key is spent: re-raise so the
                # pipeline skips its remaining AI stages rather than treating
                # this as an ordinary generation failure worth retrying.
                raise TokenBudgetExhausted(errors[-1] if errors else
                                           "Daily token budget exhausted.")
            raise LLMResponseError(
                "Could not generate any SQL. " + (errors[0] if errors else "")
            )

        # A batch may echo back a KPI that belonged to another batch; keep the
        # first (highest-confidence) mapping per scenario/KPI/type.
        deduped: dict[tuple[str, str, str], ValidationPlanItem] = {}
        for item in items:
            key = (item.scenario, item.kpi_name.casefold(), item.item_type)
            existing = deduped.get(key)
            if existing is None or item.confidence > existing.confidence:
                deduped[key] = item
        items = list(deduped.values())

        plan = ValidationPlan(
            items=items,
            provider=settings.provider,
            model=model_name,
            raw_response="\n\n".join(raw_parts),
            batches_total=len(batches),
            batches_ok=len(raw_parts),
            errors=errors,
            budget_exhausted=budget_exhausted,
        )
        if not plan.is_complete:
            _logger.warning("Validation plan INCOMPLETE: %s", plan.coverage_note())
        self._repo.save_validation_plan(project, plan)
        _logger.info(
            "Validation plan for %s: %d item(s) from %d batch(es); %d batch error(s)",
            project.id, len(items), len(batches), len(errors),
        )
        return plan

    # --- parsing ----------------------------------------------------------
    #: Requesting too many queries at once overflows the model's output budget
    #: and truncates the JSON. Batching keeps each response comfortably small.
    MAX_ITEMS_PER_CALL = 15

    @staticmethod
    def _batch_scenarios(scenarios: list[dict], max_items: int) -> list[list[dict]]:
        """Split scenarios so each batch asks for at most *max_items* queries."""
        batches: list[list[dict]] = []
        current: list[dict] = []
        used = 0

        for scenario in scenarios:
            kpis = list(scenario.get("kpis", []))
            visuals = list(scenario.get("visuals", []))
            k = v = 0
            while k < len(kpis) or v < len(visuals):
                if used >= max_items:
                    batches.append(current)
                    current, used = [], 0
                room = max_items - used
                take_k = kpis[k : k + room]
                k += len(take_k)
                room -= len(take_k)
                take_v = visuals[v : v + room] if room > 0 else []
                v += len(take_v)
                if not take_k and not take_v:
                    break
                piece = dict(scenario)
                piece["kpis"], piece["visuals"] = take_k, take_v
                current.append(piece)
                used += len(take_k) + len(take_v)

        if current:
            batches.append(current)
        return batches or [scenarios]

    @staticmethod
    def _salvage_items(content: str) -> list[dict]:
        """Recover complete plan objects from truncated or prose-wrapped JSON.

        A cut-off response still contains many valid ``{...}`` items; returning
        those beats discarding an expensive call entirely.
        """
        import json as _json

        keys = {"generated_sql", "sql", "kpi_name"}
        found: list[dict] = []
        stack: list[int] = []
        in_str = esc = False
        for i, ch in enumerate(content or ""):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                start = stack.pop()
                try:
                    obj = _json.loads(content[start : i + 1])
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict) and (keys & obj.keys()):
                    found.append(obj)
        return found

    @classmethod
    def _parse(cls, content: str, scenarios: list[dict]) -> list[ValidationPlanItem]:
        data = extract_json(content)
        if isinstance(data, dict):
            data = data.get("validation_plan", data.get("plan", data.get("items")))
        if not isinstance(data, list) or not data:
            data = cls._salvage_items(content)
            if data:
                _logger.warning(
                    "Recovered %d plan item(s) from a partial response.", len(data)
                )
        if not isinstance(data, list) or not data:
            snippet = " ".join((content or "").split())[:180] or "(empty response)"
            raise LLMResponseError(
                "The LLM did not return a usable validation plan. This is usually a "
                "truncated response — raise 'Max tokens' or use a larger model. "
                f"Model said: {snippet}"
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
