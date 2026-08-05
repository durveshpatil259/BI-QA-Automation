"""Home / overview dashboard page."""

from __future__ import annotations

import streamlit as st

from src.core.constants import AnalysisStatus
from src.ui import theme
from src.ui.state import AppContext


def render(ctx: AppContext) -> None:
    theme.app_header()

    projects = ctx.projects.list_projects()
    completed = sum(1 for p in projects if p.status == AnalysisStatus.COMPLETED)
    running = sum(1 for p in projects if p.status == AnalysisStatus.RUNNING)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Projects", len(projects))
    c2.metric("Completed", completed)
    c3.metric("Running", running)
    c4.metric("Default LLM", ctx.config.default_llm_provider)

    theme.section("Getting started")
    st.markdown(
        "1. Go to **Project Manager** and create a project (choose the BI platform).\n"
        "2. **Upload** the dashboard file and/or screenshots — the analysis mode is "
        "determined automatically.\n"
        "3. Configure a **Datasource** (SQL Server or Excel).\n"
        "4. Run **Analysis** — Python does all deterministic work, then the selected "
        "LLM generates test cases, summaries and root-cause analysis.\n"
        "5. Review **Reports** and export **Test Cases**."
    )

    theme.section("Recent projects")
    if not projects:
        st.info("No projects yet. Create your first project from **Project Manager**.")
        return

    for row in _chunk(projects[:6], 3):
        cols = st.columns(3)
        for col, project in zip(cols, row):
            with col:
                st.markdown(
                    f'<div class="bt-card">'
                    f"<h4>{project.name or 'Untitled'}</h4>"
                    f'<p>{project.bi_platform} &nbsp; '
                    f"{theme.status_badge(project.status)}</p>"
                    f'<p>{(project.description or "—")[:80]}</p>'
                    f"</div>",
                    unsafe_allow_html=True,
                )


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]
