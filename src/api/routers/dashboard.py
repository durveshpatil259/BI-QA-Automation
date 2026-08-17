"""Cross-project statistics for the dashboard overview.

Read-only and cheap: every figure comes from the small per-run summary written
when an analysis completes, so this opens one small file per project rather
than the full validation and test-case artifacts.

Nothing here is invented. A project that has never been analysed contributes
nothing, and an installation with no runs returns zeros with a null average —
the caller renders an empty state rather than a plausible-looking number.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import Container, container
from src.core.logger import get_logger

_logger = get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _humanise(ms: float | None) -> str:
    """Duration as a person would say it. '--' when there is nothing to average."""
    if not ms or ms <= 0:
        return "--"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


@router.get("/stats")
def dashboard_stats(c: Container = Depends(container)):
    """Headline figures, recent runs and the breakdowns the overview charts."""
    summaries = c.repository.list_run_summaries()

    if not summaries:
        # An installation with no completed runs. Explicit zeros and a null
        # average, so the UI can show its empty state without guessing.
        return {
            "has_data": False,
            "projects_analyzed": 0,
            "tests_passed": 0,
            "issues_found": 0,
            "test_cases_generated": 0,
            "avg_processing_time_ms": None,
            "avg_processing_time": "--",
            "test_summary": {"passed": 0, "failed": 0, "warning": 0, "skipped": 0},
            "recent_projects": [],
            "trend": [],
            "optimization": {"candidates": 0, "selected": 0,
                             "duplicates_removed": 0, "low_value_skipped": 0},
            "issue_distribution": {"runs": 0, "high": 0, "medium": 0, "low": 0},
        }

    timed = [s.processing_time_ms for s in summaries if s.processing_time_ms]
    average = sum(timed) / len(timed) if timed else None

    totals = {
        "passed": sum(s.tests_passed for s in summaries),
        "failed": sum(s.tests_failed for s in summaries),
        "warning": sum(s.tests_warning for s in summaries),
        "skipped": sum(s.tests_skipped for s in summaries),
    }

    return {
        "has_data": True,
        "projects_analyzed": len(summaries),
        "tests_passed": totals["passed"],
        "issues_found": sum(s.issues for s in summaries),
        "test_cases_generated": sum(s.test_cases for s in summaries),
        "avg_processing_time_ms": average,
        "avg_processing_time": _humanise(average),
        "test_summary": totals,
        "recent_projects": [
            {
                "project_id": s.project_id,
                "name": s.project_name,
                "status": s.status,
                "generated_at": s.generated_at.isoformat()
                if hasattr(s.generated_at, "isoformat") else str(s.generated_at),
                "tests": s.tests_total,
                "passed": s.tests_passed,
                "failed": s.tests_failed,
                "issues": s.issues,
                "processing_time": _humanise(s.processing_time_ms),
                "tokens": s.tokens,
            }
            for s in summaries[:8]
        ],
        # Oldest first: a trend reads left to right.
        "trend": [
            {
                "at": s.generated_at.isoformat()
                if hasattr(s.generated_at, "isoformat") else str(s.generated_at),
                "project": s.project_name,
                "total": s.tests_total,
                "passed": s.tests_passed,
                "failed": s.tests_failed,
            }
            for s in reversed(summaries[:14])
        ],
        # Selection figures come only from runs that actually recorded them.
        # Summing "selected" over every run while "candidates" exists on only
        # some produces a card claiming more tests were selected than were ever
        # generated — arithmetic that cannot be true, from mixing two
        # populations. Runs predating these counters are excluded outright.
        "optimization": _optimization([s for s in summaries if s.candidates > 0]),
        # Severity was added after some runs were recorded, so a run is only
        # counted when its figures are trustworthy. Two cases qualify: it
        # classified something, or it had nothing to classify because every
        # test passed — a genuine all-zero. What is excluded is the run with
        # failures but no severity, where zero means "not measured" and
        # counting it would report real problems as none.
        "issue_distribution": _severity([
            s for s in summaries
            if (s.issues_high or s.issues_medium or s.issues_low) or s.issues == 0
        ]),
    }


def _severity(scored: list) -> dict:
    return {
        "runs": len(scored),
        "high": sum(s.issues_high for s in scored),
        "medium": sum(s.issues_medium for s in scored),
        "low": sum(s.issues_low for s in scored),
    }


@router.get("/projects")
def dashboard_projects(c: Container = Depends(container)):
    """Every project, joined to its last run.

    A project with no completed run still appears — it exists, and hiding it
    would leave a user unable to find or delete something they created. Its
    result counts are null rather than zero, so the caller can tell "never run"
    apart from "ran and found nothing".
    """
    rows = []
    for project in c.project_service.list_projects():
        summary = c.repository.load_run_summary(project)
        has_report = c.repository.has_report(project)
        rows.append({
            "project_id": project.id,
            "name": project.name,
            "description": project.description,
            "platform": str(project.bi_platform),
            "environment": project.environment,
            "status": str(project.status),
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "dashboard_file": (project.dashboard_files or [None])[0],
            "has_report": has_report,
            "analysed": summary is not None,
            "tests": summary.tests_total if summary else None,
            "passed": summary.tests_passed if summary else None,
            "failed": summary.tests_failed if summary else None,
            "issues": summary.issues if summary else None,
            "test_cases": summary.test_cases if summary else None,
            "tokens": summary.tokens if summary else None,
            "processing_time": _humanise(
                summary.processing_time_ms if summary else project.processing_time_ms),
        })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"projects": rows, "total": len(rows)}


def _optimization(scored: list) -> dict:
    """Selection totals over runs that recorded them, or zeros."""
    return {
        "runs": len(scored),
        "candidates": sum(s.candidates for s in scored),
        "selected": sum(s.test_cases for s in scored),
        "duplicates_removed": sum(s.duplicates_removed for s in scored),
        "low_value_skipped": sum(s.low_value_skipped for s in scored),
        "compiled_without_llm": sum(s.compiled_without_llm for s in scored),
        "llm_calls": sum(s.llm_calls for s in scored),
        "tokens": sum(s.tokens for s in scored),
    }
