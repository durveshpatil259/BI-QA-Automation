"""Analysis page.

Home of the deterministic → AI pipeline. This module ships the **metadata
extraction** step (Module 5); later steps (screenshot processing, comparison,
validation, LLM reasoning, test-case generation) extend this same page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.constants import AnalysisMode, LLMProvider
from src.core.exceptions import BITestPilotError
from src.domain.models import (
    AIReasoning,
    AnalysisContext,
    DashboardMetadata,
    LLMSettings,
    VisualAnalysis,
)
from src.services.llm.factory import supported_providers
from src.ui import theme
from src.ui.state import AppContext, get_active_project


def _needs_metadata(mode: AnalysisMode | None) -> bool:
    return mode in (AnalysisMode.METADATA, AnalysisMode.COMPLETE)


def _needs_visual(mode: AnalysisMode | None) -> bool:
    return mode in (AnalysisMode.VISUAL, AnalysisMode.COMPLETE)


def _render_summary(metadata: DashboardMetadata) -> None:
    counts = metadata.summary_counts()
    st.caption(
        f"Model: **{metadata.model_name or '—'}** · source: {metadata.source_file}"
    )
    c = st.columns(5)
    c[0].metric("Tables", counts["tables"])
    c[1].metric("Columns", counts["columns"])
    c[2].metric("Measures", counts["measures"])
    c[3].metric("Relationships", counts["relationships"])
    c[4].metric("Pages", counts["pages"])
    c2 = st.columns(5)
    c2[0].metric("Visuals", counts["visuals"])
    c2[1].metric("Bookmarks", counts["bookmarks"])
    c2[2].metric("Calc. columns", counts["calculated_columns"])
    c2[3].metric("Calc. tables", counts["calculated_tables"])
    c2[4].metric("Report filters", len(metadata.report_level_filters))

    if metadata.extraction_warnings:
        with st.expander(f"⚠️ Extraction warnings ({len(metadata.extraction_warnings)})"):
            for w in metadata.extraction_warnings:
                st.warning(w)


def _render_tables(metadata: DashboardMetadata) -> None:
    if not metadata.tables:
        st.info("No tables extracted.")
        return
    rows = [{
        "Table": t.name,
        "Columns": len(t.columns),
        "Measures": len(t.measures),
        "Calculated": "Yes" if t.is_calculated else "",
        "Hidden": "Yes" if t.is_hidden else "",
    } for t in metadata.tables]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    table_names = [t.name for t in metadata.tables]
    chosen = st.selectbox("Inspect table", table_names)
    table = next(t for t in metadata.tables if t.name == chosen)
    if table.columns:
        st.markdown("**Columns**")
        st.dataframe(pd.DataFrame([{
            "Column": c.name, "Type": c.data_type,
            "Calculated": "Yes" if c.is_calculated else "",
            "Hidden": "Yes" if c.is_hidden else "",
            "DAX": c.dax_expression,
        } for c in table.columns]), use_container_width=True, hide_index=True)
    if table.measures:
        st.markdown("**Measures**")
        st.dataframe(pd.DataFrame([{
            "Measure": m.name, "Folder": m.display_folder,
            "Format": m.format_string, "DAX": m.dax_expression,
        } for m in table.measures]), use_container_width=True, hide_index=True)
    if table.is_calculated and table.dax_expression:
        st.markdown("**Calculated table DAX**")
        st.code(table.dax_expression)


def _render_relationships(metadata: DashboardMetadata) -> None:
    if not metadata.relationships:
        st.info("No relationships extracted.")
        return
    st.dataframe(pd.DataFrame([{
        "From": f"{r.from_table}[{r.from_column}]",
        "To": f"{r.to_table}[{r.to_column}]",
        "Cardinality": r.cardinality,
        "Cross-filter": r.cross_filter_direction,
        "Active": "Yes" if r.is_active else "No",
    } for r in metadata.relationships]), use_container_width=True, hide_index=True)


def _render_report(metadata: DashboardMetadata) -> None:
    if not metadata.pages:
        st.info("No report pages extracted.")
        return
    for page in metadata.pages:
        with st.expander(f"📄 {page.display_name} · {len(page.visuals)} visual(s)"):
            if page.visuals:
                st.dataframe(pd.DataFrame([{
                    "Visual": v.title or v.id, "Type": v.visual_type,
                    "Fields": ", ".join(v.fields),
                } for v in page.visuals]), use_container_width=True, hide_index=True)
            else:
                st.caption("No visuals on this page.")
    if metadata.bookmarks:
        st.markdown("**Bookmarks**")
        st.dataframe(pd.DataFrame([{
            "Bookmark": b.display_name, "Page": b.page,
        } for b in metadata.bookmarks]), use_container_width=True, hide_index=True)


def _render_metadata(metadata: DashboardMetadata) -> None:
    _render_summary(metadata)
    tab1, tab2, tab3 = st.tabs(["Model tables", "Relationships", "Report layout"])
    with tab1:
        _render_tables(metadata)
    with tab2:
        _render_relationships(metadata)
    with tab3:
        _render_report(metadata)


def _render_visual_analysis(ctx: AppContext, project, visual: VisualAnalysis) -> None:
    st.caption(f"{visual.total_screenshots} screenshot(s) processed.")
    if visual.warnings:
        with st.expander(f"⚠️ Processing notes ({len(visual.warnings)})"):
            for w in visual.warnings:
                st.warning(w)

    paths = ctx.projects.paths_for(project)
    for shot in visual.screenshots:
        with st.container(border=True):
            c1, c2 = st.columns([1, 2])
            img_path = paths.screenshots_dir / shot.file_name
            if img_path.exists():
                theme.show_image(c1, str(img_path))
            with c2:
                st.markdown(f"**{shot.file_name}**")
                dims = f"{shot.width}×{shot.height}" if shot.width else "unknown"
                from src.services.upload_service import AssetInfo

                size = AssetInfo(shot.file_name, shot.size_bytes or 0).size_human
                st.caption(f"{shot.format or '—'} · {dims} · {size}")
                if shot.notes:
                    st.warning(shot.notes)
                if shot.detected_text:
                    with st.expander("Detected text (OCR)"):
                        st.text(shot.detected_text)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def _metadata_step(ctx: AppContext, project) -> None:
    theme.section("Step 1 — Metadata extraction")
    if not _needs_metadata(project.analysis_mode):
        st.caption("Visual-only mode: no dashboard file, so metadata extraction is skipped.")
        return

    if not ctx.metadata_service.has_dashboard_file(project):
        st.warning("No dashboard file found. Upload one on the Upload page.")
        return

    existing = ctx.metadata_service.load(project)
    label = "🔁 Re-extract metadata" if existing else "▶️ Extract metadata"
    if st.button(label, type="primary", key="btn_extract_meta"):
        with st.spinner("Parsing dashboard file…"):
            try:
                existing = ctx.metadata_service.extract(project)
                st.success("Metadata extracted and saved.")
            except BITestPilotError as exc:
                st.error(str(exc))
                existing = None

    if existing:
        _render_metadata(existing)
    else:
        st.caption("No metadata yet. Click **Extract metadata** to parse the dashboard.")


def _visual_step(ctx: AppContext, project) -> None:
    if not _needs_visual(project.analysis_mode):
        return
    theme.section("Step 2 — Screenshot processing")

    if not ctx.screenshot_service.has_screenshots(project):
        st.warning("No screenshots found. Upload some on the Upload page.")
        return

    if not ctx.screenshot_service.ocr_available():
        st.caption(
            "OCR engine not detected — image facts (size/format/dimensions) will be "
            "extracted, but on-screen text will not. Install Tesseract + pytesseract "
            "to enable text extraction."
        )

    existing = ctx.screenshot_service.load(project)
    label = "🔁 Re-process screenshots" if existing else "▶️ Process screenshots"
    if st.button(label, type="primary", key="btn_process_shots"):
        with st.spinner("Reading screenshots…"):
            try:
                existing = ctx.screenshot_service.process(project)
                st.success("Screenshots processed and saved.")
            except BITestPilotError as exc:
                st.error(str(exc))
                existing = None

    if existing:
        _render_visual_analysis(ctx, project, existing)
    else:
        st.caption("No visual analysis yet. Click **Process screenshots** to begin.")


def _render_context(context: AnalysisContext) -> None:
    summary = context.validation_summary()
    c = st.columns(4)
    c[0].metric("Checks", summary["total"])
    c[1].metric("Passed", summary["passed"])
    c[2].metric("Failed", summary["failed"])
    c[3].metric("Critical", summary["critical"])

    tabs = st.tabs(["Validation findings", "Datasource comparison", "Data facts"])

    with tabs[0]:
        failed = [v for v in context.validations if not v.passed]
        rows = [{
            "Rule": v.rule_id, "Severity": str(v.severity), "Category": v.category,
            "Finding": v.title, "Entity": v.entity, "Detail": v.description,
        } for v in (failed or context.validations)]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption(
                f"Showing {'failing' if failed else 'all (all passed)'} checks "
                f"({len(rows)} of {summary['total']})."
            )
        else:
            st.info("No validation findings.")

    with tabs[1]:
        if context.comparisons:
            st.dataframe(pd.DataFrame([{
                "Check": c.label, "Dashboard": c.dashboard_value,
                "Datasource": c.datasource_value,
                "Match": "✓" if c.matched else "✗",
                "Severity": str(c.severity), "Difference": c.difference,
            } for c in context.comparisons]), use_container_width=True, hide_index=True)
        else:
            # Give a precise reason rather than the generic hint.
            has_ds = context.datasource_type is not None
            model_tables = len(context.metadata.tables) if context.metadata else 0
            if has_ds and model_tables == 0:
                st.warning(
                    "A datasource is configured, but the dashboard model has **0 tables** "
                    "to compare. This is a native `.pbix` with a binary data model — export "
                    "it to **.pbit** (Power BI Desktop → File → Export → Power BI template) "
                    "and re-extract so the model tables appear. Also ensure your SQL table "
                    "names match the model table names."
                )
            elif not has_ds:
                st.info("No datasource configured. Set one on the **Datasource** page to compare.")
            else:
                st.info(
                    "No matching tables found between the dashboard model and the datasource. "
                    "Rename SQL tables to match the model table names, then rebuild."
                )

    with tabs[2]:
        if context.data_results:
            st.dataframe(pd.DataFrame([{
                "Fact": d.label, "Query": d.query, "Value": d.scalar_value,
            } for d in context.data_results]), use_container_width=True, hide_index=True)
        else:
            st.caption("No datasource facts gathered.")


def _context_step(ctx: AppContext, project) -> None:
    theme.section("Step 3 — Comparison & Validation")
    st.caption(
        "Deterministic engine: compares the dashboard model against the datasource, "
        "runs validation rules, and assembles the single Analysis Context that the "
        "LLM will later reason over. No AI is involved in this step."
    )

    existing = ctx.analysis_service.load_context(project)
    label = "🔁 Rebuild analysis context" if existing else "▶️ Build analysis context"
    if st.button(label, type="primary", key="btn_build_context"):
        with st.spinner("Comparing and validating…"):
            try:
                existing = ctx.analysis_service.build_context(project)
                st.success("Analysis context built and saved.")
            except BITestPilotError as exc:
                st.error(str(exc))
                existing = None

    if existing:
        _render_context(existing)
    else:
        st.caption("No analysis context yet. Run the deterministic engine above.")


def _llm_config_expander(ctx: AppContext, project) -> LLMSettings:
    settings = ctx.llm_service.load_settings(project)
    providers = supported_providers()
    models_key = f"_llm_models_{project.id}"
    with st.expander("⚙️ LLM configuration", expanded=not settings.is_configured):
        prov_index = providers.index(settings.provider.value) if settings.provider.value in providers else 0
        with st.form("llm_settings_form"):
            provider = st.selectbox("Provider", providers, index=prov_index)
            api_key = st.text_input("API key", value=settings.api_key, type="password")
            model = st.text_input(
                "Model (blank = provider default)", value=settings.model,
                placeholder="e.g. grok-3, llama-3.3-70b-versatile, gemini-2.0-flash",
            )
            base_url = st.text_input(
                "Base URL (optional — set for a free/custom OpenAI-compatible provider)",
                value=settings.base_url,
                placeholder="e.g. https://api.groq.com/openai/v1",
            )
            st.caption(
                "💡 Free options (OpenAI-compatible): **Groq** — key: console.groq.com/keys, "
                "URL: https://api.groq.com/openai/v1, model: llama-3.3-70b-versatile · "
                "**Gemini** — key: aistudio.google.com/app/apikey, "
                "URL: https://generativelanguage.googleapis.com/v1beta/openai/, model: gemini-2.0-flash"
            )
            c1, c2 = st.columns(2)
            temperature = c1.slider("Temperature", 0.0, 1.0, float(settings.temperature), 0.05)
            max_tokens = c2.number_input(
                "Max tokens (output)", 256, 32000, min(int(settings.max_tokens), 2048), step=256
            )
            st.caption(
                "⚠️ Free tiers have per-minute token limits (e.g. Groq = 12,000 TPM, "
                "counting prompt **+** max tokens). Keep **Max tokens ≤ 2048** to stay under it."
            )
            if st.form_submit_button("💾 Save LLM settings", type="primary"):
                settings = ctx.llm_service.save_settings(project, LLMSettings(
                    provider=LLMProvider.from_value(provider), api_key=api_key.strip(),
                    model=model.strip(), base_url=base_url.strip(),
                    temperature=temperature, max_tokens=int(max_tokens),
                ))
                st.success("LLM settings saved.")

        # Model discovery — resolves "model not found" errors by listing exactly
        # what the saved API key supports. Uses saved settings, so save the key first.
        if settings.is_configured:
            if st.button("🔍 Fetch available models", key="btn_fetch_models"):
                try:
                    st.session_state[models_key] = ctx.llm_service.list_models(settings)
                except BITestPilotError as exc:
                    st.error(str(exc))
            models = st.session_state.get(models_key) or []
            if models:
                st.caption(f"{len(models)} model(s) available to your key:")
                default_idx = models.index(settings.model) if settings.model in models else 0
                chosen = st.selectbox("Pick a model", models, index=default_idx, key="pick_model")
                if st.button("Use this model", key="btn_use_model"):
                    settings.model = chosen
                    settings = ctx.llm_service.save_settings(project, settings)
                    st.success(f"Model set to '{chosen}'.")
                    st.rerun()

        if settings.is_configured:
            st.caption(f"Configured: **{settings.provider}** · {settings.model or 'default model'}")
        else:
            st.caption("Add an API key to enable AI reasoning. Users bring their own keys.")
    return settings


def _render_reasoning(reasoning: AIReasoning) -> None:
    st.caption(f"Generated by **{reasoning.provider}** ({reasoning.model}) · {reasoning.generated_at:%Y-%m-%d %H:%M}")
    st.markdown("#### Executive summary")
    st.write(reasoning.executive_summary or "_(none)_")
    st.markdown("#### Root cause analysis")
    st.write(reasoning.root_cause_analysis or "_(none)_")
    st.markdown("#### Recommendations")
    if reasoning.recommendations:
        for i, rec in enumerate(reasoning.recommendations, 1):
            st.markdown(f"{i}. {rec}")
    else:
        st.write("_(none)_")
    with st.expander("Raw model response"):
        st.code(reasoning.raw_response or "", language="json")


def _ai_step(ctx: AppContext, project) -> None:
    theme.section("Step 4 — AI Reasoning")
    st.caption(
        "The LLM reasons ONLY over the deterministic Analysis Context assembled in "
        "Step 3 — it produces the executive summary, root-cause analysis and "
        "recommendations. It never reads data, runs SQL or parses files."
    )

    context = ctx.analysis_service.load_context(project)
    if context is None:
        st.warning("Build the Analysis Context in Step 3 first.")
        return

    settings = _llm_config_expander(ctx, project)

    existing = ctx.llm_service.load_reasoning(project)
    label = "🔁 Regenerate AI reasoning" if existing else "🤖 Generate AI reasoning"
    disabled = not settings.is_configured
    if st.button(label, type="primary", key="btn_ai_reason", disabled=disabled):
        with st.spinner(f"Calling {settings.provider}…"):
            try:
                existing = ctx.llm_service.generate(project, context, settings)
                st.success("AI reasoning generated and saved.")
            except BITestPilotError as exc:
                st.error(str(exc))
    if disabled:
        st.info("Configure an LLM provider and API key above to enable this step.")

    if existing:
        _render_reasoning(existing)


def _run_full_analysis(ctx: AppContext, project) -> None:
    """One-click orchestration of the whole pipeline.

    Runs every applicable deterministic step, then (if an LLM is configured) the
    AI reasoning and test-case generation, and finally builds the report — so
    the user can simply upload assets and click one button.
    """
    mode = project.analysis_mode
    steps_done: list[str] = []
    try:
        with st.status("Running full analysis…", expanded=True) as status:
            if _needs_metadata(mode) and ctx.metadata_service.has_dashboard_file(project):
                st.write("① Extracting dashboard metadata…")
                ctx.metadata_service.extract(project)
                steps_done.append("metadata")

            if _needs_visual(mode) and ctx.screenshot_service.has_screenshots(project):
                st.write("② Processing screenshots…")
                ctx.screenshot_service.process(project)
                steps_done.append("screenshots")

            st.write("③ Comparing & validating (building analysis context)…")
            context = ctx.analysis_service.build_context(project)
            steps_done.append("context")

            # AI steps are best-effort: a provider/rate-limit error must not
            # discard the deterministic work — we still build the report.
            ai_errors: list[str] = []
            settings = ctx.llm_service.load_settings(project)
            if settings.is_configured:
                st.write(f"④ Generating AI reasoning via {settings.provider}…")
                try:
                    ctx.llm_service.generate(project, context, settings)
                    steps_done.append("ai_reasoning")
                except BITestPilotError as exc:
                    ai_errors.append(f"AI reasoning: {exc}")
                    st.warning(f"AI reasoning skipped — {exc}")

                st.write("⑤ Generating unit & QA test cases…")
                try:
                    ctx.test_case_service.generate(project, context, settings)
                    steps_done.append("test_cases")
                except BITestPilotError as exc:
                    ai_errors.append(f"Test cases: {exc}")
                    st.warning(f"Test-case generation skipped — {exc}")
            else:
                st.write("④ Skipping AI steps — no LLM configured (see Step 4 below).")

            st.write("⑥ Building validation report…")
            ctx.report_service.build_report(project)
            steps_done.append("report")

            status.update(label="Full analysis complete.", state="complete")

        ai_ok = {"ai_reasoning", "test_cases"} & set(steps_done)
        if ai_errors:
            st.warning(
                "Deterministic analysis and report are ready, but some AI steps did not "
                "complete:\n\n- " + "\n- ".join(ai_errors) +
                "\n\nOn free tiers this is usually a per-minute token limit — lower "
                "**Max tokens** to 1024, wait a minute, then retry the AI steps below."
            )
        elif ai_ok:
            st.success("Done — metadata, validation, AI reasoning, test cases and report are ready.")
        else:
            st.success(
                "Deterministic analysis and report are ready. Configure an LLM in "
                "Step 4 to add AI reasoning and test cases, then run again."
            )
    except BITestPilotError as exc:
        st.error(f"Analysis stopped: {exc}")


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("Analysis")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"Analysis · {project.name}")

    mode = project.analysis_mode
    if mode is None:
        st.warning("Upload a dashboard file and/or screenshots first (Upload page).")
        return
    st.info(f"Analysis mode: **{mode}**")

    # One-click path — does everything. The individual steps below remain
    # available for granular control / inspection.
    if st.button("🚀 Run full analysis", type="primary", key="btn_run_full"):
        _run_full_analysis(ctx, project)

    st.divider()
    theme.section("Run steps individually")
    st.caption("Granular control and inspection — the same steps the button above runs.")
    _metadata_step(ctx, project)
    _visual_step(ctx, project)
    _context_step(ctx, project)
    _ai_step(ctx, project)
