"""BI TestPilot AI — Streamlit application entry point.

Run with::

    streamlit run app.py

This module is intentionally thin: it configures the page, injects shared
styling, builds the sidebar navigation and dispatches to a page renderer. All
real work lives in the service and storage layers behind :class:`AppContext`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is importable when Streamlit launches app.py directly.
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st  # noqa: E402

from src.core.constants import APP_NAME  # noqa: E402
from src.ui import theme  # noqa: E402
from src.ui.pages import (  # noqa: E402
    analysis,
    data_validation,
    datasource,
    history,
    home,
    project_manager,
    reports,
    settings,
    test_cases,
    upload,
)
from src.ui.state import get_active_project, get_context  # noqa: E402

# --- navigation registry ---------------------------------------------------
# Each entry: label -> (icon, render_fn). Placeholder pages are swapped for real
# implementations as each build step ships.
NAV = {
    "Home": ("🏠", home.render),
    "Project Manager": ("📁", project_manager.render),
    "Upload": ("⬆️", upload.render),
    "Datasource": ("🔌", datasource.render),
    "Analysis": ("⚙️", analysis.render),
    "Data Validation": ("🧮", data_validation.render),
    "Reports": ("📊", reports.render),
    "Test Cases": ("✅", test_cases.render),
    "Settings": ("🛠️", settings.render),
    "History": ("🕑", history.render),
}


def _render_sidebar() -> str:
    with st.sidebar:
        st.markdown(f"### 🧭 {APP_NAME}")
        st.caption("QA Automation for BI Dashboards")
        st.divider()

        active = get_active_project()
        if active:
            st.success(f"**Active project**\n\n{active.name}")
            st.caption(f"{active.bi_platform} · {active.status}")
        else:
            st.info("No active project selected.")
        st.divider()

        selection = st.radio(
            "Navigation",
            list(NAV.keys()),
            label_visibility="collapsed",
            format_func=lambda k: f"{NAV[k][0]}  {k}",
        )
        st.divider()
        st.caption("v0.1.0 · Local · No cloud")
    return selection


def main() -> None:
    st.set_page_config(
        page_title=APP_NAME,
        page_icon="🧭",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    theme.inject_css()

    ctx = get_context()
    selection = _render_sidebar()

    _, render_fn = NAV[selection]
    render_fn(ctx)


if __name__ == "__main__":
    main()
