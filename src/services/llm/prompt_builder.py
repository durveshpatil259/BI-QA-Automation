"""Build LLM prompts from the deterministic :class:`AnalysisContext`.

The system prompt fixes the AI's role and hard constraints (reason only over the
provided deterministic results; never invent data). The user prompt is a
compact, bounded serialization of the context so the model gets the signal it
needs without unbounded token cost.
"""

from __future__ import annotations

from src.domain.models import AnalysisContext

_MAX_DAX = 200
_MAX_FINDINGS = 60
_MAX_TABLES = 40
_MAX_TEXT = 400

SYSTEM_PROMPT = (
    "You are a senior Business Intelligence QA analyst. You are given the results "
    "of a DETERMINISTIC analysis that Python already performed on a BI dashboard: "
    "extracted metadata, datasource comparisons, validation findings and (optionally) "
    "screenshot facts.\n\n"
    "Your job is ONLY to reason over these provided results and produce narrative "
    "output. You MUST NOT invent tables, measures, numbers, or findings that are not "
    "present in the input. You do not have access to any database, SQL, or dashboard "
    "files — do not claim to have run queries or opened files.\n\n"
    "Respond with STRICT JSON (no markdown, no code fences) using exactly these keys:\n"
    '  "executive_summary": string — a concise business-facing summary of dashboard '
    "quality and readiness.\n"
    '  "root_cause_analysis": string — for the failing/critical findings, explain the '
    "likely underlying causes, grounded in the provided evidence.\n"
    '  "recommendations": array of strings — concrete, prioritized remediation steps.\n'
)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _metadata_section(ctx: AnalysisContext) -> list[str]:
    md = ctx.metadata
    if not md:
        return ["METADATA: none (visual-only analysis)."]
    counts = md.summary_counts()
    lines = [
        f"METADATA (model '{md.model_name}'): " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        "Tables:",
    ]
    for t in md.tables[:_MAX_TABLES]:
        tag = " [calculated]" if t.is_calculated else ""
        lines.append(f"  - {t.name}{tag}: {len(t.columns)} cols, {len(t.measures)} measures")
    if len(md.tables) > _MAX_TABLES:
        lines.append(f"  … and {len(md.tables) - _MAX_TABLES} more tables")
    measures = md.all_measures
    if measures:
        lines.append("Measures (name = DAX):")
        for m in measures[:_MAX_TABLES]:
            lines.append(f"  - {m.table}[{m.name}] = {_clip(m.dax_expression, _MAX_DAX)}")
    return lines


def _validation_section(ctx: AnalysisContext) -> list[str]:
    summary = ctx.validation_summary()
    lines = [
        f"VALIDATION SUMMARY: {summary['total']} checks, {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['critical']} critical."
    ]
    failing = [v for v in ctx.validations if not v.passed]
    if failing:
        lines.append("Failing findings:")
        for v in failing[:_MAX_FINDINGS]:
            lines.append(
                f"  - [{v.rule_id}|{v.severity}] {v.title} | entity={v.entity} | "
                f"{_clip(v.description, _MAX_TEXT)}"
            )
        if len(failing) > _MAX_FINDINGS:
            lines.append(f"  … and {len(failing) - _MAX_FINDINGS} more failing findings")
    else:
        lines.append("No failing findings.")
    return lines


def _comparison_section(ctx: AnalysisContext) -> list[str]:
    if not ctx.comparisons:
        return ["DATASOURCE COMPARISON: none (no datasource configured)."]
    lines = [f"DATASOURCE COMPARISON ({ctx.datasource_type}):"]
    for c in ctx.comparisons:
        mark = "MATCH" if c.matched else "MISMATCH"
        detail = f" — {c.difference}" if c.difference else ""
        lines.append(
            f"  - [{mark}|{c.severity}] {c.label}: dashboard='{c.dashboard_value}' "
            f"vs datasource='{c.datasource_value}'{detail}"
        )
    if ctx.data_results:
        lines.append("Datasource facts:")
        for d in ctx.data_results:
            lines.append(f"  - {d.label} = {d.scalar_value}")
    return lines


