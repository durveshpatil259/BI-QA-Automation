"""Parse TMDL (Tabular Model Definition Language) used by PBIP semantic models.

TMDL is an indentation-based text format. One file per table under
``definition/tables/<Table>.tmdl`` plus ``definition/relationships.tmdl``.
This is a pragmatic, defensive parser covering the constructs the product needs:
tables, columns (incl. calculated), measures (incl. multi-line DAX), calculated
tables, and relationships. Unrecognised lines are ignored rather than fatal.
"""

from __future__ import annotations

import re

from src.domain.models import Column, Measure, Relationship, Table

# Property keys that terminate a multi-line DAX expression block.
_PROPERTY_KEYS = {
    "datatype", "formatstring", "displayfolder", "lineagetag", "summarizeby",
    "mode", "ishidden", "iskey", "isnullable", "sortbycolumn", "datacategory",
    "annotation", "changedproperty", "description", "source", "relatedcolumndetails",
    "encoding", "dataaccessoptions", "querygroup", "isavailableinmdx",
}

_OBJECT_KEYWORDS = ("column", "measure", "partition", "hierarchy", "calculationgroup")


def _indent(line: str) -> int:
    """Indentation depth in tab-equivalents (4 spaces == 1 tab)."""
    n = 0
    for ch in line:
        if ch == "\t":
            n += 1
        elif ch == " ":
            n += 1  # count spaces; compared relatively so unit need not be exact
        else:
            break
    return n


