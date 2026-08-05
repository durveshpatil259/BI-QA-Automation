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
    "You are a Business Intelligence data-validation architect. You are given a list "
    "of dashboard KPIs (with their displayed values) and the datasource SCHEMA "
    "(tables, columns, primary/foreign keys). For EACH KPI, map it to the datasource "
    "and generate ONE read-only SQL query that computes the KPI's value from the "
    "database.\n\n"
    "Hard rules:\n"
    "- Use ONLY tables/columns present in the provided schema. Never invent names.\n"
    "- Each query MUST be a single read-only SELECT that returns ONE scalar value "
    "(the metric). Do NOT use INSERT/UPDATE/DELETE/DDL, and no multiple statements.\n"
    "- Target dialect: {dialect}. For percentages, compute the percentage number so "
    "it is comparable to the displayed value.\n"
    "- You do NOT execute SQL. Python will run it and compare the result.\n\n"
    "Respond with STRICT JSON (no markdown/fences): an object with key "
    '"validation_plan" whose value is an array of objects with keys:\n'
    '  "kpi_name": string (must match one of the given KPI names)\n'
    '  "table": string    (schema.table used)\n'
    '  "column": string   (primary measured column)\n'
    '  "aggregation": string (SUM/AVG/COUNT/DISTINCTCOUNT/…)\n'
    '  "business_meaning": string\n'
    '  "filters": [string] (WHERE conditions applied, if any)\n'
    '  "generated_sql": string (the single read-only SELECT)\n'
    '  "confidence": number between 0 and 1 (mapping confidence)\n'
)


def build_plan_user_prompt(kpis: list[tuple[str, str]], schema_text: str, dialect: str) -> str:
    """kpis: list of (name, displayed_value). schema_text: DbSchema.compact_text()."""
    lines = [f"DATASOURCE DIALECT: {dialect}", "", "KPIs TO MAP:"]
    for name, value in kpis:
        lines.append(f"  - {name}" + (f"  (displayed: {value})" if value else ""))
    lines += ["", "DATASOURCE SCHEMA:", schema_text or "(no schema provided)", ""]
    lines.append(
        "Produce the STRICT JSON validation_plan now — one item per KPI, each with a "
        "single read-only SELECT that returns the KPI's scalar value."
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
    '  "filters": [string]  — slicer/filter names or selected values\n'
    '  "visible_text": string  — other notable on-screen text/titles\n'
)

VISION_USER_PROMPT = (
    "Analyse the attached dashboard screenshot(s) and return the STRICT JSON object "
    "described in the system message. Capture every KPI card with its exact displayed "
    "value, and every chart with its title and the fields/measures it appears to show."
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
