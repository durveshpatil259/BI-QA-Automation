"""Background job management for pipeline runs.

Clicking *Analyze* must return immediately, so the run happens in a background
asyncio task and progress is streamed. The pipeline itself is synchronous and
blocking (subprocesses, pyodbc, HTTP), so it is executed via
``asyncio.to_thread`` — the event loop is never stalled.

Concurrency is deliberately bounded: LLM providers rate-limit aggressively and
Power BI Desktop hosts a single model instance, so unbounded parallelism is
actively harmful.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

from src.core.logger import get_logger
from src.pipeline.context import PipelineContext
from src.pipeline.progress import ProgressEvent, ProgressReporter
from src.pipeline.runner import PipelineCancelled, PipelineRunner
from src.pipeline.stages import STAGE_ORDER

_logger = get_logger()

#: Sentinel pushed onto a job's queue to close its stream.
_DONE = object()


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    project_id: str
    state: JobState = JobState.QUEUED
    created_at: _dt.datetime = field(default_factory=_dt.datetime.now)
    finished_at: _dt.datetime | None = None
    error: str = ""
    context: PipelineContext | None = None
    events: list[ProgressEvent] = field(default_factory=list)

    def snapshot(self) -> dict:
        last = self.events[-1] if self.events else None
        return {
            "job_id": self.id,
            "project_id": self.project_id,
            "state": self.state.value,
            "pct": last.pct if last else 0,
            "stage": last.stage if last else None,
            "message": last.message if last else "",
            "elapsed_ms": last.elapsed_ms if last else 0,
            "error": self.error,
            "summary": self.context.summary() if self.context else {},
            "warnings": list(self.context.warnings) if self.context else [],
        }


class JobManager:
    """Submits, tracks, streams and cancels pipeline runs."""

    def __init__(self, runner: PipelineRunner, max_concurrent: int = 2):
        self._runner = runner
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(max_concurrent)

    # --- submission -------------------------------------------------------
    def submit(self, ctx: PipelineContext) -> Job:
        """Start a run in the background and return immediately.

        Must be called from within a running event loop (an ``async def``
        endpoint), since the run is scheduled as an asyncio task.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - programming error
            raise RuntimeError(
                "JobManager.submit() requires a running event loop — call it "
                "from an 'async def' endpoint."
            ) from exc
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        job = Job(id=job_id, project_id=ctx.project.id, context=ctx)
        self._jobs[job_id] = job
        self._queues[job_id] = asyncio.Queue()
        self._tasks[job_id] = asyncio.create_task(self._execute(job, ctx))
        _logger.info("job=%s submitted project=%s", job_id, ctx.project.id)
        return job

    async def _execute(self, job: Job, ctx: PipelineContext) -> None:
        queue = self._queues[job.id]
        loop = asyncio.get_running_loop()

        def sink(event: ProgressEvent) -> None:
            # Called from the worker thread -> hop back to the loop safely.
            job.events.append(event)
            loop.call_soon_threadsafe(queue.put_nowait, event)

        reporter = ProgressReporter(job.id, len(STAGE_ORDER), sink=sink)

        async with self._sem:
            job.state = JobState.RUNNING
            finished = False
            try:
                await asyncio.to_thread(self._runner.run, ctx, reporter)
                job.state = JobState.COMPLETED
                finished = True
            except PipelineCancelled:
                job.state = JobState.CANCELLED
                finished = True
            except asyncio.CancelledError:
                # The task itself was torn down (e.g. server shutdown). Only
                # report cancellation if the run had not already finished.
                if not finished:
                    job.state = JobState.CANCELLED
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                job.state = JobState.FAILED
                job.error = str(exc)
                finished = True
                _logger.warning("job=%s failed: %s", job.id, exc)
            finally:
                job.finished_at = _dt.datetime.now()
                loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    # --- queries ----------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def stream(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        """Yield progress events until the run finishes."""
        queue = self._queues.get(job_id)
        if queue is None:
            return
        # Replay what already happened so a late subscriber sees the full run.
        job = self._jobs[job_id]
        for event in list(job.events):
            yield event
        while True:
            item = await queue.get()
            if item is _DONE:
                return
            yield item

    # --- control ----------------------------------------------------------
    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation; the runner stops between stages."""
        job = self._jobs.get(job_id)
        if job is None or job.state in (
            JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED
        ):
            return False
        if job.context is not None:
            job.context.cancel()
        _logger.info("job=%s cancellation requested", job_id)
        return True
