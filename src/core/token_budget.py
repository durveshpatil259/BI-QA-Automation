"""Daily token budget for one API key.

:mod:`src.core.usage` counts what a *run* cost. This counts what a *key* has
spent today, and refuses work that would overrun the provider's daily cap.

Two different limits bite on a free tier, and only one of them was handled:

* **tokens per minute** — paced by :mod:`src.services.llm.rate_limiter`, which
  waits for room. A TPM ceiling costs time, never results.
* **tokens per day** — no amount of waiting clears it. Hitting it mid-run used
  to surface as a wall of 429s: every remaining batch failed one by one, the
  report came out silently short, and nothing said the key was simply spent.

So the daily figure is tracked here, persisted across restarts, and checked
*before* each call rather than discovered from an error afterwards. When the
budget runs out the run stops cleanly and reports how far it got — a short
report that says why is worth far more than a short report that does not.

The ledger counts what **this application** spent. A key also used elsewhere
(another tool, the provider's playground) will have less real headroom than the
ledger believes, which is why :func:`observe_provider_headers` folds in the
provider's own numbers whenever a response carries them.

Persisted next to ``app_config.json``. Only a hash of the key is stored, never
the key itself, so the file is safe to keep and to read.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.core.logger import get_logger

_logger = get_logger()

__all__ = [
    "BudgetStatus",
    "DailyTokenLedger",
    "ledger",
    "status_for",
    "check_affordable",
    "record_usage",
    "observe_provider_headers",
]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _next_midnight() -> str:
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.isoformat(timespec="seconds")


def fingerprint(api_key: str) -> str:
    """A stable, non-reversible id for a key.

    Swapping in a different key must start a fresh count rather than inherit
    the old one's spend, and the file must never contain the secret.
    """
    key = (api_key or "").strip()
    if not key:
        return "no-key"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class BudgetStatus:
    """What one key has spent today, and what is left."""

    provider: str = ""
    model: str = ""
    limit: int = 0                  # 0 = no daily cap known; tracking only
    used: int = 0
    calls: int = 0
    resets_at: str = ""
    #: Lowest remaining figure the provider itself reported today, when it
    #: reports one. Authoritative where present: it counts spend from outside
    #: this application too.
    provider_remaining: int | None = None

    @property
    def enforced(self) -> bool:
        return self.limit > 0

    @property
    def remaining(self) -> int:
        """Tokens left today. ``-1`` when no cap is known."""
        if not self.enforced:
            return -1
        headroom = max(0, self.limit - self.used)
        if self.provider_remaining is not None:
            # Trust the smaller of the two: ours can only under-count.
            return min(headroom, max(0, self.provider_remaining))
        return headroom

    @property
    def percent_used(self) -> float:
        return (self.used / self.limit * 100.0) if self.enforced else 0.0

    def describe(self) -> str:
        if not self.enforced:
            return f"{self.used:,} tokens today over {self.calls} call(s); no daily cap configured"
        return (
            f"{self.used:,}/{self.limit:,} tokens today ({self.percent_used:.0f}%), "
            f"{self.remaining:,} remaining, resets {self.resets_at[:16].replace('T', ' ')}"
        )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "limit": self.limit,
            "used": self.used,
            "calls": self.calls,
            "remaining": self.remaining,
            "percent_used": round(self.percent_used, 1),
            "enforced": self.enforced,
            "resets_at": self.resets_at,
            "provider_remaining": self.provider_remaining,
        }


class DailyTokenLedger:
    """Per-key, per-model token totals for the current day."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict | None = None

    # --- persistence ------------------------------------------------------
    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            # A missing or corrupt ledger must never block a run: the worst
            # case is that today's count restarts at zero.
            self._data = {}
        self._data.setdefault("entries", {})
        return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            temp.replace(self._path)          # atomic, so a crash cannot truncate
        except OSError as exc:
            _logger.warning("Could not write the token ledger: %s", exc)

    @staticmethod
    def _key(provider: str, model: str, api_key: str) -> str:
        return f"{provider}|{model}|{fingerprint(api_key)}"

    def _entry(self, key: str) -> dict:
        entries = self._load()["entries"]
        entry = entries.get(key)
        if not isinstance(entry, dict) or entry.get("date") != _today():
            # A new day starts a new count. Yesterday's row is simply replaced;
            # this is a budget, not an audit log.
            entry = {"date": _today(), "tokens": 0, "calls": 0,
                     "provider_remaining": None}
            entries[key] = entry
        return entry

    # --- public -----------------------------------------------------------
    def status(self, provider: str, model: str, api_key: str,
               limit: int) -> BudgetStatus:
        with self._lock:
            entry = self._entry(self._key(provider, model, api_key))
            return BudgetStatus(
                provider=provider, model=model, limit=max(0, int(limit or 0)),
                used=int(entry.get("tokens") or 0),
                calls=int(entry.get("calls") or 0),
                resets_at=_next_midnight(),
                provider_remaining=entry.get("provider_remaining"),
            )

    def add(self, provider: str, model: str, api_key: str, tokens: int,
            limit: int) -> BudgetStatus:
        with self._lock:
            key = self._key(provider, model, api_key)
            entry = self._entry(key)
            entry["tokens"] = int(entry.get("tokens") or 0) + max(0, int(tokens))
            entry["calls"] = int(entry.get("calls") or 0) + 1
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()
            return BudgetStatus(
                provider=provider, model=model, limit=max(0, int(limit or 0)),
                used=entry["tokens"], calls=entry["calls"],
                resets_at=_next_midnight(),
                provider_remaining=entry.get("provider_remaining"),
            )

    def note_provider_remaining(self, provider: str, model: str, api_key: str,
                                remaining: int) -> None:
        """Record the provider's own daily figure, keeping the lowest seen.

        It only ever falls during a day, so the minimum is the current truth
        and a stale higher reading cannot revive an exhausted budget.
        """
        with self._lock:
            entry = self._entry(self._key(provider, model, api_key))
            seen = entry.get("provider_remaining")
            entry["provider_remaining"] = (
                remaining if seen is None else min(int(seen), int(remaining))
            )
            self._save()

    def reset(self, provider: str, model: str, api_key: str) -> None:
        """Clear today's count for one key — for tests and manual overrides."""
        with self._lock:
            self._load()["entries"].pop(self._key(provider, model, api_key), None)
            self._save()


