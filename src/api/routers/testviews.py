"""Filtered views over one project's stored results.

Nothing is recomputed and no AI is involved: the validation run and the
generated test cases are already on disk, and these are four ways of reading
them — grouped into suites, the SQL that ran, the model-level checks, and the
mismatches worth investigating.

Scoped to a single project on purpose. A test id is only unique within a run,
and a table mixing two projects' ids invites someone to compare rows that were
never comparable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import Container, container
from src.core.logger import get_logger

_logger = get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

#: Module prefix -> suite. Ordered: the first match wins, so the specific
#: "SQL Validation:" prefix is tested before the generic "KPI:".
_SUITES = (
    ("sql validation:", "SQL Tests"),
    ("kpi:", "KPI Tests"),
    ("chart:", "Visual Tests"),
    ("filters", "Filter Tests"),
    ("slicer", "Filter Tests"),
    ("navigation", "Navigation Tests"),
    ("measure", "Measure / DAX Tests"),
    ("dax", "Measure / DAX Tests"),
    ("relationship", "Data Model Tests"),
    ("dataset", "Data Model Tests"),
    ("model", "Data Model Tests"),
    ("page", "Navigation Tests"),
    ("security", "Data Model Tests"),
)


def _suite_of(case) -> str:
    module = (getattr(case, "module", "") or "").casefold()
    scenario = (getattr(case, "test_scenario", "") or "").casefold()
    if "performance" in module or "performance" in scenario:
        return "Performance Tests"
    for prefix, suite in _SUITES:
        if module.startswith(prefix) or prefix in module:
            return suite
    return "Other Tests"


def _status_of(obj) -> str:
    return str(getattr(obj, "status", "") or "").strip() or "Not Executed"


def _project_or_404(c: Container, project_id: str):
    try:
        return c.project_service.get_project(project_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a 404
        raise HTTPException(404, f"No project '{project_id}'.") from exc


def _classify(row):
    """Delegates to the shared rule so this view and the stored summary agree."""
    from src.services.validation.issue_severity import classify

    return classify(getattr(row, "match_type", ""),
                    getattr(row, "database_value", ""))


@router.get("/projects/{project_id}/tests")
def project_tests(project_id: str, c: Container = Depends(container)):
    """Suites, SQL validations, unit tests and mismatches for one project."""
    project = _project_or_404(c, project_id)
    run = c.repository.load_data_validation(project)
    cases = c.repository.load_test_cases(project) or []

    # --- suites -----------------------------------------------------------
    suites: dict[str, dict] = {}
    for case in cases:
        name = _suite_of(case)
        bucket = suites.setdefault(
            name, {"suite": name, "total": 0, "passed": 0, "failed": 0,
                   "warning": 0, "not_executed": 0,
                   "automatable": 0, "manual": 0})
        bucket["total"] += 1
        # Coverage is meaningful only over tests something could decide. A
        # suite of manual checks showing 0% reads as failed automation; it is
        # simply work that has not been done by hand yet.
        if getattr(case, "automatable", True):
            bucket["automatable"] += 1
        else:
            bucket["manual"] += 1
        status = _status_of(case).casefold()
        if status.startswith("pass"):
            bucket["passed"] += 1
        elif status.startswith("fail"):
            bucket["failed"] += 1
        elif status.startswith("warn"):
            bucket["warning"] += 1
        else:
            bucket["not_executed"] += 1

    # --- SQL validations --------------------------------------------------
    sql_rows = []
    bugs = []
    for row in (run.results if run else []):
        evidence = (getattr(row, "source_evidence", "")
                    or getattr(row, "generated_sql", "") or "")
        status = _status_of(row)
        sql_rows.append({
            "test_id": row.test_id,
            "kpi": row.kpi_name,
            "scenario": row.scenario or "",
            "query": evidence,
            "dashboard_value": row.dashboard_value or "",
            "database_value": row.database_value or "",
            "difference": row.difference or "",
            "match_type": row.match_type or "",
            "execution_time_ms": row.execution_time_ms,
            "status": status,
        })
        if not status.casefold().startswith("pass"):
            issue, severity = _classify(row)
            bugs.append({
                "test_id": row.test_id,
                "kpi": row.kpi_name,
                "scenario": row.scenario or "",
                "issue": issue,
                "severity": severity,
                "dashboard_value": row.dashboard_value or "",
                "database_value": row.database_value or "",
                "difference": row.difference or "",
                "status": status,
            })

    # --- unit tests -------------------------------------------------------
    # The developer suite: measures, calculated columns, relationships. These
    # are the checks a developer runs against the model rather than the report.
    unit = [
        {
            "test_case_id": case.test_case_id,
            "module": case.module,
            "scenario": case.test_scenario,
            "test_data": case.test_data,
            "expected": case.expected_result,
            "actual": case.actual_result,
            "priority": str(case.priority),
            "status": _status_of(case),
            "remarks": case.remarks,
        }
        for case in cases if str(case.kind).casefold().startswith("developer")
    ]

    return {
        "project_id": project.id,
        "project_name": project.name,
        "analysed": run is not None,
        "suites": sorted(suites.values(), key=lambda s: -s["total"]),
        "sql_validations": sql_rows,
        "unit_tests": unit,
        "visual_bugs": bugs,
        "counts": {
            "suites": len(suites),
            "sql": len(sql_rows),
            "unit": len(unit),
            "bugs": len(bugs),
        },
    }
