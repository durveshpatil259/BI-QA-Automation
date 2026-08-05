"""History page — past analysis reports for the active project."""

from __future__ import annotations

import streamlit as st

from src.ui import theme
from src.ui.state import AppContext, get_active_project


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("History")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"History · {project.name}")

    reports = ctx.report_service.list_reports(project)
    if not reports:
        st.info("No reports yet. Generate one on the **Reports** page.")
        return

    st.caption(f"{len(reports)} report(s), newest first.")
    for report in reports:
        vs = report.validation_summary or {}
        title = (
            f"{report.created_at:%Y-%m-%d %H:%M} · {report.id} · "
            f"{vs.get('failed', 0)} failed / {vs.get('total', 0)} checks · "
            f"{len(report.test_cases)} test cases"
        )
        with st.expander(title):
            st.caption(
                f"{report.platform} · {report.analysis_mode} · {report.status}"
                + (f" · AI: {report.llm_provider}" if report.llm_provider else "")
            )
            if report.executive_summary:
                st.write(report.executive_summary)
            html = ctx.report_service.to_html(report)
            st.download_button(
                "⬇️ Download HTML", data=html.encode("utf-8"),
                file_name=f"{report.project_name}_{report.id}.html",
                mime="text/html", key=f"dl_{report.id}",
            )