def _visual_section(ctx: AnalysisContext) -> list[str]:
    va = ctx.visual_analysis
    if not va or not va.screenshots:
        return []
    lines = [f"SCREENSHOTS: {va.total_screenshots} processed."]
    for s in va.screenshots:
        dims = f"{s.width}x{s.height}" if s.width else "unknown"
        line = f"  - {s.file_name} ({s.format or '?'}, {dims})"
        if s.detected_text:
            line += f" text: {_clip(s.detected_text, _MAX_TEXT)}"
        lines.append(line)
    return lines


EXPLAIN_SYSTEM_PROMPT = (
    "You are a BI QA analyst. You are given data-validation FAILURES where a "
    "dashboard KPI value did not match the value computed from the database by an "
    "already-executed SQL query. For each failure, explain the most likely root "
    "cause and a concrete recommendation. You did not run anything; reason only over "
    "the provided facts.\n\n"
    "Respond with STRICT JSON (no fences): an object with key \"explanations\" whose "
    'value is an array of {"test_id": string, "recommendation": string}.'
)


def build_explain_user_prompt(failures) -> str:
    lines = ["FAILURES:"]
    for r in failures:
        lines.append(
            f"- test_id={r.test_id} | KPI={r.kpi_name} | dashboard={r.dashboard_value} "
            f"| database={r.database_value} | diff={r.difference} | reason={r.reason}"
        )
        if r.generated_sql:
            lines.append(f"    SQL: {r.generated_sql}")
    lines.append("")
    lines.append("Return the STRICT JSON explanations now (one per test_id).")
    return "\n".join(lines)