def _strip_quotes(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def _split_name_expr(remainder: str) -> tuple[str, str]:
    """Split ``Name = expr`` / ``'My Name' = expr`` into (name, inline_expr)."""
    remainder = remainder.strip()
    # Match a possibly-quoted name, then optional '= expr'.
    m = re.match(r"^('(?:[^']|'')*'|[^=\s]+)\s*(?:=\s*(.*))?$", remainder)
    if not m:
        return _strip_quotes(remainder), ""
    return _strip_quotes(m.group(1)), (m.group(2) or "").strip()


def _is_property_line(stripped: str) -> bool:
    key = re.split(r"[:\s]", stripped, 1)[0].lower()
    return key in _PROPERTY_KEYS


def _get_property(lines: list[str], key: str) -> str:
    key_l = key.lower()
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith(key_l):
            rest = s[len(key):].lstrip()
            if rest.startswith(":"):
                return rest[1:].strip()
    return ""


def _has_flag(lines: list[str], flag: str) -> bool:
    return any(ln.strip().lower() == flag.lower() for ln in lines)


def parse_table_tmdl(text: str) -> Table | None:
    """Parse one table's TMDL text into a :class:`Table`."""
    raw_lines = [ln for ln in text.splitlines()]
    # Locate the `table <Name>` header.
    header_idx = next(
        (i for i, ln in enumerate(raw_lines) if ln.strip().lower().startswith("table ")),
        None,
    )
    if header_idx is None:
        return None

    header = raw_lines[header_idx].strip()[len("table "):]
    table_name, table_expr = _split_name_expr(header)
    table = Table(name=table_name)
    if table_expr:  # `table X = <expr>` is a calculated table
        table.is_calculated = True
        table.dax_expression = table_expr

    body = raw_lines[header_idx + 1:]
    i = 0
    n = len(body)
    while i < n:
        line = body[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        low = stripped.lower()
        obj_indent = _indent(line)

        if low.startswith("column "):
            name, expr = _split_name_expr(stripped[len("column "):])
            block, i = _collect_block(body, i + 1, obj_indent, expr == "")
            expr = expr or _dedent_join(block["expr_lines"])
            table.columns.append(Column(
                name=name,
                data_type=_get_property(block["prop_lines"], "dataType"),
                is_hidden=_has_flag(block["prop_lines"], "isHidden"),
                is_calculated=bool(expr),
                dax_expression=expr,
                format_string=_get_property(block["prop_lines"], "formatString"),
                description=_get_property(block["prop_lines"], "description"),
            ))
            continue

        if low.startswith("measure "):
            name, expr = _split_name_expr(stripped[len("measure "):])
            block, i = _collect_block(body, i + 1, obj_indent, expr == "")
            expr = expr or _dedent_join(block["expr_lines"])
            table.measures.append(Measure(
                name=name,
                table=table_name,
                dax_expression=expr,
                format_string=_get_property(block["prop_lines"], "formatString"),
                display_folder=_get_property(block["prop_lines"], "displayFolder"),
                is_hidden=_has_flag(block["prop_lines"], "isHidden"),
                description=_get_property(block["prop_lines"], "description"),
            ))
            continue

        if low.startswith("partition "):
            _, part_expr = _split_name_expr(stripped[len("partition "):])
            block, i = _collect_block(body, i + 1, obj_indent, False)
            if part_expr.lower() == "calculated" or _get_property(
                block["prop_lines"], "source"
            ).lower().startswith("calculated"):
                table.is_calculated = True
            source = _get_property(block["prop_lines"], "source")
            if source and not table.is_calculated and not table.source_query:
                table.source_query = source
            continue

        i += 1

    return table


def _collect_block(
    body: list[str], start: int, obj_indent: int, expect_expr: bool
) -> tuple[dict, int]:
    """Collect the lines belonging to an object (deeper indent than its header).

    Splits them into DAX expression continuation lines and property lines.
    Returns (block, next_index)."""
    expr_lines: list[str] = []
    prop_lines: list[str] = []
    i = start
    n = len(body)
    seen_property = False
    while i < n:
        line = body[i]
        stripped = line.strip()
        if stripped and _indent(line) <= obj_indent:
            break  # dedent -> object ended
        if not stripped:
            i += 1
            continue
        if _is_property_line(stripped):
            seen_property = True
            prop_lines.append(line)
        elif expect_expr and not seen_property:
            expr_lines.append(line)
        else:
            prop_lines.append(line)
        i += 1
    return {"expr_lines": expr_lines, "prop_lines": prop_lines}, i


def _dedent_join(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(ln.strip() for ln in lines).strip()


# --- relationships ---------------------------------------------------------
def _split_table_column(ref: str) -> tuple[str, str]:
    """Split ``Table.Column`` / ``'My Table'.Column`` into (table, column)."""
    ref = ref.strip()
    if ref.startswith("'"):
        end = ref.find("'", 1)
        while end != -1 and end + 1 < len(ref) and ref[end + 1] == "'":
            end = ref.find("'", end + 2)
        if end != -1:
            table = _strip_quotes(ref[: end + 1])
            column = ref[end + 1:].lstrip(".").strip()
            return table, column
    if "." in ref:
        table, _, column = ref.rpartition(".")
        return _strip_quotes(table), column.strip()
    return "", ref


def parse_relationships_tmdl(text: str) -> list[Relationship]:
    relationships: list[Relationship] = []
    current: dict[str, str] | None = None

    def flush():
        if not current:
            return
        ft, fc = _split_table_column(current.get("fromColumn", ""))
        tt, tc = _split_table_column(current.get("toColumn", ""))
        xf = current.get("crossFilteringBehavior", "").lower()
        relationships.append(Relationship(
            from_table=ft, from_column=fc, to_table=tt, to_column=tc,
            cardinality=(
                f"{current.get('fromCardinality','many')}-to-"
                f"{current.get('toCardinality','one')}"
            ),
            cross_filter_direction={"bothdirections": "both", "onedirection": "single"}
            .get(xf, xf or "single"),
            is_active=current.get("isActive", "true").lower() != "false",
        ))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("relationship "):
            flush()
            current = {}
            continue
        if current is not None and ":" in stripped:
            key, _, val = stripped.partition(":")
            current[key.strip()] = val.strip()
    flush()
    return relationships
