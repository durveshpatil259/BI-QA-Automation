"""Test Cases page.

Generates enterprise-format test cases (LLM-authored, deterministically
auto-populated with Actual/Status/Remarks) and displays them in the standard
QA columns, with filtering and CSV export.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.core.constants import TestCaseKind, TestStatus
from src.core.exceptions import BITestPilotError
from src.domain.models import TestCase
from src.ui import theme
from src.ui.state import AppContext, get_active_project

_COLUMNS = [
    "Test Case ID", "Kind", "Module", "Test Scenario", "Test Steps", "Test Data",
    "Expected Result", "Actual Result", "Status", "Priority", "Remarks",
]


def _to_dataframe(cases: list[TestCase]) -> pd.DataFrame:
    rows = [{
        "Test Case ID": c.test_case_id,
        "Kind": str(c.kind),
        "Module": c.module,
        "Test Scenario": c.test_scenario,
        "Test Steps": c.test_steps,
        "Test Data": c.test_data,
        "Expected Result": c.expected_result,
        "Actual Result": c.actual_result,
        "Status": str(c.status),
        "Priority": str(c.priority),
        "Remarks": c.remarks,
    } for c in cases]
    return pd.DataFrame(rows, columns=_COLUMNS)


def _summary(cases: list[TestCase]) -> None:
    total = len(cases)
    passed = sum(1 for c in cases if c.status == TestStatus.PASS)
    failed = sum(1 for c in cases if c.status == TestStatus.FAIL)
    not_exec = sum(1 for c in cases if c.status == TestStatus.NOT_EXECUTED)
    c = st.columns(4)
    c[0].metric("Total", total)
    c[1].metric("Pass", passed)
    c[2].metric("Fail", failed)
    c[3].metric("Manual / Not executed", not_exec)


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("Test Cases")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"Test Cases · {project.name}")

    context = ctx.analysis_service.load_context(project)
    settings = ctx.llm_service.load_settings(project)

    if context is None:
        st.warning(
            "No Analysis Context found. Go to **Analysis** and run Step 3 "
            "(Comparison & Validation) first."
        )
        return

    st.caption(
        "Test cases are authored by the LLM from the deterministic context, then "
        "**Actual Result / Status / Remarks are auto-populated by Python** from the "
        "validation findings — the verdict is evidence-based, not model opinion."
    )

    existing = ctx.test_case_service.load(project)
    label = "🔁 Regenerate test cases" if existing else "🧪 Generate test cases"
    disabled = not settings.is_configured
    if st.button(label, type="primary", disabled=disabled):
        with st.spinner(f"Generating test cases via {settings.provider}…"):
            try:
                existing = ctx.test_case_service.generate(project, context, settings)
                st.success(f"Generated {len(existing)} test case(s).")
            except BITestPilotError as exc:
                st.error(str(exc))
    if disabled:
        st.info(
            "Configure an LLM provider and API key on the **Analysis** page (Step 4) "
            "to enable generation."
        )

    if not existing:
        st.caption("No test cases yet.")
        return

    _summary(existing)

    # Filters
    f1, f2 = st.columns(2)
    kinds = f1.multiselect("Kind", [k.value for k in TestCaseKind], default=[])
    statuses = f2.multiselect("Status", [s.value for s in TestStatus], default=[])
    filtered = existing
    if kinds:
        filtered = [c for c in filtered if c.kind.value in kinds]
    if statuses:
        filtered = [c for c in filtered if c.status.value in statuses]

    df = _to_dataframe(filtered)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download CSV",
        data=_to_dataframe(existing).to_csv(index=False).encode("utf-8"),
        file_name=f"{project.name}_test_cases.csv",
        mime="text/csv",
    )