PLAN_SYSTEM_PROMPT = (
    "You are a senior BI data-validation engineer. For EACH dashboard KPI you are "
    "given, write ONE read-only SQL query that reproduces that KPI's number from the "
    "source database, so it can be compared against the dashboard.\n\n"
    "You are given: the KPI name and its displayed value, the DAX formula behind it "
    "when available, the slicer values ACTIVE on the dashboard, and the database "
    "schema with sample column values and join paths.\n\n"
    "HARD RULES\n"
    "1. Use ONLY tables/columns from the provided schema. Never invent a name.\n"
    "2. JOIN whenever the measure and the filter live in different tables. Use the "
    "listed JOIN PATHS (they are reliable even when no foreign key is declared).\n"
    "3. Apply EVERY active dashboard filter as a WHERE clause. This is mandatory — a "
    "KPI displayed under Fiscal Year = FY2020 MUST be filtered to FY2020, or the "
    "comparison is meaningless.\n"
    "4. Match literals to the sample values shown for that column (e.g. write "
    "'FY2020', not 2020, if that is the real stored format).\n"
    "5. Mirror the DAX when given: SUM->SUM, DISTINCTCOUNT->COUNT(DISTINCT ...), "
    "AVERAGE->AVG, DIVIDE(a,b)->a*1.0/NULLIF(b,0).\n"
    "6. FORMAT THE RESULT TO MATCH THE DASHBOARD EXACTLY. Look at the displayed "
    "value and reproduce that exact string in SQL, so the query output can be "
    "compared character-for-character. Apply the same scaling, rounding, currency "
    "symbol and suffix:\n"
    "     $51.88M  -> CONCAT('$', ROUND(SUM(x)/1000000.0, 2), 'M')\n"
    "     11.2%    -> CONCAT(ROUND(100.0*SUM(a)/NULLIF(SUM(b),0), 1), '%')\n"
    "     24K      -> CONCAT(ROUND(COUNT(DISTINCT x)/1000.0, 0), 'K')\n"
    "     $2,206   -> CONCAT('$', FORMAT(ROUND(AVG(x), 0), 'N0'))\n"
    "   Return exactly ONE column containing that formatted value.\n"
    "   If no displayed value is given, use the FORMAT guidance instead — it is the "
    "measure's Power BI format string, so following it reproduces exactly how the "
    "dashboard renders that KPI.\n"
    "   Measure DAX may reference OTHER measures in [Brackets] (e.g. "
    "[Total Sales] - [Total Cost]). Expand those into their own SQL expressions.\n"
    "7. A single read-only SELECT per KPI. No semicolons, no INSERT/UPDATE/DELETE/DDL. "
    "You do NOT execute anything — Python runs it.\n"
    "8. Target dialect: {dialect}. Bracket-quote identifiers containing spaces, e.g. "
    "[Sales Amount], [Fiscal Year].\n\n"
    "WORKED EXAMPLE\n"
    "KPI 'Total Sales' displayed as $51.88M, DAX SUM(Sales[Sales Amount]), active "
    "filter Fiscal Year = FY2020, join path Sales_data.OrderDateKey = "
    "date_data.DateKey. Correct answer:\n"
    "  SELECT CONCAT('$', ROUND(SUM(F.[Sales Amount])/1000000.0, 2), 'M') "
    "FROM dbo.Sales_data F "
    "JOIN dbo.date_data D ON F.OrderDateKey = D.DateKey "
    "WHERE D.[Fiscal Year] = 'FY2020'\n"
    "That returns exactly '$51.88M', matching the dashboard character-for-character.\n\n"
    "You may be given SEVERAL SCENARIOS — the same dashboard captured under "
    "different slicer selections (e.g. one screenshot per fiscal year). Produce a "
    "SEPARATE plan item for EVERY (scenario, KPI) pair, each filtered to that "
    "scenario's slicer values. Copy the scenario id onto each item.\n\n"
    "=== CHARTS, TABLES, MATRICES, GAUGES, MAPS ===\n"
    "You are ALSO given a list of VISUALS (not just KPI cards) — bar/line/donut/pie "
    "charts, tables, matrices, gauges, maps. Each has a dimension_field (its "
    "category/axis) and measure_field (what it plots), and is tagged "
    "values_visible=true or false:\n\n"
    "CASE A — values_visible=true (the chart showed data labels, or it is a "
    "table/matrix/gauge): generate item_type='grouped'. Write ONE SQL query that "
    "GROUPs BY the dimension column and aggregates the measure column, returning "
    "one row per category — e.g. for 'Sales by Category' (dimension=Category, "
    "measure=Sales Amount):\n"
    "  SELECT P.Category, SUM(F.[Sales Amount]) FROM dbo.Sales_data F "
    "JOIN dbo.product_data P ON F.ProductKey = P.ProductKey "
    "GROUP BY P.Category\n"
    "Format the aggregated column to match the data labels shown, same as for KPIs. "
    "Set dimension_column to the GROUP BY column's schema name.\n\n"
    "CASE B — values_visible=false (only shapes/colours, no printed numbers): "
    "generate item_type='structural'. Write a SELECT DISTINCT on the dimension "
    "column only — no aggregation, no numbers — e.g.:\n"
    "  SELECT DISTINCT Region FROM dbo.Sales_Territory_data\n"
    "This validates that the categories shown on the chart actually exist in the "
    "data (and none are missing), even though the exact plotted values cannot be "
    "read from a screenshot. Set dimension_column to that DISTINCT column.\n\n"
    "Skip a visual only if it has no usable dimension_field (e.g. a slicer, or a "
    "purely decorative image).\n\n"
    "Respond with STRICT JSON (no markdown/fences): an object with key "
    '"validation_plan" whose value is an array of objects with keys:\n'
    '  "scenario": string (the scenario id you were given, e.g. "S1")\n'
    '  "item_type": "scalar" | "grouped" | "structural"\n'
    '  "kpi_name": string (KPI name for scalar; the CHART TITLE for grouped/structural)\n'
    '  "table": string    (main fact table used)\n'
    '  "column": string   (measured column; blank for structural)\n'
    '  "dimension_column": string (GROUP BY / DISTINCT column; blank for scalar)\n'
    '  "aggregation": string (SUM/AVG/COUNT/COUNT DISTINCT/…; blank for structural)\n'
    '  "business_meaning": string (what it measures, in one line)\n'
    '  "filters": [string] (each WHERE condition you applied)\n'
    '  "generated_sql": string (single read-only SELECT; scalar formats to match the '
    "displayed value, grouped returns (dimension, value) rows, structural returns "
    "distinct dimension values only)\n"
    '  "confidence": number 0-1 (your mapping confidence)\n'
)


