"""Deduplicate and prioritise generated test cases.

Test cases were produced as a cross-product: every template applied to every
KPI, every chart and every filter, once per scenario. On a real dashboard that
yielded 906 cases in which ``Gross Margin`` alone appeared 18 times — the same
logical check re-stated for each slicer combination. Volume like that is not
coverage; it buries the handful of results a reviewer would actually act on.

Two rules, both deterministic:

* **Fingerprint.** A test is identified by what it *proves*, not by its wording.
  "Validate Total Sales" and "Check Total Sales KPI" collapse to one entry.
* **Priority.** Low-priority cosmetic checks are kept only where nothing else
  already covers that visual, so formatting never crowds out data validation.

Nothing here calls an LLM, and nothing here changes a verdict: a test that was
executed keeps its status. Only duplicates and low-value repeats are removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.core.constants import Priority
from src.core.logger import get_logger

_logger = get_logger()

__all__ = ["DedupStats", "fingerprint", "deduplicate"]

#: Wording that differs between templates but not in meaning.
_NOISE = re.compile(
    r"\b(validate|verify|check|ensure|confirm|test|the|a|an|is|are|for|of|"
    r"correct|correctly|value|values|kpi|card|visual|chart)\b",
    re.IGNORECASE,
)


@dataclass
class DedupStats:
    """What the pass removed, for the report's optimisation section."""

    original: int = 0
    kept: int = 0
    duplicates_removed: int = 0
    low_value_skipped: int = 0
    by_priority: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "kept": self.kept,
            "duplicates_removed": self.duplicates_removed,
            "low_value_skipped": self.low_value_skipped,
            "by_priority": dict(self.by_priority),
        }

    def describe(self) -> str:
        return (
            f"{self.original} candidate tests -> {self.kept} kept "
            f"({self.duplicates_removed} duplicates, "
            f"{self.low_value_skipped} low-value skipped)"
        )


def _normalise(text: str) -> str:
    """Reduce a title to the concepts it names, so wording stops mattering."""
    words = _NOISE.sub(" ", (text or "").casefold())
    return " ".join(sorted(set(re.findall(r"[a-z0-9%]+", words))))


def fingerprint(case) -> tuple:
    """What this test proves: subject, kind, and the check being made.

    ``module`` carries the subject (``KPI: Total Sales``) and ``test_scenario``
    the check. Two cases agreeing on both prove the same thing however they are
    phrased.

    An **executed** test additionally carries its filter context and its query.
    Those are not restatements of one check: ``Gross Margin`` under FY2018 and
    under FY2019 are different measurements with different values, and folding
    them together would delete evidence — including the outliers most worth
    looking at. Only the unexecuted template tests collapse across scenarios.
    """
    base = (
        str(getattr(case, "kind", "")),
        (getattr(case, "module", "") or "").casefold().strip(),
        _normalise(getattr(case, "test_scenario", "")),
    )
    if not _is_executed(case):
        return base
    return base + (
        (getattr(case, "test_data", "") or "").casefold().strip(),
        " ".join((getattr(case, "generated_sql", "") or "").split()).casefold(),
    )


def _priority_of(case) -> Priority:
    value = getattr(case, "priority", None)
    if isinstance(value, Priority):
        return value
    text = str(value or "").casefold()
    if text.startswith("high"):
        return Priority.HIGH
    if text.startswith("low"):
        return Priority.LOW
    return Priority.MEDIUM


def _is_executed(case) -> bool:
    """A test with a real verdict is evidence and is never dropped."""
    status = str(getattr(case, "status", "") or "").casefold()
    return bool(getattr(case, "generated_sql", "")) or status in (
        "pass", "fail", "warning"
    )


def deduplicate(cases: list, *, max_low_per_subject: int = 1,
                max_medium_per_subject: int = 2,
                max_high_per_subject: int = 3) -> tuple[list, DedupStats]:
    """Collapse duplicates and trim low-value repeats.

    Executed tests are kept unconditionally — they carry a measured result, and
    discarding one would remove evidence rather than noise.
    """
    stats = DedupStats(original=len(cases))
    seen: set[tuple] = set()
    # Subjects already proved by a measured result. A template test restating
    # one adds nothing: the executed row carries the same claim plus a value.
    proved: set[tuple] = set()
    low_per_subject: dict[str, int] = {}
    kept: list = []

    # Executed first, so when an executed and a template test collide it is the
    # executed one that survives.
    ordered = sorted(cases, key=lambda c: (not _is_executed(c),
                                           _priority_of(c) is not Priority.HIGH))
    for case in ordered:
        key = fingerprint(case)
        executed = _is_executed(case)
        base = key[:3]
        if key in seen or (not executed and base in proved):
            stats.duplicates_removed += 1
            continue

        # Per-subject caps for unexecuted template tests. High priority is
        # never capped — that is the data validation the report exists for.
        # The caps apply to the restatements around it: a KPI does not need
        # four separate cosmetic checks to be well covered.
        priority = _priority_of(case)
        if not executed:
            subject = (getattr(case, "module", "") or "").casefold()
            cap = {Priority.LOW: max_low_per_subject,
                   Priority.MEDIUM: max_medium_per_subject,
                   Priority.HIGH: max_high_per_subject}[priority]
            bucket = f"{priority.value}|{subject}"
            if low_per_subject.get(bucket, 0) >= cap:
                stats.low_value_skipped += 1
                continue
            low_per_subject[bucket] = low_per_subject.get(bucket, 0) + 1

        seen.add(key)
        if executed:
            proved.add(base)
        kept.append(case)
        label = _priority_of(case).value
        stats.by_priority[label] = stats.by_priority.get(label, 0) + 1

    stats.kept = len(kept)
    _logger.info("Test deduplication: %s", stats.describe())
    return kept, stats
