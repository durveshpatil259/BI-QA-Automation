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
    "Respond with STRICT JSON (no markdown/fences): an object with key "
    '"validation_plan" whose value is an array of objects with keys:\n'
    '  "scenario": string (the scenario id you were given, e.g. "S1")\n'
    '  "kpi_name": string (must match one of the given KPI names exactly)\n'
    '  "table": string    (main fact table used)\n'
    '  "column": string   (measured column)\n'
    '  "aggregation": string (SUM/AVG/COUNT/COUNT DISTINCT/…)\n'
    '  "business_meaning": string (what it measures, in one line)\n'
    '  "filters": [string] (each WHERE condition you applied)\n'
    '  "generated_sql": string (single read-only SELECT, formatted to match the '
    "displayed value)\n"
    '  "confidence": number 0-1 (your mapping confidence)\n'
)


def build_plan_user_prompt(scenarios, schema_text: str, dialect: str) -> str:
    """Build the mapping prompt from one or more filter scenarios.

    ``scenarios`` is a list of dicts::

        {"id": "S1", "label": "Fiscal Year=FY2020",
         "filters": [(name, selected), ...],
         "kpis": [(name, displayed_value, dax), ...]}
    """
    lines = [f"DATASOURCE DIALECT: {dialect}", ""]
    lines.append(
        f"SCENARIOS ({len(scenarios)}) — each is the same dashboard under different "
        "slicer selections. Generate one plan item per (scenario, KPI)."
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
        lines.append("")

    lines += ["DATABASE SCHEMA:", schema_text or "(no schema provided)", ""]
    lines.append(
        "Produce the STRICT JSON validation_plan now — one item per (scenario, KPI). "
        "JOIN to the tables holding the filter columns, apply that scenario's filters "
        "in the WHERE clause, and format the output to match the displayed value "
        "character-for-character."
    )
    return "\n".join(lines)


VISION_SYSTEM_PROMPT = (
    "You are a Business Intelligence dashboard vision analyst. You are given one or "
    "more screenshots of a Power BI (or similar) dashboard. Extract what is visible "
    "into STRICT JSON — no markdown, no prose, no code fences.\n\n"
    "Read values EXACTLY as displayed (keep suffixes and symbols, e.g. '109.81M', "
    "'11.4%', '$1,234'). Do not compute or invent values; only report what you see.\n\n"
    "Return an object with these keys:\n"
    '  "kpis": array of {"name": string, "value": string}  — KPI/card metrics\n'
    '  "charts": array of {"visual_type": string, "title": string, '
    '"fields": [string], "text": string}  — visual_type like bar_chart, line_chart, '
    "pie/donut, table, matrix, gauge, map, treemap, slicer, card\n"
    '  "filters": array of {"name": string, "selected": string}  — EVERY slicer, '
    'with its currently selected value. Use "All" when nothing is narrowed. This '
    "is critical: a KPI shown under Fiscal Year = FY2020 is only valid for FY2020.\n"
    '  "visible_text": string  — other notable on-screen text/titles\n'
)

VISION_USER_PROMPT = (
    "Analyse the attached dashboard screenshot(s) and return the STRICT JSON object "
    "described in the system message. Capture every KPI card with its exact displayed "
    "value, every chart with its title and the fields/measures it appears to show, and "
    "every slicer at the top/side WITH the value currently selected in it (read the "
    "text inside the slicer box, e.g. 'FY2020' or 'All')."
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
