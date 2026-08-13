"""Report download endpoints — HTML, PDF and Excel."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from src.api.deps import Container, container

router = APIRouter(prefix="/api/projects", tags=["reports"])


def _latest(c: Container, project_id: str):
    project = c.project_service.get_project(project_id)
    report = c.report_service.latest(project)
    if report is None:
        raise HTTPException(404, "No report yet — run analysis first.")
    return project, report


@router.get("/{project_id}/report.html", response_class=HTMLResponse)
def report_html(project_id: str, c: Container = Depends(container)):
    _, report = _latest(c, project_id)
    return HTMLResponse(c.report_service.to_html(report))


@router.get("/{project_id}/report.pdf")
def report_pdf(project_id: str, c: Container = Depends(container)):
    """Render the HTML report to PDF (WeasyPrint, falling back to xhtml2pdf)."""
    from src.services.reporting.pdf_renderer import PdfRenderError, render_pdf

    project, report = _latest(c, project_id)
    try:
        pdf, engine = render_pdf(c.report_service.to_html(report))
    except PdfRenderError as exc:
        raise HTTPException(501, str(exc))

    filename = f"{project.name}_{report.id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-PDF-Engine": engine,
        },
    )


@router.get("/{project_id}/report.xlsx")
def report_xlsx(project_id: str, c: Container = Depends(container)):
    """Multi-sheet workbook: Summary, Data Validation, Test Cases, Findings."""
    import pandas as pd

    project, report = _latest(c, project_id)
    vs = report.validation_summary or {}
    dvs = report.data_validation_summary or {}

    summary = pd.DataFrame([
        {"Metric": "Project", "Value": report.project_name},
        {"Metric": "Platform", "Value": str(report.platform)},
        {"Metric": "Generated", "Value": report.created_at.strftime("%Y-%m-%d %H:%M")},
        {"Metric": "Checks", "Value": vs.get("total", 0)},
        {"Metric": "Checks passed", "Value": vs.get("passed", 0)},
        {"Metric": "Checks failed", "Value": vs.get("failed", 0)},
        {"Metric": "Data validations", "Value": dvs.get("total", 0)},
        {"Metric": "Data validations passed", "Value": dvs.get("passed", 0)},
        {"Metric": "Data validations failed", "Value": dvs.get("failed", 0)},
        {"Metric": "Test cases", "Value": len(report.test_cases)},
    ])

    validations = pd.DataFrame([{
        "Test ID": r.test_id, "Scenario": r.scenario, "KPI": r.kpi_name,
        "Dashboard Value": r.dashboard_value,
        # Whichever proof the datasource produced: SQL for a database, or
        # sheet/operation/filters for a spreadsheet.
        "How it was calculated": getattr(r, "source_evidence", "")
                                 or (r.generated_sql or "").strip(),
        "Database Value": r.database_value, "Difference": r.difference,
        "Match": r.match_type, "Execution Time (ms)": r.execution_time_ms,
        "Status": str(r.status), "Reason": r.reason,
        "AI Recommendation": r.recommendation,
    } for r in report.sql_validations])

    tests = pd.DataFrame([{
        "Test Case ID": t.test_case_id, "Kind": str(t.kind), "Module": t.module,
        "Test Scenario": t.test_scenario, "Test Steps": t.test_steps,
        "Test Data": t.test_data, "Expected Result": t.expected_result,
        "Actual Result": t.actual_result, "Status": str(t.status),
        "Priority": str(t.priority), "Remarks": t.remarks,
        "Generated SQL": t.generated_sql,
    } for t in report.test_cases])

    findings = pd.DataFrame([{
        "Rule": f.rule_id, "Severity": str(f.severity), "Category": f.category,
        "Finding": f.title, "Entity": f.entity, "Detail": f.description,
        "Passed": f.passed,
    } for f in report.findings])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        for name, frame in (
            ("Data Validation", validations),
            ("Test Cases", tests),
            ("Findings", findings),
        ):
            # openpyxl rejects a sheet with zero columns.
            (frame if not frame.empty else pd.DataFrame({"(none)": []})).to_excel(
                writer, sheet_name=name, index=False
            )

    filename = f"{project.name}_{report.id}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
