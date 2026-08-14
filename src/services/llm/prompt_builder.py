"""Build LLM prompts from the deterministic :class:`AnalysisContext`.

The system prompt fixes the AI's role and hard constraints (reason only over the
provided deterministic results; never invent data). The user prompt is a
compact, bounded serialization of the context so the model gets the signal it
needs without unbounded token cost.
"""

from __future__ import annotations

from src.domain.models import AnalysisContext

# Every section is bounded. The reasoning prompt writes a narrative summary —
# it needs the SHAPE of the model and the FAILURES, not an exhaustive dump.
# Small hosted models have tight per-minute budgets (llama-3.1-8b-instant is
# 6,000 TPM), and max_tokens is charged against that budget too.
_MAX_DAX = 90
_MAX_FINDINGS = 20
_MAX_TABLES = 15
_MAX_MEASURES = 15
_MAX_COMPARISONS = 15
_MAX_FACTS = 10
_MAX_TEXT = 200

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
        for m in measures[:_MAX_MEASURES]:
            lines.append(f"  - {m.table}[{m.name}] = {_clip(m.dax_expression, _MAX_DAX)}")
        if len(measures) > _MAX_MEASURES:
            lines.append(f"  … and {len(measures) - _MAX_MEASURES} more measures")
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

    # Mismatches first — a summary is written from what went wrong, and a long
    # tail of "MATCH" rows is the least useful thing to spend tokens on.
    ordered = sorted(ctx.comparisons, key=lambda c: c.matched)
    shown = ordered[:_MAX_COMPARISONS]
    matched = sum(1 for c in ctx.comparisons if c.matched)

    lines = [
        f"DATASOURCE COMPARISON ({ctx.datasource_type}): "
        f"{len(ctx.comparisons)} checks, {matched} matched, "
        f"{len(ctx.comparisons) - matched} mismatched."
    ]
    for c in shown:
        mark = "MATCH" if c.matched else "MISMATCH"
        detail = f" — {_clip(c.difference, 120)}" if c.difference else ""
        lines.append(
            f"  - [{mark}|{c.severity}] {c.label}: dashboard='{c.dashboard_value}' "
            f"vs datasource='{c.datasource_value}'{detail}"
        )
    if len(ctx.comparisons) > len(shown):
        lines.append(f"  … and {len(ctx.comparisons) - len(shown)} more checks")

    if ctx.data_results:
        lines.append("Datasource facts:")
        for d in ctx.data_results[:_MAX_FACTS]:
            lines.append(f"  - {d.label} = {d.scalar_value}")
        if len(ctx.data_results) > _MAX_FACTS:
            lines.append(f"  … and {len(ctx.data_results) - _MAX_FACTS} more")
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


