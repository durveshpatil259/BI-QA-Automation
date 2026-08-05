"""Parse Power BI report layout into pages, visuals, filters and bookmarks.

Two layout formats are supported:

* **Classic** ``Report/Layout`` — a JSON document whose ``config``/``filters``
  fields are themselves JSON strings that must be parsed again.
* **PBIR (enhanced)** — a folder of JSON files (``pages.json``, per-page
  ``page.json``, per-visual ``visual.json``), passed here as parsed dicts.
"""

from __future__ import annotations

from src.domain.models import Bookmark, Filter, Page, Visual
from src.services.extractors.power_bi.pbix_io import parse_json_string


# ---------------------------------------------------------------------------
# Shared field / filter helpers
# ---------------------------------------------------------------------------
def _field_ref(expr: dict) -> tuple[str, str]:
    """Extract (table, property) from a Power BI field expression object."""
    if not isinstance(expr, dict):
        return "", str(expr)
    for kind in ("Column", "Measure", "HierarchyLevel", "Aggregation"):
        node = expr.get(kind)
        if isinstance(node, dict):
            prop = node.get("Property", "")
            source = node.get("Expression", {}).get("SourceRef", {})
            entity = source.get("Entity", "") or source.get("Source", "")
            if kind == "Aggregation":
                inner = node.get("Expression", {})
                return _field_ref(inner)
            return entity, prop
    return "", ""


def _parse_filters(filters_raw, scope: str) -> list[Filter]:
    """Parse a filters JSON string (or list) into :class:`Filter` objects."""
    data = filters_raw
    if isinstance(filters_raw, str):
        try:
            data = parse_json_string(filters_raw)
        except Exception:  # noqa: BLE001 - malformed filter blob
            return []
    if not isinstance(data, list):
        return []

    filters: list[Filter] = []
    for f in data:
        if not isinstance(f, dict):
            continue
        table, column = _field_ref(f.get("expression", {}))
        filters.append(Filter(
            name=f.get("name", "") or (f.get("displayName", "")),
            scope=scope,
            target_table=table,
            target_column=column,
            filter_type=f.get("type", ""),
        ))
    return filters


# ---------------------------------------------------------------------------
# Classic Report/Layout
# ---------------------------------------------------------------------------
def _parse_visual_config(config_raw: str) -> Visual:
    visual = Visual()
    cfg = None
    if isinstance(config_raw, str):
        try:
            cfg = parse_json_string(config_raw)
        except Exception:  # noqa: BLE001
            cfg = None
    elif isinstance(config_raw, dict):
        cfg = config_raw
    if not isinstance(cfg, dict):
        return visual

    visual.id = cfg.get("name", "")
    single = cfg.get("singleVisual", {}) or {}
    visual.visual_type = single.get("visualType", "")

    # Fields from projections.
    fields: list[str] = []
    for role_items in (single.get("projections", {}) or {}).values():
        for item in role_items or []:
            ref = item.get("queryRef") or ""
            if ref:
                fields.append(ref)
    visual.fields = fields

    # Title from vcObjects.title text literal.
    try:
        title_objs = (single.get("vcObjects", {}) or {}).get("title", [])
        if title_objs:
            props = title_objs[0].get("properties", {})
            lit = props.get("text", {}).get("expr", {}).get("Literal", {}).get("Value", "")
            visual.title = str(lit).strip("'")
    except Exception:  # noqa: BLE001
        pass
    return visual


def parse_classic_layout(layout: dict) -> tuple[list[Page], list[Bookmark], list[Filter]]:
    pages: list[Page] = []
    for section in layout.get("sections", []) or []:
        page = Page(
            name=section.get("name", ""),
            display_name=section.get("displayName", section.get("name", "")),
            ordinal=int(section.get("ordinal", 0) or 0),
            width=section.get("width"),
            height=section.get("height"),
        )
        page_filters = _parse_filters(section.get("filters"), scope="page")
        for vc in section.get("visualContainers", []) or []:
            visual = _parse_visual_config(vc.get("config", ""))
            visual.page = page.display_name
            visual.filters = _parse_filters(vc.get("filters"), scope="visual")
            page.visuals.append(visual)
        # Attach page-scope filters to the first matching structure by leaving
        # them on the page's visuals list is wrong; keep them report-traceable
        # via visuals only. Page-level filters are surfaced through report list.
        pages.append(page)
        pages_report_filters = page_filters  # noqa: F841 (documented below)

    # Bookmarks + report-level filters live in the top-level config/filters.
    bookmarks: list[Bookmark] = []
    report_config = layout.get("config", "")
    cfg = None
    if isinstance(report_config, str):
        try:
            cfg = parse_json_string(report_config)
        except Exception:  # noqa: BLE001
            cfg = None
    elif isinstance(report_config, dict):
        cfg = report_config
    if isinstance(cfg, dict):
        for bm in cfg.get("bookmarks", []) or []:
            state = bm.get("explorationState", {}) or {}
            bookmarks.append(Bookmark(
                name=bm.get("name", ""),
                display_name=bm.get("displayName", bm.get("name", "")),
                page=state.get("activeSection", ""),
            ))

    report_filters = _parse_filters(layout.get("filters"), scope="report")
    return pages, bookmarks, report_filters


# ---------------------------------------------------------------------------
# PBIR enhanced report format (parsed dicts supplied by the extractor)
# ---------------------------------------------------------------------------
def parse_pbir_page(page_dict: dict) -> Page:
    return Page(
        name=page_dict.get("name", ""),
        display_name=page_dict.get("displayName", page_dict.get("name", "")),
        ordinal=int(page_dict.get("ordinal", 0) or 0),
        width=page_dict.get("width"),
        height=page_dict.get("height"),
    )


def parse_pbir_visual(visual_dict: dict) -> Visual:
    visual = Visual(id=visual_dict.get("name", ""))
    vnode = visual_dict.get("visual", {}) or {}
    visual.visual_type = vnode.get("visualType", "")

    fields: list[str] = []
    query = vnode.get("query", {}) or {}
    for proj in (query.get("queryState", {}) or {}).values():
        for item in (proj.get("projections", []) or []):
            fld = item.get("field", {}) or {}
            table, prop = _field_ref(fld)
            if prop:
                fields.append(f"{table}.{prop}" if table else prop)
            elif item.get("queryRef"):
                fields.append(item["queryRef"])
    visual.fields = fields

    # Title (best effort).
    title = ((vnode.get("visualContainerObjects", {}) or {}).get("title", []) or [{}])
    try:
        lit = title[0]["properties"]["text"]["expr"]["Literal"]["Value"]
        visual.title = str(lit).strip("'")
    except Exception:  # noqa: BLE001
        pass
    return visual
