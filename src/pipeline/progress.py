"""Progress reporting for a pipeline run.

The runner never writes to a queue or socket directly — it calls a
:class:`ProgressReporter`. That indirection is what lets the same runner drive a
Streamlit callback today and an SSE stream (or Celery events) tomorrow.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from src.pipeline.stages import Stage


@dataclass
class ProgressEvent:
    """One observable moment in a run."""

    job_id: str
    stage: str
    index: int                   # 1-based position in STAGE_ORDER
    total: int
    status: str                  # "running" | "done" | "skipped" | "failed"
    message: str = ""
    pct: int = 0
    elapsed_ms: int = 0
    at: _dt.datetime = field(default_factory=_dt.datetime.now)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data


class ProgressReporter:
    """Collects events and forwards them to an optional sink."""

    def __init__(self, job_id: str, total_stages: int, sink: Callable | None = None):
        self.job_id = job_id
        self.total_stages = total_stages
        self._sink = sink
        self._started = time.perf_counter()
        self.events: list[ProgressEvent] = []

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)

    def emit(
        self,
        stage: Stage,
        index: int,
        status: str,
        message: str = "",
    ) -> ProgressEvent:
        # Percentage reflects *completed* stages so a running stage never shows
        # as finished.
        completed = index if status in ("done", "skipped", "failed") else index - 1
        pct = int(max(0, min(100, completed / self.total_stages * 100)))
        event = ProgressEvent(
            job_id=self.job_id,
            stage=stage.name,
            index=index,
            total=self.total_stages,
            status=status,
            message=message or stage.value,
            pct=pct,
            elapsed_ms=self._elapsed_ms(),
        )
        self.events.append(event)
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:  # noqa: BLE001 - a broken sink must not fail the run
                pass
        return event

    @property
    def elapsed_ms(self) -> int:
        return self._elapsed_ms()
