"""Parse and compare human-formatted numeric values.

Dashboards show values like ``109.81M``, ``11.4%``, ``$1,234.5``, ``(500)``.
The datasource returns plain numbers. To compare the two deterministically we
normalise both to floats and apply a percentage tolerance.

This module is the arithmetic core of the data-validation engine and is fully
unit-testable with no external dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Magnitude suffixes (case-insensitive).
_SUFFIX = {
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "t": 1e12,
}
_CURRENCY = "$€£₹¥"

# number with optional decimal + optional 1-2 letter magnitude suffix
_NUM_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z]{1,2})?$")


def parse_value(raw) -> tuple[float | None, str]:
    """Parse *raw* into ``(numeric, unit)``.

    ``unit`` is ``"%"`` for percentages, ``"currency"`` if a currency symbol was
    present, else ``""``. Returns ``(None, unit)`` when nothing numeric is found.

    Examples
    --------
    >>> parse_value("109.81M")   # (109810000.0, "")
    >>> parse_value("11.4%")     # (11.4, "%")
    >>> parse_value("$1,234.5")  # (1234.5, "currency")
    >>> parse_value("(500)")     # (-500.0, "")
    """
    if raw is None:
        return None, ""
    s = str(raw).strip()
    if not s:
        return None, ""

    unit = ""
    negative = False

    # Accounting-style negatives: (1,234)
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # Percentage
    if s.endswith("%"):
        unit = "%"
        s = s[:-1].strip()

    # Currency symbols
    for c in _CURRENCY:
        if c in s:
            unit = unit or "currency"
            s = s.replace(c, "")

    # Thousands separators and spaces
    s = s.replace(",", "").replace(" ", "").replace(" ", "").strip()

    # Explicit sign
    if s.startswith("-"):
        negative = True
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]

    if not s:
        return None, unit

    m = _NUM_RE.match(s)
    if not m:
        # last resort: a bare float
        try:
            val = float(s)
        except ValueError:
            return None, unit
        return (-val if negative else val), unit

    number = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix and suffix not in _SUFFIX:
        # Unknown trailing letters — not a value we can trust.
        return None, unit
    value = number * _SUFFIX.get(suffix, 1.0)
    return (-value if negative else value), unit


@dataclass
class ComparisonOutcome:
    passed: bool
    difference: float | None            # signed: database - dashboard
    difference_pct: float | None        # absolute percentage difference
    difference_display: str             # formatted signed difference
    reason: str
    match_type: str = ""                # "exact" | "numeric" | ""


def normalize_display(value) -> str:
    """Normalise a displayed value for exact string comparison.

    Ignores only cosmetic differences (case, spaces, thousands separators) so
    ``$51.88M`` and ``$ 51.88 M`` are treated as the same displayed value, while
    ``51.88M`` vs ``51.9M`` remain different.
    """
    s = str(value or "").strip()
    for ch in (" ", " ", ",", "\t"):
        s = s.replace(ch, "")
    return s.casefold()


def compare_display_values(
    dashboard_raw,
    database_raw,
    *,
    tolerance_pct: float = 1.0,
) -> ComparisonOutcome:
    """Compare a dashboard value to a database value, format-first.

    1. If the database returned the **same displayed string** (e.g. the SQL
       formatted its result as ``$51.88M``), that is an exact match — the
       strongest possible signal, and no numeric conversion is involved.
    2. Otherwise fall back to numeric comparison with a tolerance, so a raw
       aggregate like ``51879473.12`` still validates against ``$51.88M``.
    """
    dash_disp, db_disp = normalize_display(dashboard_raw), normalize_display(database_raw)
    if dash_disp and dash_disp == db_disp:
        return ComparisonOutcome(
            passed=True, difference=0.0, difference_pct=0.0, difference_display="0",
            reason=(
                f"Exact format match: database returned '{database_raw}', "
                f"identical to the dashboard display."
            ),
            match_type="exact",
        )

    dash_num, dash_unit = parse_value(dashboard_raw)
    db_num, db_unit = parse_value(database_raw)
    outcome = compare_values(dash_num, db_num, tolerance_pct=tolerance_pct)
    if outcome.passed:
        outcome.match_type = "numeric"
        outcome.reason = (
            f"Numeric match within tolerance (display differs: dashboard "
            f"'{dashboard_raw}' vs database '{database_raw}'). " + outcome.reason
        )
        return outcome

    # A percentage has two conventional representations and the source picks
    # one of them: a dashboard showing 60.0% is 60 to a query that formats a
    # percentage, and 0.6 to one that returns the underlying ratio. DAX DIVIDE
    # yields the ratio, so a correct value was being failed on scale alone.
    scaled = _compare_percent_scale(
        dash_num, dash_unit, db_num, db_unit, tolerance_pct
    )
    if scaled is not None:
        return scaled

    rounded = _compare_at_display_precision(
        dashboard_raw, dash_num, database_raw, db_num, tolerance_pct
    )
    if rounded is not None:
        return rounded
    return outcome


def _decimals_shown(text) -> int | None:
    """Decimal places in a displayed number, or None if it is not numeric."""
    digits = re.sub(r"[^0-9.]", "", str(text or ""))
    if not digits or digits.count(".") > 1:
        return None
    return len(digits.split(".")[1]) if "." in digits else 0


def _compare_at_display_precision(
    dashboard_raw, dash_num, database_raw, db_num, tolerance_pct
) -> "ComparisonOutcome | None":
    """Retry with the source rounded to the precision the dashboard shows.

    A card displaying ``4`` for 3.5557 is not wrong — it is rounded. Comparing
    the rendered string against full precision made a correct value fail by
    12%, which is the dashboard's formatting, not a data defect.

    Only ever *loses* precision from the source, and only when the dashboard
    genuinely shows fewer decimals, so it cannot rescue a real mismatch.
    """
    if dash_num is None or db_num is None:
        return None
    dash_places = _decimals_shown(dashboard_raw)
    db_places = _decimals_shown(database_raw)
    if dash_places is None or db_places is None or db_places <= dash_places:
        return None

    if round(db_num, dash_places) != dash_num:
        return None
    outcome = compare_values(dash_num, round(db_num, dash_places),
                             tolerance_pct=tolerance_pct)
    if not outcome.passed:
        return None
    outcome.match_type = "rounded"
    outcome.reason = (
        f"Match at the dashboard's displayed precision: it shows "
        f"'{dashboard_raw}' ({dash_places} dp) and the source returned "
        f"{db_num:g}, which rounds to the same value. Differences smaller than "
        f"the displayed precision cannot be detected from a rendered value."
    )
    return outcome


def _compare_percent_scale(
    dash_num, dash_unit, db_num, db_unit, tolerance_pct
) -> "ComparisonOutcome | None":
    """Retry a percentage comparison with the ratio/percent scales reconciled.

    Only when exactly one side is written as a percentage — if both are, or
    neither is, there is no scale question to resolve and a 100x adjustment
    would manufacture a match.
    """
    if dash_num is None or db_num is None:
        return None
    is_percent = (dash_unit == "%", db_unit == "%")
    if is_percent[0] == is_percent[1]:
        return None

    if is_percent[0]:
        percent_value, plain_value, side = dash_num, db_num, "database"
    else:
        percent_value, plain_value, side = db_num, dash_num, "dashboard"

    # A ratio is bounded in practice; without this, 6000 would "match" 60%.
    if abs(plain_value) > 1.5:
        return None

    outcome = compare_values(percent_value / 100.0, plain_value,
                             tolerance_pct=tolerance_pct)
    if not outcome.passed:
        return None
    outcome.match_type = "numeric-percent"
    outcome.reason = (
        f"Percentage match once the scales are reconciled: {percent_value}% "
        f"is the ratio {percent_value / 100.0:g}, which the {side} returned "
        f"as {plain_value:g}. " + outcome.reason
    )
    return outcome


def _fmt(n: float) -> str:
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.4f}".rstrip("0").rstrip(".")


def compare_values(
    dashboard_numeric: float | None,
    database_numeric: float | None,
    *,
    tolerance_pct: float = 1.0,
) -> ComparisonOutcome:
    """Compare two numbers with a percentage *tolerance_pct*.

    A pair is a PASS when the absolute difference is within ``tolerance_pct`` of
    the dashboard value (or of the database value when the dashboard value is 0).
    """
    if dashboard_numeric is None or database_numeric is None:
        return ComparisonOutcome(
            passed=False, difference=None, difference_pct=None,
            difference_display="—",
            reason="Could not parse one of the values to a number.",
        )

    difference = database_numeric - dashboard_numeric
    denom = abs(dashboard_numeric) if dashboard_numeric != 0 else (
        abs(database_numeric) if database_numeric != 0 else 1.0
    )
    difference_pct = abs(difference) / denom * 100.0
    passed = difference_pct <= tolerance_pct

    if passed:
        reason = (
            f"Values match within tolerance ({difference_pct:.3f}% ≤ {tolerance_pct}%)."
        )
    else:
        reason = (
            f"Difference {difference_pct:.3f}% exceeds tolerance {tolerance_pct}% "
            f"(dashboard={_fmt(dashboard_numeric)}, database={_fmt(database_numeric)})."
        )
    return ComparisonOutcome(
        passed=passed,
        difference=difference,
        difference_pct=difference_pct,
        difference_display=("+" if difference > 0 else "") + _fmt(difference),
        reason=reason,
    )