def build_plan_user_prompt(scenarios, schema_text: str, dialect: str) -> str:
    """Build the mapping prompt from one or more filter scenarios.

    ``scenarios`` is a list of dicts::

        {"id": "S1", "label": "Fiscal Year=FY2020",
         "filters": [(name, selected), ...],
         "kpis": [(name, displayed_value, dax), ...],
         "visuals": [{"title", "visual_type", "dimension_field", "measure_field",
                      "values_visible", "categories": [name, ...]}, ...]}
    """
    lines = [f"DATASOURCE DIALECT: {dialect}", ""]
    lines.append(
        f"SCENARIOS ({len(scenarios)}) — each is the same dashboard under different "
        "slicer selections. Generate one plan item per (scenario, KPI) AND per "
        "(scenario, visual)."
    )
    lines.append("")

    for sc in scenarios:
        lines.append(f"SCENARIO {sc['id']} — {sc['label']}")
        lines.append("  Active filters (apply ALL in the WHERE clause):")
        if sc.get("filters"):
            for name, selected in sc["filters"]:
                lines.append(f"    - {name} = {selected}")
        else:
            lines.append("    (none — query the full dataset)")

        lines.append("  KPIs displayed in this scenario:")
        for kpi in sc.get("kpis", []):
            name, value = kpi[0], kpi[1]
            dax = kpi[2] if len(kpi) > 2 else ""
            fmt = kpi[3] if len(kpi) > 3 else ""
            line = f"    - {name}"
            if value:
                line += f"  | displayed: {value}  (MATCH THIS FORMAT EXACTLY)"
            if dax:
                line += f"  | DAX: {dax}"
            if fmt:
                # Power BI format string — authoritative when no screenshot
                # value exists, so the SQL still returns dashboard-shaped output.
                line += f"  | FORMAT: {fmt}"
            lines.append(line)

        visuals = sc.get("visuals", [])
        if visuals:
            lines.append("  Charts/tables/matrices displayed in this scenario:")
            for v in visuals:
                mode = "VALUES VISIBLE -> item_type=grouped" if v.get("values_visible") \
                    else "no numbers visible -> item_type=structural"
                lines.append(
                    f"    - '{v.get('title', '')}' ({v.get('visual_type', '')}) | "
                    f"dimension_field={v.get('dimension_field', '?')} | "
                    f"measure_field={v.get('measure_field', '?')} | {mode}"
                )
                cats = v.get("categories", [])
                if cats:
                    lines.append(f"      categories shown: {', '.join(cats[:20])}")
        lines.append("")

    lines += ["DATABASE SCHEMA:", schema_text or "(no schema provided)", ""]
    lines.append(
        "Produce the STRICT JSON validation_plan now — one item per (scenario, KPI) "
        "AND one item per (scenario, chart/table/matrix). JOIN to the tables holding "
        "the filter and dimension columns, apply that scenario's filters in the WHERE "
        "clause, format scalar output to match the displayed value "
        "character-for-character, use GROUP BY for grouped items, and SELECT DISTINCT "
        "for structural items."
    )
    return "\n".join(lines)


VISION_SYSTEM_PROMPT = (
    "You are a Business Intelligence dashboard vision analyst. You are given one or "
    "more screenshots of a Power BI (or similar) dashboard. Extract what is visible "
    "into STRICT JSON — no markdown, no prose, no code fences.\n\n"
    "Read values EXACTLY as displayed (keep suffixes and symbols, e.g. '109.81M', "
    "'11.4%', '$1,234'). Do not compute or invent values; only report what you see.\n\n"
    "For EVERY chart, table, matrix, gauge, map or donut — not just KPI cards — you "
    "MUST determine whether exact per-category numbers are readable:\n"
    "- If the chart has DATA LABELS printed on it, or it is a TABLE/MATRIX (whose "
    "cells always show exact values), or a GAUGE (whose needle value is printed): "
    "set values_visible=true and list every row/category with its exact value in "
    "data_points.\n"
    "- If the chart shows ONLY bar length, line shape, slice size or map colour "
    "with NO printed numbers (a common default in Power BI): set "
    "values_visible=false, and list data_points with dimension names ONLY (leave "
    "value blank) — e.g. for an unlabeled bar chart of regions, still list every "
    "region name shown on the axis, just without a value.\n"
    "Also identify, for each chart: dimension_field (the category/axis being "
    "grouped, e.g. 'Region', 'Category', 'Year') and measure_field (what is being "
    "measured, e.g. 'Sales Amount').\n\n"
    "Return an object with these keys:\n"
    '  "kpis": array of {"name": string, "value": string}  — single-number KPI cards\n'
    '  "charts": array of objects, one per chart/table/matrix/gauge/map/donut:\n'
    '    {"visual_type": string, "title": string, "fields": [string], "text": string, '
    '"dimension_field": string, "measure_field": string, "values_visible": boolean, '
    '"data_points": [{"dimension": string, "value": string}]}\n'
    "    visual_type is one of: bar_chart, line_chart, donut, pie, table, matrix, "
    "gauge, map, treemap, card, slicer\n"
    '  "filters": array of {"name": string, "selected": string}  — EVERY slicer, '
    'with its currently selected value. Use "All" when nothing is narrowed. This '
    "is critical: a KPI shown under Fiscal Year = FY2020 is only valid for FY2020.\n"
    '  "visible_text": string  — other notable on-screen text/titles\n'
)

