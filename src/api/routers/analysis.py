"""Analyze + job endpoints — screen 2 (live progress) of the UI."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from src.api.deps import Container, container, jobs as jobs_dep
from src.api.schemas import (
    AnalyzeRequest,
    JobResponse,
    JobSummary,
    ResultsResponse,
    ValidationRow,
)
from src.pipeline import PipelineContext
from src.pipeline.jobs import JobManager

router = APIRouter(prefix="/api", tags=["analysis"])


def _job_response(snapshot: dict) -> JobResponse:
    return JobResponse(
        job_id=snapshot["job_id"],
        project_id=snapshot["project_id"],
        state=snapshot["state"],
        pct=snapshot["pct"],
        stage=snapshot["stage"],
        message=snapshot["message"],
        elapsed_ms=snapshot["elapsed_ms"],
        error=snapshot["error"],
        summary=JobSummary(**(snapshot.get("summary") or {})),
        warnings=snapshot.get("warnings", []),
    )


@router.post("/projects/{project_id}/analyze", response_model=JobResponse, status_code=202)
async def analyze(  # async so JobManager.submit sees the running event loop
    project_id: str,
    body: AnalyzeRequest = AnalyzeRequest(),
    c: Container = Depends(container),
    manager: JobManager = Depends(jobs_dep),
):
    """Start the full pipeline in the background. Returns immediately."""
    project = c.project_service.get_project(project_id)
    if not project.dashboard_files:
        raise HTTPException(400, "Upload a PBIX file before running analysis.")

    ctx = PipelineContext(
        project=project,
        datasource=c.datasource_service.load(project),
        tolerance_pct=body.tolerance_pct,
    )
    job = manager.submit(ctx)
    return _job_response(job.snapshot())


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, manager: JobManager = Depends(jobs_dep)):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, f"No job '{job_id}'.")
    return _job_response(job.snapshot())


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, manager: JobManager = Depends(jobs_dep)):
    """Server-Sent Events feed of pipeline progress.

    SSE rather than WebSockets: progress is one-directional, it is plain HTTP,
    it auto-reconnects in the browser, and it traverses proxies cleanly.
    """
    if manager.get(job_id) is None:
        raise HTTPException(404, f"No job '{job_id}'.")

    async def events():
        async for event in manager.stream(job_id):
            yield f"data: {json.dumps(event.to_dict())}\n\n"
        final = manager.get(job_id)
        if final is not None:
            yield f"event: done\ndata: {json.dumps(final.snapshot())}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/cancel", response_model=dict)
def cancel_job(job_id: str, manager: JobManager = Depends(jobs_dep)):
    if manager.get(job_id) is None:
        raise HTTPException(404, f"No job '{job_id}'.")
    return {"cancelled": manager.cancel(job_id)}


@router.get("/projects/{project_id}/results", response_model=ResultsResponse)
def get_results(project_id: str, c: Container = Depends(container)):
    """Screen 3 — the summary dashboard and per-test rows."""
    project = c.project_service.get_project(project_id)
    run = c.sql_validation_engine.load(project)
    if run is None:
        return ResultsResponse(project_id=project_id, summary=JobSummary())

    s = run.summary()
    rows = [
        ValidationRow(
            test_id=r.test_id, kpi=r.kpi_name, scenario=r.scenario,
            dashboard_value=r.dashboard_value, generated_sql=r.generated_sql,
            source_evidence=getattr(r, 'source_evidence', ''),
            database_value=r.database_value, difference=r.difference,
            match_type=r.match_type, execution_time_ms=r.execution_time_ms,
            status=str(r.status),
        )
        for r in run.results
    ]
    return ResultsResponse(
        project_id=project_id,
        summary=JobSummary(
            tests=s["total"], passed=s["passed"], failed=s["failed"], warnings=s["errors"]
        ),
        rows=rows,
    )


@router.post("/projects/{project_id}/explain", response_model=dict)
def explain_failures(project_id: str, c: Container = Depends(container)):
    """AI explanations for failing validations (screen 3 action)."""
    project = c.project_service.get_project(project_id)
    run = c.sql_validation_engine.explain_failures(project)
    return {
        "explained": [
            {"test_id": r.test_id, "kpi": r.kpi_name, "recommendation": r.recommendation}
            for r in run.results if r.recommendation
        ]
    }
