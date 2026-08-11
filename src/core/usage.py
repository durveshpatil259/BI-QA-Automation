"""Token accounting for one run.

Every provider returns ``usage`` on each response, but it was parsed and thrown
away — so a run that quietly cost 50,000 tokens looked identical to one that
cost 5,000, and there was no way to see which stage was expensive.

Published on a :class:`~contextvars.ContextVar` like
:mod:`src.core.cancellation`, so the LLM client can record a call without every
service passing an accumulator down through its signatures.

Lives in ``core`` because ``services`` must not import from ``pipeline``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

__all__ = ["StageUsage", "UsageAccumulator", "current", "use_collector", "record"]


@dataclass
class StageUsage:
    """Tokens attributed to one pipeline stage."""

    stage: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int, total: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        # Providers that omit a total still let us report a useful sum.
        self.total_tokens += total or (prompt + completion)


class UsageAccumulator:
    """Thread-safe per-run token totals, broken down by stage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, StageUsage] = {}
        self._label = "other"
        self._models: set[str] = set()

    @contextmanager
    def stage(self, label: str) -> Iterator[None]:
        """Attribute calls made inside the block to *label*."""
        with self._lock:
            previous = self._label
            self._label = label or "other"
        try:
            yield
        finally:
            with self._lock:
                self._label = previous

    def record(self, model: str, usage: dict) -> None:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        if not (prompt or completion or total):
            return
        with self._lock:
            entry = self._stages.setdefault(self._label, StageUsage(stage=self._label))
            entry.add(prompt, completion, total)
            if model:
                self._models.add(model)

    # --- reporting --------------------------------------------------------
    @property
    def stages(self) -> list[StageUsage]:
        with self._lock:
            return sorted(
                self._stages.values(), key=lambda s: s.total_tokens, reverse=True
            )

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return sum(s.total_tokens for s in self._stages.values())

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(s.calls for s in self._stages.values())

    @property
    def models(self) -> list[str]:
        with self._lock:
            return sorted(self._models)

    def to_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": sum(s.prompt_tokens for s in self.stages),
            "completion_tokens": sum(s.completion_tokens for s in self.stages),
            "total_calls": self.total_calls,
            "models": self.models,
            "by_stage": [
                {
                    "stage": s.stage,
                    "calls": s.calls,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": s.completion_tokens,
                    "total_tokens": s.total_tokens,
                }
                for s in self.stages
            ],
        }


_current: ContextVar[UsageAccumulator | None] = ContextVar(
    "bi_testpilot_token_usage", default=None
)


def current() -> UsageAccumulator | None:
    return _current.get()


@contextmanager
def use_collector(accumulator: UsageAccumulator) -> Iterator[UsageAccumulator]:
    reset = _current.set(accumulator)
    try:
        yield accumulator
    finally:
        _current.reset(reset)


def record(model: str, usage: dict) -> None:
    """No-op outside a run, so the client stays usable standalone."""
    accumulator = _current.get()
    if accumulator is not None and usage:
        accumulator.record(model, usage)