VISION_USER_PROMPT = (
    "Analyse the attached dashboard screenshot(s) and return the STRICT JSON object "
    "described in the system message. Capture:\n"
    "1. Every KPI card with its exact displayed value.\n"
    "2. Every OTHER visual (bar/line/donut/pie/table/matrix/gauge/map/treemap) with "
    "its title, dimension_field, measure_field, and its full list of data_points — "
    "with real values when data labels/cells/needle are visible (values_visible=true), "
    "or category names only when the chart shows shape/colour but no printed numbers "
    "(values_visible=false). Do NOT skip a chart just because it has no visible "
    "numbers — still list its categories with values_visible=false.\n"
    "3. Every slicer WITH the value currently selected in it (read the text inside "
    "the slicer box, e.g. 'FY2020' or 'All')."
)


TESTCASE_SYSTEM_PROMPT = (
    "You are a senior BI QA engineer. Using ONLY the deterministic analysis results "
    "provided, generate enterprise QA and developer unit test cases for the dashboard.\n\n"
    "Rules:\n"
    "- Base every test case on evidence in the input (a validation finding, a "
    "measure, a relationship, a comparison, a visual). Do NOT invent entities.\n"
    "- Cover both failing findings (regression/negative tests) and important passing "
    "areas (confirmation tests).\n"
    "- Provide a mix of kind='unit' (developer, e.g. DAX/measure logic, model "
    "integrity) and kind='qa' (functional, e.g. visuals, filters, data reconciliation).\n"
    "- Do NOT fill in actual_result, status or remarks — those are populated "
    "deterministically afterwards. When a test relates to a specific validation "
    "finding, set related_rule_id to that finding's rule id and related_entity to its "
    "entity so results can be linked.\n\n"
    "Respond with STRICT JSON (no markdown, no fences): an object with key "
    '"test_cases" whose value is an array of objects with these keys:\n'
    '  "kind": "unit" | "qa"\n'
    '  "module": string\n'
    '  "test_scenario": string\n'
    '  "test_steps": string (numbered steps, newline-separated)\n'
    '  "test_data": string\n'
    '  "expected_result": string\n'
    '  "priority": "High" | "Medium" | "Low"\n'
    '  "related_rule_id": string (optional, e.g. "MD-001")\n'
    '  "related_entity": string (optional, e.g. "Sales[Total]")\n'
)


def build_testcase_user_prompt(ctx: AnalysisContext) -> str:
    """Same evidence as reasoning, framed for test-case generation."""
    parts: list[str] = [
        f"PROJECT: {ctx.project_name} | Platform: {ctx.platform} | Mode: {ctx.analysis_mode}",
        "",
        *_metadata_section(ctx),
        "",
        *_validation_section(ctx),
        "",
        *_comparison_section(ctx),
    ]
    visual = _visual_section(ctx)
    if visual:
        parts += ["", *visual]
    parts += [
        "",
        "Generate the STRICT JSON test_cases described in the system message now, "
        "each grounded in the evidence above. Prefer 6-12 well-targeted, CONCISE cases "
        "(keep test_steps brief) and return ONLY the JSON object — no prose or fences.",
    ]
    return "\n".join(parts)


def build_user_prompt(ctx: AnalysisContext) -> str:
    parts: list[str] = [
        f"PROJECT: {ctx.project_name} | Platform: {ctx.platform} | Mode: {ctx.analysis_mode}",
        "",
        *_metadata_section(ctx),
        "",
        *_validation_section(ctx),
        "",
        *_comparison_section(ctx),
    ]
    visual = _visual_section(ctx)
    if visual:
        parts += ["", *visual]
    parts += [
        "",
        "Produce the STRICT JSON described in the system message now, grounded only in "
        "the evidence above.",
    ]
    return "\n".join(parts)
