"""Analyse DAX measures for cross-measure consistency rules.

A PBIX/PBIT never stores a *rendered* KPI value, only its DAX formula — so
there is no displayed number to validate against. However, many measures are
defined **in terms of other measures**::

    Total Profit    = [Total Sales] - [Total Cost]
    Profit Margin % = DIVIDE([Total Profit], [Total Sales])
    Avg Order Value = DIVIDE([Total Sales], [Total Orders])

That gives a genuine, screenshot-free verdict: execute the SQL for each measure
and check the arithmetic actually holds in the database. If
``Total Profit != Total Sales - Total Cost`` the model or the mapping is wrong.

This module extracts those relationships deterministically (no AI) and also
translates Power BI format strings into a description the SQL generator can use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# [Measure Name] references inside a DAX expression.
_MEASURE_REF = re.compile(r"\[([^\]\[]+)\]")

# Recognised binary shapes, checked against the *whole* trimmed expression.
_DIVIDE = re.compile(r"^DIVIDE\s*\(\s*\[([^\]]+)\]\s*,\s*\[([^\]]+)\]\s*(?:,[^)]*)?\)$", re.I)
_SUBTRACT = re.compile(r"^\[([^\]]+)\]\s*-\s*\[([^\]]+)\]$")
_ADD = re.compile(r"^\[([^\]]+)\]\s*\+\s*\[([^\]]+)\]$")
_MULTIPLY = re.compile(r"^\[([^\]]+)\]\s*\*\s*\[([^\]]+)\]$")


@dataclass
class ConsistencyRule:
    """``target`` must equal ``left <op> right`` when both are computed."""

    target: str
    left: str
    right: str
    op: str                          # "divide" | "subtract" | "add" | "multiply"
    # True when the target's format string renders it as a percentage. DAX
    # returns the raw ratio (0.112) while the displayed/queried value is 11.2,
    # so the expected value must be scaled by 100 before comparing.
    target_is_percent: bool = False

    @property
    def scale(self) -> float:
        return 100.0 if self.target_is_percent else 1.0

    def describe(self) -> str:
        symbol = {"divide": "/", "subtract": "-", "add": "+", "multiply": "*"}[self.op]
        suffix = " x100 (percentage format)" if self.target_is_percent else ""
        return f"[{self.target}] = [{self.left}] {symbol} [{self.right}]{suffix}"

    def apply(self, left: float, right: float) -> float | None:
        """Expected value of ``target``, scaled to how the measure is displayed."""
        if self.op == "divide":
            if right == 0:
                return None
            value = left / right
        elif self.op == "subtract":
            value = left - right
        elif self.op == "add":
            value = left + right
        else:
            value = left * right
        return value * self.scale


def measure_references(dax: str) -> list[str]:
    """Return the measure names referenced by *dax* (``[Name]`` tokens).

    Column references (``Table[Column]``) are excluded — only a bracket that is
    NOT preceded by a table name is a measure reference.
    """
    refs: list[str] = []
    for m in _MEASURE_REF.finditer(dax or ""):
        start = m.start()
        preceding = (dax[:start].rstrip())[-1:] if start else ""
        # Table[Column] -> preceded by an identifier char or a closing quote.
        if preceding and (preceding.isalnum() or preceding in "_'"):
            continue
        name = m.group(1).strip()
        if name and name not in refs:
            refs.append(name)
    return refs


def extract_consistency_rules(measures) -> list[ConsistencyRule]:
    """Derive checkable arithmetic rules from a list of Measure objects."""
    known = {m.name.casefold() for m in measures if m.name}
    percent = {
        m.name.casefold() for m in measures
        if m.name and "%" in (m.format_string or "")
    }
    rules: list[ConsistencyRule] = []
    for m in measures:
        expr = (m.dax_expression or "").strip()
        if not expr or not m.name:
            continue
        # Collapse whitespace/newlines so multi-line DAX still matches.
        flat = " ".join(expr.split())
        for pattern, op in (
            (_DIVIDE, "divide"), (_SUBTRACT, "subtract"),
            (_ADD, "add"), (_MULTIPLY, "multiply"),
        ):
            hit = pattern.match(flat)
            if not hit:
                continue
            left, right = hit.group(1).strip(), hit.group(2).strip()
            # Both operands must themselves be measures we can compute.
            if left.casefold() in known and right.casefold() in known:
                rules.append(ConsistencyRule(
                    m.name, left, right, op,
                    target_is_percent=m.name.casefold() in percent,
                ))
            break
    return rules


# --- Power BI format strings -----------------------------------------------
_CURRENCY_WORDS = (
    "sales", "cost", "profit", "revenue", "amount", "price", "value",
    "discount", "margin$", "spend", "budget",
)
_COUNT_FUNCS = ("COUNTROWS", "DISTINCTCOUNT", "COUNT(")
_COUNT_WORDS = ("count", "quantity", "orders", "customers", "resellers", "lines", "units")


def _scales_to_percent(dax: str) -> bool:
    """True when the measure itself already multiplies by 100.

    ``DIVIDE(a, b, 0) * 100`` returns 35 for a 35% rate, so the value is
    already in percentage points. Only a trailing top-level ``* 100`` counts —
    a 100 buried inside an argument means something else entirely.
    """
    text = " ".join((dax or "").split())
    if not text:
        return False
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch in ")]":
            depth += 1
        elif ch in "([":
            depth -= 1
        elif ch == "*" and depth == 0:
            tail = text[i + 1:].strip()
            try:
                return float(tail) == 100.0
            except ValueError:
                return False
    return False


def infer_format_string(name: str, dax: str) -> str:
    """Guess a Power BI format string when the model does not supply one.

    ``pbixray`` cannot read measure ``formatString`` from a binary ``.pbix``, so
    without this every KPI would be compared as a bare float and the results
    grid would show ``51878274.54`` where the dashboard shows ``$51,878,275``.
    The guess is deliberately conservative: currency, percentage or integer.
    """
    n = (name or "").casefold()
    d = (dax or "").upper()

    # A percentage is signalled by the name or by a ratio-shaped DAX.
    if "%" in n or "percent" in n or "rate" in n or (
        d.startswith("DIVIDE") and ("margin" in n or "ratio" in n or "share" in n)
    ):
        # A measure that already multiplies by 100 is expressed in percentage
        # points, not as a ratio. Applying a "%" format would scale it again —
        # a 35% rate rendered as 3500.0%.
        return '0.0"%"' if _scales_to_percent(dax) else "0.0%"

    if any(f in d for f in _COUNT_FUNCS) or any(w in n for w in _COUNT_WORDS):
        return "#,0"

    if any(w.rstrip("$") in n for w in _CURRENCY_WORDS):
        # Averages of money keep cents; totals do not.
        return "\\$#,0.00" if n.startswith(("avg", "average")) else "\\$#,0"

    return "#,0.00"


def apply_format(value: float, format_string: str) -> str:
    """Render *value* the way Power BI would, per its format string.

    Turns the computed number into the string the dashboard actually shows
    (``51879473.12`` -> ``$51,879,473``), so the expected value handed to the
    SQL generator and shown in the results grid is the *dashboard reading*,
    not a raw float.

    Only the common numeric formats are honoured; anything unrecognised falls
    back to a plain number rather than guessing.
    """
    fs = (format_string or "").replace("\\", "").strip()
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if not fs:
        return f"{number:,.2f}".rstrip("0").rstrip(".")

    # Percentages. A quoted "%" is a literal suffix: the measure already
    # multiplies by 100, so scaling again turns a 35% rate into 3500%.
    if '"%"' in fs:
        plain = fs.replace('"%"', "")
        decimals = len(plain.split(".")[-1]) if "." in plain else 0
        return f"{number:.{decimals}f}%"
    if "%" in fs:
        decimals = len(fs.split(".")[-1].replace("%", "")) if "." in fs else 0
        return f"{number * 100:.{decimals}f}%"

    decimals = len(fs.split(".")[-1]) if "." in fs else 0
    thousands = "," if "," in fs else ""
    body = f"{number:{thousands}.{decimals}f}"
    return f"${body}" if "$" in fs else body


def describe_format(format_string: str) -> str:
    """Turn a Power BI format string into plain guidance for SQL formatting.

    Examples: ``\\$#,0`` -> "currency, thousands separator, no decimals";
    ``0.0%`` -> "percentage with 1 decimal (value x100 then '%')".
    """
    fs = (format_string or "").strip()
    if not fs:
        return ""
    clean = fs.replace("\\", "")
    parts: list[str] = []
    if "$" in clean:
        parts.append("currency prefix '$'")
    if '"%"' in clean:
        parts.append("append '%' — the measure already multiplies by 100")
    elif "%" in clean:
        parts.append("percentage: multiply by 100 and append '%'")
    if "," in clean:
        parts.append("thousands separator")
    if "." in clean:
        decimals = len(clean.split(".")[-1].replace("%", "").replace("0", "0"))
        decimals = clean.split(".")[-1].count("0")
        parts.append(f"{decimals} decimal place(s)")
    elif "%" not in clean:
        parts.append("0 decimal places")
    return f"{fs} -> " + ", ".join(parts) if parts else fs
