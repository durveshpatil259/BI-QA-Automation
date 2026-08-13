"""Does a slicer actually reach a given table?

Power BI relationships are directional. A ``M:1`` relationship with
``cross_filter='Single'`` carries a filter from the *one* side to the *many*
side only — never back again. So in a classic star schema:

    Date --(one)--> Sales      a Fiscal Year slicer DOES filter Sales
    Sales <--(many)-- Customer a Fiscal Year slicer does NOT reach Customer

That means ``DISTINCTCOUNT(Customer[Customer ID])`` shows the same number under
every year — which looks like a bug, and is in fact what Power BI renders.

Without this, the AI assumed every filter applies to every KPI and wrote a
``WHERE`` clause joining Customer through Sales to Date, computing "customers
who purchased in FY2018" (2,460) instead of the measure the dashboard defines
(18,485). Every one of those comparisons then failed against a correct dashboard.
"""

from __future__ import annotations

import re

__all__ = ["tables_in_dax", "reachable_tables", "filter_applies"]

#: ``Table[Column]`` or ``'Quoted Table'[Column]``.
_TABLE_REF = re.compile(r"'([^']+)'\s*\[|(\b\w+)\s*\[")


def tables_in_dax(expression: str) -> set[str]:
    """Model tables a DAX expression reads columns from."""
    found: set[str] = set()
    for quoted, bare in _TABLE_REF.findall(expression or ""):
        name = (quoted or bare).strip()
        if name:
            found.add(name)
    return found


def reachable_tables(metadata, source: str) -> set[str]:
    """Tables a filter placed on *source* propagates to.

    Traverses only in the direction Power BI actually propagates: from the
    ``1`` side to the ``M`` side, plus either direction when the relationship
    is bidirectional.
    """
    edges: dict[str, set[str]] = {}

    def link(a: str, b: str) -> None:
        edges.setdefault(a.casefold(), set()).add(b.casefold())

    for rel in (getattr(metadata, "relationships", None) or []):
        if not getattr(rel, "is_active", True):
            continue
        many, one = rel.from_table, rel.to_table
        cardinality = (rel.cardinality or "").upper().replace(" ", "")
        both = (rel.cross_filter_direction or "").casefold() == "both"

        if cardinality in ("M:1", "MANY:1", "*:1"):
            link(one, many)                 # the one side filters the many side
        elif cardinality in ("1:M", "1:MANY", "1:*"):
            link(many, one)
        else:                               # 1:1 — symmetric
            link(one, many)
            link(many, one)
        if both:
            link(many, one)
            link(one, many)

    seen = {source.casefold()}
    queue = [source.casefold()]
    while queue:
        for nxt in edges.get(queue.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def filter_applies(metadata, filter_table: str, dax_expression: str) -> bool:
    """True when a slicer on *filter_table* can affect this measure.

    Unknown shapes return True: assuming a filter applies matches the previous
    behaviour and keeps the WHERE clause, so a parsing gap cannot silently drop
    a filter that genuinely belongs.
    """
    if not filter_table or not dax_expression:
        return True
    referenced = tables_in_dax(dax_expression)
    if not referenced:
        return True

    relationships = getattr(metadata, "relationships", None) or []
    if not relationships:
        # A flat model, or extraction that found no relationships. With no
        # graph to reason over we cannot prove a filter is irrelevant — and
        # answering "no" here would strip the WHERE clause from every query.
        return True

    known: set[str] = set()
    for rel in relationships:
        known.add((rel.from_table or "").casefold())
        known.add((rel.to_table or "").casefold())

    # Only a table that participates in the graph can be reasoned about.
    if filter_table.casefold() not in known:
        return True
    if not any(t.casefold() in known for t in referenced):
        return True

    reach = reachable_tables(metadata, filter_table)
    return any(t.casefold() in reach for t in referenced)
