"""Deterministic data-validation building blocks.

Pure Python: parses human-formatted dashboard values (e.g. "109.81M", "11.4%",
"$1,234") into numbers and compares them to datasource results with a tolerance.
No AI is involved here — this is the arithmetic the LLM must never do.
"""

from src.services.validation.value_parser import (
    ComparisonOutcome,
    compare_display_values,
    compare_values,
    normalize_display,
    parse_value,
)

__all__ = [
    "parse_value",
    "compare_values",
    "compare_display_values",
    "normalize_display",
    "ComparisonOutcome",
]
