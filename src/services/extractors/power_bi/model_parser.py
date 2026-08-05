"""Parse a Power BI TOM/TMSL model (``DataModelSchema`` / ``*.bim`` JSON).

The Tabular Object Model serialization stores the semantic model as JSON with a
top-level ``model`` object containing ``tables`` and ``relationships``. This
module maps that structure onto the neutral domain models, including calculated
columns/tables and DAX expressions.
"""

from __future__ import annotations

from src.domain.models import Column, Measure, Relationship, Table


def _expr_to_str(value) -> str:
    """TOM expressions may be a string or a list of lines; normalise to str."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _parse_columns(raw_columns: list[dict]) -> list[Column]:
    columns: list[Column] = []
    for rc in raw_columns or []:
        col_type = str(rc.get("type", "")).lower()
        is_calc = col_type == "calculated"
        columns.append(Column(
            name=rc.get("name", ""),
            data_type=rc.get("dataType", ""),
            is_hidden=bool(rc.get("isHidden", False)),
            is_calculated=is_calc,
            dax_expression=_expr_to_str(rc.get("expression")) if is_calc else "",
            description=_expr_to_str(rc.get("description")),
            format_string=rc.get("formatString", ""),
        ))
    return columns


def _parse_measures(table_name: str, raw_measures: list[dict]) -> list[Measure]:
    measures: list[Measure] = []
    for rm in raw_measures or []:
        measures.append(Measure(
            name=rm.get("name", ""),
            table=table_name,
            dax_expression=_expr_to_str(rm.get("expression")),
            data_type=rm.get("dataType", ""),
            format_string=rm.get("formatString", ""),
            is_hidden=bool(rm.get("isHidden", False)),
            description=_expr_to_str(rm.get("description")),
            display_folder=rm.get("displayFolder", ""),
        ))
    return measures


def _table_is_calculated(raw_table: dict) -> tuple[bool, str]:
    """Detect a calculated table via its partition source, returning (flag, dax)."""
    for part in raw_table.get("partitions", []) or []:
        source = part.get("source", {}) or {}
        if str(source.get("type", "")).lower() == "calculated":
            return True, _expr_to_str(source.get("expression"))
    return False, ""


def _partition_query(raw_table: dict) -> str:
    """Return the first partition's M/SQL source text, if any."""
    for part in raw_table.get("partitions", []) or []:
        source = part.get("source", {}) or {}
        expr = source.get("expression") or source.get("query")
        if expr:
            return _expr_to_str(expr)
    return ""


def _map_cardinality(rel: dict) -> str:
    frm = rel.get("fromCardinality")
    to = rel.get("toCardinality")
    if frm and to:
        return f"{frm}-to-{to}"
    # TOM default when unspecified.
    return "many-to-one"


def _map_cross_filter(rel: dict) -> str:
    mapping = {
        "onedirection": "single",
        "bothdirections": "both",
        "automatic": "automatic",
    }
    raw = str(rel.get("crossFilteringBehavior", "")).lower()
    return mapping.get(raw, raw or "single")


def parse_model(model_root: dict) -> tuple[list[Table], list[Relationship]]:
    """Parse a TOM document (the object containing a ``model`` key, or a bare
    ``model`` object) into tables and relationships."""
    model = model_root.get("model", model_root) if isinstance(model_root, dict) else {}

    tables: list[Table] = []
    for rt in model.get("tables", []) or []:
        name = rt.get("name", "")
        is_calc, calc_dax = _table_is_calculated(rt)
        tables.append(Table(
            name=name,
            is_hidden=bool(rt.get("isHidden", False)),
            is_calculated=is_calc,
            dax_expression=calc_dax,
            source_query="" if is_calc else _partition_query(rt),
            columns=_parse_columns(rt.get("columns", [])),
            measures=_parse_measures(name, rt.get("measures", [])),
            description=_expr_to_str(rt.get("description")),
        ))

    relationships: list[Relationship] = []
    for rr in model.get("relationships", []) or []:
        relationships.append(Relationship(
            from_table=rr.get("fromTable", ""),
            from_column=rr.get("fromColumn", ""),
            to_table=rr.get("toTable", ""),
            to_column=rr.get("toColumn", ""),
            cardinality=_map_cardinality(rr),
            cross_filter_direction=_map_cross_filter(rr),
            is_active=bool(rr.get("isActive", True)),
        ))

    return tables, relationships
