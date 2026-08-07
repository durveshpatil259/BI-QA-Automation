"""Data Validation page (redesign V7).

Enterprise view of dashboard-vs-database validation: the results grid plus a
row drill-down (dashboard screenshot, generated SQL, execution result, PASS/FAIL
reason and AI recommendation). Python executes and compares; the AI only
explains failures.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.constants import SCREENSHOT_EXTENSIONS, TestStatus
from src.core.exceptions import BITestPilotError
from src.domain.models import DataValidationRun
from src.storage import file_manager as fm
from src.ui import theme
from src.ui.state import AppContext, get_active_project

_GRID_COLUMNS = [
    "Test ID", "Scenario", "Visual", "KPI / Category", "Dashboard Value",
    "Generated SQL", "Database Value", "Difference", "Match",
    "Execution Time (ms)", "Status",
]


def _grid(run: DataValidationRun) -> pd.DataFrame:
    return pd.DataFrame([{
        "Test ID": r.test_id, "Scenario": r.scenario,
        "Visual": r.visual_title or "KPI card", "KPI / Category": r.kpi_name,
        "Dashboard Value": r.dashboard_value, "Generated SQL": r.generated_sql,
        "Database Value": r.database_value, "Difference": r.difference,
        "Match": r.match_type, "Execution Time (ms)": r.execution_time_ms,
        "Status": str(r.status),
    } for r in run.results], columns=_GRID_COLUMNS)


def _status_badge(status: TestStatus) -> str:
    return theme.badge(str(status), theme.test_status_color(status))


def _drilldown(ctx: AppContext, project, run: DataValidationRun) -> None:
    theme.section("Row details")
    options = {
        f"{r.test_id} · {r.kpi_name}"
        + (f" · {r.scenario}" if r.scenario else "")
        + f" · {r.status}": r
        for r in run.results
    }
    choice = st.selectbox("Select a test to inspect", list(options.keys()))
    r = options[choice]

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**Dashboard screenshot**")
        paths = ctx.projects.paths_for(project)
        shots = fm.list_dir(paths.screenshots_dir, SCREENSHOT_EXTENSIONS)
        if shots:
            theme.show_image(st, str(shots[0]))
        else:
            st.caption("No screenshot uploaded for this project.")
    with right:
        st.markdown(
            f"**Status:** {_status_badge(r.status)}", unsafe_allow_html=True
        )
        m = st.columns(2)
        m[0].metric("Dashboard value", r.dashboard_value or "—")
        m[1].metric("Database value", r.database_value or "—")
        m2 = st.columns(2)
        m2[0].metric("Difference", r.difference or "—")
        m2[1].metric(
            "Exec time (ms)",
            r.execution_time_ms if r.execution_time_ms is not None else "—",
        )
        if r.difference_pct is not None:
            st.caption(f"Difference: {r.difference_pct:.3f}% (tolerance {r.tolerance_pct}%)")

    st.markdown("**Generated SQL**")
    st.code(r.generated_sql or "(none)", language="sql")
    st.markdown("**Execution result**")
    st.write(
        f"Execution status: `{r.execution_status or '—'}` · Result value: "
        f"`{r.database_value or '—'}`"
    )
    st.markdown("**Reason for PASS/FAIL**")
    st.write(r.reason or "—")
    st.markdown("**AI recommendation**")
    st.write(r.recommendation or "_Run 'Explain failures with AI' to populate._")


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("Data Validation")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"Data Validation · {project.name}")
    st.caption(
        "Dashboard KPI values validated against the database via generated SQL. "
        "Python executes and compares; the AI only explains failures."
    )

    plan = ctx.validation_plan_service.load(project)
    if not plan or not plan.items:
        st.info(
            "No validation plan yet. On the **Analysis** page: run AI vision (Step 2), "
            "read the DB schema (Datasource), then generate the plan (Step 5)."
        )
        return

    tolerance = st.number_input(
        "Tolerance (%)", min_value=0.0, max_value=50.0, value=1.0, step=0.5
    )
    a, b = st.columns(2)
    if a.button("▶️ Run / re-run SQL validation", type="primary"):
        with st.spinner("Executing SQL and comparing…"):
            try:
                ctx.sql_validation_engine.run(project, tolerance_pct=float(tolerance))
                st.success("Data validation complete.")
            except BITestPilotError as exc:
                st.error(str(exc))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Data validation failed: {exc}")

    run = ctx.sql_validation_engine.load(project)
    if not run or not run.results:
        st.caption("No data validation run yet. Click **Run SQL validation** above.")
        return

    settings = ctx.llm_service.load_settings(project)
    if b.button(
        "🧠 Explain failures with AI",
        disabled=not settings.is_configured
        or not any(r.status == TestStatus.FAIL for r in run.results),
    ):
        with st.spinner("Asking the AI to explain failures…"):
            try:
                run = ctx.sql_validation_engine.explain_failures(project, settings)
                st.success("Explanations added.")
            except BITestPilotError as exc:
                st.error(str(exc))

    s = run.summary()
    c = st.columns(4)
    c[0].metric("Tests", s["total"])
    c[1].metric("Pass", s["passed"])
    c[2].metric("Fail", s["failed"])
    c[3].metric("Errors", s["errors"])

    df = _grid(run)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download CSV", data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{project.name}_data_validation.csv", mime="text/csv",
    )

    _drilldown(ctx, project, run)
