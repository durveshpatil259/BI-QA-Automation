"""How bad a failing validation is, decided once.

Both the run summary and the Visual Bugs view need to say what a failure means.
Two implementations of that judgement would eventually disagree, and the
dashboard would then contradict the page it links to. This is the single place
the question is answered.
"""

from __future__ import annotations

__all__ = ["classify"]


def classify(match_type: str, database_value: str) -> tuple[str, str]:
    """``(issue, severity)`` for one non-passing validation.

    A query that never ran is a different problem from one that ran and
    disagreed, so the empty-value check comes first: without a source value
    there is nothing to have disagreed with.
    """
    match = (match_type or "").casefold()
    value = str(database_value or "").strip()

    if not value or value == "—":
        return "Query did not run", "High"
    if match.startswith("chart"):
        return "Category set differs from the source", "Medium"
    if "percent" in match:
        return "Percentage value mismatch", "High"
    if match == "dax-consistency":
        return "Measure disagrees with its own components", "High"
    if match in ("exact", "numeric", "rounded", ""):
        return "Value differs from the source", "High"
    return "Value differs from the source", "Medium"
