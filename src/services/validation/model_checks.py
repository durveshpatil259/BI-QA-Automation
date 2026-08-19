"""Deterministic checks over the semantic model itself.

The developer suite used to be a checklist: "review the DAX", "verify the
relationship". Written that way it can only ever be marked Not Executed, which
reports a permanent 0% for a suite that in fact holds the most mechanically
checkable facts in the whole model.

Most of these questions have an answer already sitting on disk. A measure
either references a column that exists or it does not. A relationship either
has both endpoints or it does not. The model evaluation stage already produced
a value for every measure it could compute. None of this needs an AI, and none
of it needs Power BI open.

What genuinely cannot be decided here — whether a *correct-looking* measure
encodes the business rule someone intended — stays manual, and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.constants import TestStatus
from src.core.logger import get_logger

_logger = get_logger()

__all__ = ["CheckResult", "check_measure", "check_dax", "check_relationship",
           "check_dataset", "check_binding"]

#: ``Table[Column]`` inside a DAX expression.
_REF = re.compile(r"(?:'([^']+)'|(\w+))\s*\[\s*([^\]]+?)\s*\]")
#: A bare measure reference, ``[Total Sales]``.
_MEASURE_REF = re.compile(r"(?<![\w\]'])\[([^\]]+)\]")


@dataclass
class CheckResult:
    status: TestStatus
    remark: str
    actual: str = ""


def _names(metadata):
    """(table -> {columns}, {measures}) folded for comparison."""
    tables: dict[str, set[str]] = {}
    measures: set[str] = set()
    for table in (metadata.tables if metadata else []):
        tables[(table.name or "").casefold()] = {
            (c.name or "").casefold() for c in table.columns
        }
        for m in table.measures:
            measures.add((m.name or "").casefold())
    return tables, measures


def check_measure(measure, dax_values: dict) -> CheckResult:
    """Did this measure actually produce a value from the model's own data?

    The evaluation stage computes every measure it can from the data inside the
    file. A measure that yields a number is demonstrably calculable; one that
    does not is either unsupported DAX or genuinely broken, and the difference
    matters enough to report separately.
    """
    name = (measure.name or "").strip()
    value = None
    for key, candidate in (dax_values or {}).items():
        if str(key).casefold() == name.casefold():
            value = candidate
            break

    if value not in (None, ""):
        return CheckResult(
            TestStatus.PASS,
            "Evaluated from the model's own data.",
            str(value),
        )
    if not (measure.dax_expression or "").strip():
        return CheckResult(
            TestStatus.FAIL, "The measure has no DAX expression.", "")
    return CheckResult(
        TestStatus.BLOCKED,
        "Could not be evaluated from the file — the DAX is outside the "
        "evaluator's grammar, so correctness must be confirmed by hand.",
        "",
    )


def check_dax(measure, metadata) -> CheckResult:
    """Does every name the DAX references exist in the model?

    A reference to a column that was renamed or removed is a defect that shows
    up as a broken visual, and it is decidable by reading the model. This does
    not judge whether the formula is *right* — only that it can resolve.
    """
    dax = " ".join((measure.dax_expression or "").split())
    if not dax:
        return CheckResult(TestStatus.FAIL, "No DAX expression to check.", "")

    tables, measures = _names(metadata)
    if not any(tables.values()):
        # Some extractors return measures without ever listing columns. With no
        # column inventory every reference looks missing, which would fail every
        # measure in the model for a defect in the extractor, not the report.
        return CheckResult(
            TestStatus.BLOCKED,
            "The model metadata lists no columns, so references cannot be "
            "resolved against it.",
            dax[:90],
        )
    missing: list[str] = []

    for quoted, bare, column in _REF.findall(dax):
        # Compare folded, but report the name exactly as the author wrote it —
        # a developer searching their DAX for "sales[missing]" finds nothing.
        raw_table, raw_column = (quoted or bare or "").strip(), column.strip()
        table, column = raw_table.casefold(), raw_column.casefold()
        if not table:
            continue
        if table not in tables:
            missing.append(f"table '{raw_table}'")
        elif column not in tables[table]:
            # A measure may be written Table[Measure]; that is still resolvable.
            if column not in measures:
                missing.append(f"{raw_table}[{raw_column}]")

    # Bare [Measure] references must name a measure that exists.
    for ref in _MEASURE_REF.findall(re.sub(r"(?:'[^']+'|\w+)\s*\[[^\]]+\]", " ", dax)):
        if ref.strip().casefold() not in measures:
            missing.append(f"[{ref.strip()}]")

    if missing:
        unique = list(dict.fromkeys(missing))[:4]
        return CheckResult(
            TestStatus.FAIL,
            "References that do not exist in the model: " + ", ".join(unique),
            dax[:90],
        )
    return CheckResult(
        TestStatus.PASS,
        "Every table, column and measure referenced exists in the model. "
        "Business correctness still needs a human.",
        dax[:90],
    )


def check_relationship(relationship, metadata) -> CheckResult:
    """Do both endpoints of the relationship exist, and is it active?"""
    tables, _ = _names(metadata)
    problems = []
    for table, column, side in (
        (relationship.from_table, relationship.from_column, "from"),
        (relationship.to_table, relationship.to_column, "to"),
    ):
        key = (table or "").casefold()
        if key not in tables:
            problems.append(f"{side} table '{table}' does not exist")
        elif (column or "").casefold() not in tables[key]:
            problems.append(f"{side} column '{table}[{column}]' does not exist")

    detail = (f"{relationship.from_table}[{relationship.from_column}] -> "
              f"{relationship.to_table}[{relationship.to_column}]")
    if problems:
        return CheckResult(TestStatus.FAIL, "; ".join(problems), detail)
    if not relationship.is_active:
        return CheckResult(
            TestStatus.PASS,
            "Both endpoints exist. The relationship is inactive, so it "
            "propagates no filter unless USERELATIONSHIP invokes it — normal "
            "for a role-playing dimension, worth a look if it was not intended.",
            detail,
        )
    return CheckResult(
        TestStatus.PASS,
        f"Both endpoints exist; active, {relationship.cardinality or 'cardinality unstated'}.",
        detail,
    )


#: An aggregation wrapper around a field, e.g. ``Sum(Sales.Amount)``.
_AGG = re.compile(r"^\s*\w+\s*\(\s*(.+?)\s*\)\s*$")


def _binding_parts(field: str) -> tuple[str, str]:
    """Split a report's field reference into (table, column).

    Report layouts do not write DAX. A binding arrives as ``Sales.Total Profit``,
    wrapped in its aggregate as ``Sum(Sales.Sales Amount)``, or extended down a
    date hierarchy as ``Date.Date.Variation.Date Hierarchy.Year``. Reading any
    of those literally finds no such column and reports a healthy visual as
    broken, which is exactly what happened before this existed.
    """
    text = field.strip()
    while (agg := _AGG.match(text)):
        text = agg.group(1).strip()

    match = _REF.fullmatch(text)
    if match:
        quoted, bare, column = match.groups()
        return (quoted or bare or "").strip(), column.strip()

    if "." in text:
        # The first two segments are table and column; anything after them is
        # hierarchy navigation, which resolves through the column itself.
        table, _, rest = text.partition(".")
        return table.strip(), rest.partition(".")[0].strip()
    return "", text


def check_binding(fields, metadata) -> CheckResult:
    """Does every field the visual binds to still exist in the model?

    This is the check that catches a renamed or deleted column after someone
    edits the model: the visual keeps its binding, and the report breaks. It is
    answerable by name lookup, which is why it belongs here and not on a
    reviewer's checklist.
    """
    wanted = [str(f).strip() for f in (fields or []) if str(f).strip()]
    if not wanted:
        return CheckResult(
            TestStatus.NOT_EXECUTED,
            "The extraction recorded no field bindings for this visual, so "
            "there is nothing to resolve.",
            "",
        )

    tables, measures = _names(metadata)
    every_column = {c for cols in tables.values() for c in cols}
    if not every_column and not measures:
        return CheckResult(
            TestStatus.BLOCKED,
            "The model metadata lists no columns or measures, so bindings "
            "cannot be resolved against it.",
            ", ".join(wanted[:5]),
        )
    unresolved = []
    for field in wanted:
        table, name = _binding_parts(field)
        table, name = table.casefold(), name.casefold()
        if table in tables and (name in tables[table] or name in measures):
            continue
        # Unqualified, or qualified by a table the extractor named differently:
        # the name still counts as resolved if the model has it somewhere.
        if name in every_column or name in measures:
            continue
        unresolved.append(field)

    detail = ", ".join(wanted[:5])
    if unresolved:
        return CheckResult(
            TestStatus.FAIL,
            "Bound to field(s) that do not exist in the model: "
            + ", ".join(unresolved[:4]),
            detail,
        )
    return CheckResult(
        TestStatus.PASS,
        f"All {len(wanted)} bound field(s) resolve against the model.",
        detail,
    )


def check_dataset(table, db_schema) -> CheckResult:
    """Does the model table have columns, and can it be found in the source?"""
    columns = [c for c in table.columns if (c.name or "").strip()]
    if not columns:
        return CheckResult(
            TestStatus.FAIL, "The table has no columns in the model.", "0 columns")

    if db_schema is None:
        return CheckResult(
            TestStatus.BLOCKED,
            f"{len(columns)} column(s) in the model. No datasource schema was "
            "read, so the source side could not be checked.",
            f"{len(columns)} columns",
        )

    from src.services.validation.column_mapper import map_table_to_dataset

    source_tables = {t.full_name: [c.name for c in t.columns]
                     for t in (db_schema.tables or [])}
    match, score = (map_table_to_dataset([c.name for c in columns], source_tables)
                    if source_tables else ("", 0.0))
    if getattr(table, "is_calculated", False):
        return CheckResult(
            TestStatus.PASS,
            f"{len(columns)} column(s); calculated table, so it is defined by "
            "DAX and has no source table to match.",
            f"{len(columns)} columns (calculated)",
        )
    if not match:
        return CheckResult(
            TestStatus.BLOCKED,
            f"{len(columns)} column(s) in the model, but no source table shares "
            "enough of them to be identified as its origin.",
            f"{len(columns)} columns",
        )
    return CheckResult(
        TestStatus.PASS,
        f"{len(columns)} column(s); backed by {match} in the datasource "
        f"({score:.0%} of its columns matched).",
        f"{len(columns)} columns -> {match}",
    )
