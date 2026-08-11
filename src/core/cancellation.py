"""Cooperative cancellation that reaches all the way down to the HTTP retry.

Checking a flag between pipeline stages is not enough: a single stage can sit
inside a provider back-off for minutes (3 retries x 30s, per batch, x N
batches), so a user who clicks Cancel keeps watching the run call the LLM.

The token is published on a :class:`~contextvars.ContextVar` so services do not
need a ``cancel_token`` parameter threaded through every signature. Context
variables are copied into worker threads by :func:`asyncio.to_thread`, and the
token wraps a :class:`threading.Event`, so the event loop can cancel a run that
is executing in a worker thread.

Lives in ``core`` because ``services`` must never import from ``pipeline``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from src.core.exceptions import OperationCancelled

__all__ = [
    "CancelToken",
    "current_token",
    "use_token",
    "is_cancelled",
    "raise_if_cancelled",
    "sleep",
]


class CancelToken:
    """A shared cancellation flag, safe to set from another thread."""

    __slots__ = ("_event",)

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self, what: str = "Run") -> None:
        if self._event.is_set():
            raise OperationCancelled(f"{what} cancelled by user.")

    def sleep(self, seconds: float) -> None:
        """Wait, but wake immediately on cancel.

        ``Event.wait`` returns as soon as the flag is set, which is what makes
        a 30s provider back-off abortable.
        """
        if self._event.wait(timeout=max(0.0, seconds)):
            raise OperationCancelled("Run cancelled by user.")


#: ``None`` outside a run, so library code stays usable without a pipeline.
_current: ContextVar[CancelToken | None] = ContextVar(
    "bi_testpilot_cancel_token", default=None
)


def current_token() -> CancelToken | None:
    return _current.get()


@contextmanager
def use_token(token: CancelToken) -> Iterator[CancelToken]:
    """Publish ``token`` for the duration of the block."""
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)


def is_cancelled() -> bool:
    token = _current.get()
    return bool(token and token.cancelled)


def raise_if_cancelled(what: str = "Run") -> None:
    """No-op when no run is active."""
    token = _current.get()
    if token is not None:
        token.raise_if_cancelled(what)


def sleep(seconds: float) -> None:
    """Cancellable sleep; falls back to a plain sleep outside a run."""
    token = _current.get()
    if token is None:
        threading.Event().wait(timeout=max(0.0, seconds))
        return
    token.sleep(seconds)
