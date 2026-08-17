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
           "check_dataset"]

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
        TestStatus.WARNING,
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
    missing: list[str] = []

    for quoted, bare, column in _REF.findall(dax):
        table = (quoted or bare or "").strip().casefold()
        column = column.strip().casefold()
        if not table:
            continue
        if table not in tables:
            missing.append(f"table '{quoted or bare}'")
        elif column not in tables[table]:
            # A measure may be written Table[Measure]; that is still resolvable.
            if column not in measures:
                missing.append(f"{quoted or bare}[{column}]")

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
            TestStatus.WARNING,
            "Both endpoints exist but the relationship is inactive, so it "
            "propagates no filter unless USERELATIONSHIP invokes it.",
            detail,
        )
    return CheckResult(
        TestStatus.PASS,
        f"Both endpoints exist; active, {relationship.cardinality or 'cardinality unstated'}.",
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
            TestStatus.WARNING,
            f"{len(columns)} column(s) in the model. No datasource schema was "
            "read, so the source side could not be checked.",
            f"{len(columns)} columns",
        )

    from src.services.validation.column_mapper import map_table_to_dataset

    source_tables = {t.full_name: [c.name for c in t.columns]
                     for t in (db_schema.tables or [])}
    match, score = (map_table_to_dataset([c.name for c in columns], source_tables)
                    if source_tables else ("", 0.0))
    if not match:
        return CheckResult(
            TestStatus.WARNING,
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
