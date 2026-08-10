"""Pipeline orchestration layer.

Turns the previously manual, click-per-step workflow into a single deterministic
run. :class:`PipelineRunner` executes a fixed, ordered list of stages; a
:class:`JobManager` runs it in the background and streams progress so the UI
never blocks.

No AI decides what runs next — the sequence is plain code. Each stage delegates
to an existing single-responsibility service.
"""

from src.pipeline.context import PipelineContext
from src.pipeline.jobs import Job, JobManager, JobState
from src.pipeline.progress import ProgressEvent, ProgressReporter
from src.pipeline.runner import PipelineRunner
from src.pipeline.stages import FailurePolicy, Stage, STAGE_ORDER

__all__ = [
    "PipelineContext",
    "PipelineRunner",
    "ProgressEvent",
    "ProgressReporter",
    "Job",
    "JobManager",
    "JobState",
    "Stage",
    "FailurePolicy",
    "STAGE_ORDER",
]