_LEDGER: DailyTokenLedger | None = None
_LEDGER_LOCK = threading.Lock()


def ledger() -> DailyTokenLedger:
    """The process-wide ledger, created on first use."""
    global _LEDGER
    with _LEDGER_LOCK:
        if _LEDGER is None:
            from src.core.config import CONFIG_DIR

            _LEDGER = DailyTokenLedger(CONFIG_DIR / "token_usage.json")
        return _LEDGER


def _daily_limit(provider, model: str) -> int:
    """Configured daily cap: explicit config first, then the provider default."""
    from src.core.config import load_config
    from src.services.llm import provider_registry as registry

    configured = int(getattr(load_config(), "llm_tokens_per_day", 0) or 0)
    if configured > 0:
        return configured
    return registry.tokens_per_day_for(provider, model)


def status_for(provider, model: str, api_key: str) -> BudgetStatus:
    name = getattr(provider, "value", str(provider))
    return ledger().status(name, model, api_key, _daily_limit(provider, model))


def check_affordable(provider, model: str, api_key: str, tokens: int) -> BudgetStatus:
    """Raise :class:`TokenBudgetExhausted` when *tokens* will not fit today.

    Checked before sending rather than reserved: calls are already serialised
    by the rate limiter, and a reservation that is never released would leak
    budget whenever a call fails.
    """
    from src.core.exceptions import TokenBudgetExhausted

    status = status_for(provider, model, api_key)
    if not status.enforced:
        return status
    if tokens > status.remaining:
        raise TokenBudgetExhausted(
            f"Daily token budget reached for {status.provider} / {status.model}: "
            f"{status.used:,} of {status.limit:,} used, {status.remaining:,} left, "
            f"and this call needs about {tokens:,}. "
            f"The budget resets at {status.resets_at[:16].replace('T', ' ')}."
        )
    return status


def record_usage(provider, model: str, api_key: str, tokens: int) -> BudgetStatus:
    name = getattr(provider, "value", str(provider))
    return ledger().add(name, model, api_key, tokens, _daily_limit(provider, model))


#: Header names that carry a *daily* remaining-token figure. Providers publish
#: per-minute figures under similar names, so only these exact day-scoped keys
#: are trusted — a per-minute remaining read as a daily one would halt a run
#: that has plenty of budget left.
_DAILY_TOKEN_HEADERS = (
    "x-ratelimit-remaining-tokens-day",
    "x-ratelimit-remaining-tokens-per-day",
    "anthropic-ratelimit-tokens-remaining-day",
)


def observe_provider_headers(provider, model: str, api_key: str, headers) -> None:
    """Fold a provider's own daily figure into the ledger, when it sends one.

    Most OpenAI-compatible providers report only per-minute headroom, so this
    is usually a no-op. Where a daily figure does arrive it is worth far more
    than the local count, because it includes spend this application never saw.
    """
    if not headers:
        return
    name = getattr(provider, "value", str(provider))
    lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    for header in _DAILY_TOKEN_HEADERS:
        raw = lowered.get(header)
        if raw is None:
            continue
        try:
            remaining = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
        ledger().note_provider_remaining(name, model, api_key, remaining)
        _logger.info("%s reports %d tokens remaining today (%s)",
                     name, remaining, header)
        return
