"""Client-side token pacing.

Free tiers cap *tokens per minute*, not requests. One SQL-generation batch for
a medium dashboard costs ~5,400 tokens, so only two fit in Groq's 12,000 TPM
window — yet the plan service fires nine back-to-back. Batches 3+ were rejected
before they were ever really attempted, and the run silently produced a tenth
of the validations it should have.

Waiting *before* sending is strictly better than retrying after a 429: the
provider charges rejected requests against the daily quota too, so a burst that
gets bounced burns the allowance without producing anything.
"""

from __future__ import annotations

import threading
import time

from src.core import cancellation
from src.core.logger import get_logger

_logger = get_logger()

__all__ = ["TokenRateLimiter"]

_WINDOW = 60.0  # providers publish limits per minute


class TokenRateLimiter:
    """Rolling-window token budget, shared across threads."""

    def __init__(self, tokens_per_minute: int) -> None:
        self._tpm = max(0, int(tokens_per_minute or 0))
        self._events: list[tuple[float, int]] = []   # (sent_at, tokens)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._tpm > 0

    def _used(self, now: float) -> int:
        self._events = [(t, n) for t, n in self._events if now - t < _WINDOW]
        return sum(n for _, n in self._events)

    def acquire(self, tokens: int) -> None:
        """Block until *tokens* fit in the window, then record them.

        Sleeps via :mod:`src.core.cancellation`, so Cancel still interrupts a
        run that is waiting here.
        """
        if not self.enabled:
            return
        tokens = max(0, int(tokens))
        while True:
            with self._lock:
                now = time.monotonic()
                used = self._used(now)
                # A single call larger than the whole budget can never fit;
                # let it through rather than deadlock, and let the retry cope.
                if used + tokens <= self._tpm or not self._events:
                    self._events.append((now, tokens))
                    return
                oldest = self._events[0][0]
                wait = max(0.0, _WINDOW - (now - oldest)) + 0.25
            _logger.info(
                "Rate pacing: %d tokens would exceed %d TPM (used %d); waiting %.1fs",
                tokens, self._tpm, used, wait,
            )
            cancellation.sleep(wait)
