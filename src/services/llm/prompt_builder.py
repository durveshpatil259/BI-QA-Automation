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