#: Written for what the model still does. Compile-first means every measure
#: Python can express is turned into SQL without an LLM call, so what arrives
#: here is the residue — ranking, share-of-total, running totals — plus the
#: visuals. Rules earn their place by a failure observed on a real run, not by
#: sounding prudent; the trace for each is in the git history of this file.
#:
#: The refusal path is the important part. Python's compiler declines what it
#: cannot express, which is why its output can be trusted. A model with no way
#: to decline invents instead: YoY% came back as (total - total) / total, zero
#: by construction, and matched a dashboard also showing 0.0%.
PLAN_SYSTEM_PROMPT = (
    "You write ONE read-only SQL query per item so Python can compare its result "
    "to what a Power BI dashboard displays. You never execute anything.\n\n"

    "Simple measures are already compiled to SQL by Python and never reach you. "
    "What you get is the hard residue — ranking, share-of-total, running totals — "
    "plus charts. Expect to work harder per item than the wording of a measure "
    "suggests.\n\n"

    "RULES\n"
    "1. Use only table and column names from the schema below. Never invent one.\n"
    "2. Every statement must be complete and standalone: SELECT ... FROM ... . If "
    "you alias a table you MUST bind that alias in a FROM or JOIN clause.\n"
    "   WRONG: SELECT SUM(F.[Sales Amount])            -- no FROM; F is unbound\n"
    "   RIGHT: SELECT SUM(F.[Sales Amount]) FROM dbo.Sales_data F\n"
    "3. Apply EVERY active filter you are given as a WHERE clause. A KPI shown "
    "under Fiscal Year = FY2020 that is not filtered to FY2020 is not a "
    "comparison, it is a different number.\n"
    "4. Match literals to how the column stores them. A fiscal year held as "
    "'FY2018' is text: compare it to text, and never do arithmetic on it.\n"
    "   WRONG: WHERE [Fiscal Year] = (SELECT MAX([Fiscal Year]) - 1 FROM ...)\n"
    "5. {dialect}. Bracket-quote any identifier containing a space or a dash: "
    "[Sales Amount], [Fiscal Year], [State-Province].\n"
    "6. Never nest an aggregate inside another aggregate. Put the inner one in "
    "its own scalar subquery.\n"
    "   WRONG: SUM(CASE WHEN x = (SELECT MAX(y) FROM t) THEN a END)\n"
    "   RIGHT: compute the inner value in a subquery in the WHERE clause\n"
    "7. An aggregate over a dimension table counts the WHOLE dimension, not the "
    "rows that happen to join to a fact. Put it in its own scalar subquery so "
    "the join cannot filter it, and apply the scenario filter inside that "
    "subquery when the filter is on that same dimension.\n"
    "8. Format the result to match the displayed value character-for-character, "
    "returning exactly ONE column:\n"
    "     $51.88M -> CONCAT('$', ROUND(SUM(x)/1000000.0, 2), 'M')\n"
    "     $2,206  -> CONCAT('$', FORMAT(ROUND(AVG(x), 0), 'N0'))\n"
    "   A '%' format code already multiplies by 100 — multiplying as well scales "
    "the answer by 10,000:\n"
    "     WRONG: FORMAT(100.0 * a / b, '0.0%')\n"
    "     RIGHT: FORMAT(a / NULLIF(b, 0), '0.0%')\n"
    "9. A single read-only SELECT. No semicolons, no INSERT/UPDATE/DELETE/DDL.\n\n"

    "IF YOU CANNOT WRITE IT FAITHFULLY, SAY SO\n"
    "Some DAX has no honest SQL equivalent against this schema. When that "
    "happens, return the item with \"generated_sql\": \"\" and "
    "\"confidence\": 0, and put the reason in \"business_meaning\". Python then "
    "reports it as needing manual review.\n"
    "Do NOT approximate. A query that returns a plausible number for the wrong "
    "calculation is far worse than one that is absent: it will be compared, it "
    "may match by coincidence, and the report will call it correct. If a "
    "measure references another measure you could not express, you cannot "
    "express this one either.\n\n"

    "VISUALS\n"
    "Each visual has a dimension (its axis/category) and a measure, and is "
    "tagged values_visible.\n"
    "* values_visible=true -> item_type='grouped'. ONE query that GROUPs BY the "
    "dimension and aggregates the measure, one row per category:\n"
    "    SELECT P.Category, SUM(F.[Sales Amount]) FROM dbo.Sales_data F "
    "JOIN dbo.product_data P ON F.ProductKey = P.ProductKey GROUP BY P.Category\n"
    "* values_visible=false -> item_type='structural'. SELECT DISTINCT on the "
    "dimension only, no aggregation — this checks the categories exist.\n"
    "Set dimension_column to the GROUP BY / DISTINCT column. Skip a visual only "
    "when it has no usable dimension.\n\n"

    "You may be given several SCENARIOS — the same dashboard under different "
    "slicer selections. Produce a separate item for every (scenario, KPI) pair, "
    "filtered to that scenario, and copy the scenario id onto each item.\n\n"

    "Reply with STRICT JSON only, no markdown fences: "
    '{"validation_plan": [ ... ]} where each item has:\n'
    '  scenario, item_type ("scalar"|"grouped"|"structural"), kpi_name '
    "(chart title for grouped/structural), table, column, dimension_column, "
    "aggregation, business_meaning, filters (array of the WHERE conditions you "
    "applied), generated_sql, confidence (0-1).\n"
)


def build_plan_user_prompt(
    scenarios, schema_text: str, dialect: str, table_map: str = "",
    calculated_columns: str = "",
) -> str:
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
            # Power BI relationships are directional: a slicer on one dimension
            # does not reach another dimension across the fact table. Without
            # this the model filtered every KPI by every slicer.
            if len(kpi) > 4 and kpi[4] is False:
                line += ("  | *** THIS KPI IS NOT AFFECTED BY THE ACTIVE FILTER "
                         "(no relationship path) — DO NOT add a WHERE clause or "
                         "JOIN for it; return the same unfiltered value ***")
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

    # The notation has to be stated or it is guessable-but-ambiguous: a bare
    # asterisk reads as a wildcard, and "FK a -> b.c" could be either direction.
    lines += [
        "DATABASE SCHEMA — written as schema.table(column, column, …). "
        "A trailing * marks a primary key. 'date/time:' lists the columns with "
        "a date or timestamp type; every other column is non-temporal. "
        "'FK col -> table.col' is a foreign key on the table it appears under.",
        schema_text or "(no schema provided)",
        "",
    ]
    # Placed after the schema so it is the last thing read before the task:
    # which physical table backs each model table is settled by Python, not
    # inferred from name similarity.
    if table_map:
        lines += [table_map, ""]
    # A measure like SUM(Sales[Profit]) is unreadable without knowing what the
    # Profit *column* is. Left unstated, the model invents a plausible formula
    # (Sales Amount - Total Product Cost) that is not what the dashboard does.
    if calculated_columns:
        lines += [
            "CALCULATED COLUMNS — these are computed inside the model, so the "
            "database has no such column. Expand the formula inline in SQL "
            "instead of selecting the column by name:",
            calculated_columns,
            "",
        ]
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
