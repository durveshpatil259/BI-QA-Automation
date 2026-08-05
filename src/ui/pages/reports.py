"""Reports page — generate and export the final analysis report."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.exceptions import BITestPilotError
from src.domain.models import AnalysisReport
from src.ui import theme
from src.ui.state import AppContext, get_active_project


def _render_report(ctx: AppContext, report: AnalysisReport) -> None:
    vs = report.validation_summary or {}
    c = st.columns(5)
    c[0].metric("Checks", vs.get("total", 0))
    c[1].metric("Passed", vs.get("passed", 0))
    c[2].metric("Failed", vs.get("failed", 0))
    c[3].metric("Critical", vs.get("critical", 0))
    c[4].metric("Test cases", len(report.test_cases))

    st.caption(
        f"Report **{report.id}** · {report.created_at:%Y-%m-%d %H:%M} · "
        f"{report.platform} · {report.analysis_mode}"
        + (f" · AI: {report.llm_provider} ({report.llm_model})" if report.llm_provider else "")
    )

    # Exports
    html = ctx.report_service.to_html(report)
    e1, e2 = st.columns(2)
    e1.download_button(
        "⬇️ Download HTML report", data=html.encode("utf-8"),
        file_name=f"{report.project_name}_{report.id}.html", mime="text/html",
        use_container_width=True,
    )
    if report.test_cases:
        rows = [{
            "Test Case ID": t.test_case_id, "Kind": str(t.kind), "Module": t.module,
            "Test Scenario": t.test_scenario, "Test Steps": t.test_steps,
            "Test Data": t.test_data, "Expected Result": t.expected_result,
            "Actual Result": t.actual_result, "Status": str(t.status),
            "Priority": str(t.priority), "Remarks": t.remarks,
        } for t in report.test_cases]
        e2.download_button(
            "⬇️ Download test cases (CSV)",
            data=pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
            file_name=f"{report.project_name}_test_cases.csv", mime="text/csv",
            use_container_width=True,
        )

    st.markdown("#### Executive summary")
    st.write(report.executive_summary or "_Not generated (run Analysis Step 4)._")
    st.markdown("#### Root cause analysis")
    st.write(report.root_cause_analysis or "_Not generated._")
    st.markdown("#### Recommendations")
    if report.recommendations:
        for i, r in enumerate(report.recommendations, 1):
            st.markdown(f"{i}. {r}")
    else:
        st.write("_None._")

    with st.expander("Preview HTML report"):
        st.components.v1.html(html, height=600, scrolling=True)


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("Reports")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"Reports · {project.name}")
    st.caption(
        "A report combines the deterministic findings, AI narrative and generated "
        "test cases into one exportable document."
    )

    latest = ctx.report_service.latest(project)
    label = "🔁 Generate new report" if latest else "📄 Generate report"
    if st.button(label, type="primary"):
        with st.spinner("Assembling report…"):
            try:
                latest = ctx.report_service.build_report(project)
                st.success(f"Report {latest.id} generated.")
            except BITestPilotError as exc:
                st.error(str(exc))

    if latest:
        _render_report(ctx, latest)
    else:
        st.caption("No report yet. Complete the Analysis steps, then generate a report.")
