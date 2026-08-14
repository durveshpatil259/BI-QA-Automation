"""Platform-neutral report model.

The QA engine reasons about pages, visuals, dimensions and measures. None of
that is specific to Power BI, yet the engine reads ``DashboardMetadata``, whose
vocabulary is Power BI's: ``clusteredBarChart``, ``cardVisual``, ``slicer``.
Adding Tableau would mean either teaching every rule a second vocabulary or
pretending Tableau visuals are Power BI ones.

This is the shared vocabulary instead. A platform adapter produces a
:class:`BIReport`; the engine consumes it and never learns a platform name.

The classification is the load-bearing part. "Is this a data visual?" decides
whether a chart test is worth generating at all — on a real report, 37 of 58
visuals were cards, slicers, textboxes and buttons, and generating chart tests
for them produced two thirds of a test suite that proved nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.constants import BIPlatform
from src.domain.serialization import SerializableMixin

__all__ = ["VisualKind", "BIVisual", "BIPage", "BIReport"]


class VisualKind:
    """Normalised visual categories, shared across platforms.

    Deliberately coarse: the engine's rules differ by *what a visual proves*,
    not by its exact rendering. A clustered bar and a stacked bar are validated
    identically — one grouped query compared row by row.
    """

    KPI = "kpi"                  # single number: card, KPI, gauge
    CATEGORICAL = "categorical"  # bar/column/pie/donut/treemap/funnel
    TIMESERIES = "timeseries"    # line/area/combo over a date axis
    TABULAR = "tabular"          # table, matrix, pivot
    MAP = "map"                  # any geographic visual
    SCATTER = "scatter"          # scatter/bubble
    FILTER = "filter"            # slicer, parameter control
    DECORATION = "decoration"    # textbox, image, shape, button
    OTHER = "other"

    #: Kinds that plot data and are therefore worth a data validation. A KPI is
    #: excluded because it is validated as a KPI, not as a chart; a filter is
    #: validated as a slicer. Testing them twice is the duplication this model
    #: exists to prevent.
    DATA_KINDS = frozenset({CATEGORICAL, TIMESERIES, TABULAR, MAP, SCATTER})


#: Substring -> kind. Matched case-insensitively against the platform's own
#: visual type, longest first so "lineclusteredcolumncombochart" beats "line".
_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("multirowcard", VisualKind.TABULAR),
    ("cardvisual", VisualKind.KPI),
    ("kpi", VisualKind.KPI),
    ("gauge", VisualKind.KPI),
    ("card", VisualKind.KPI),
    ("slicer", VisualKind.FILTER),
    ("parameter", VisualKind.FILTER),
    ("textbox", VisualKind.DECORATION),
    ("actionbutton", VisualKind.DECORATION),
    ("button", VisualKind.DECORATION),
    ("image", VisualKind.DECORATION),
    ("shape", VisualKind.DECORATION),
    ("scatter", VisualKind.SCATTER),
    ("bubble", VisualKind.SCATTER),
    ("map", VisualKind.MAP),
    ("choropleth", VisualKind.MAP),
    ("filled", VisualKind.MAP),
    ("pivottable", VisualKind.TABULAR),
    ("matrix", VisualKind.TABULAR),
    ("tableex", VisualKind.TABULAR),
    ("table", VisualKind.TABULAR),
    ("crosstab", VisualKind.TABULAR),
    ("combochart", VisualKind.TIMESERIES),
    ("linechart", VisualKind.TIMESERIES),
    ("areachart", VisualKind.TIMESERIES),
    ("line", VisualKind.TIMESERIES),
    ("area", VisualKind.TIMESERIES),
    ("waterfall", VisualKind.CATEGORICAL),
    ("funnel", VisualKind.CATEGORICAL),
    ("treemap", VisualKind.CATEGORICAL),
    ("donut", VisualKind.CATEGORICAL),
    ("pie", VisualKind.CATEGORICAL),
    ("column", VisualKind.CATEGORICAL),
    ("bar", VisualKind.CATEGORICAL),
    ("histogram", VisualKind.CATEGORICAL),
)


def classify_visual(visual_type: str) -> str:
    """Map a platform's visual type onto a :class:`VisualKind`.

    Unknown types become ``OTHER`` rather than being guessed into a data kind:
    a custom visual validated as if it were a bar chart would produce a
    confident, wrong comparison.
    """
    text = (visual_type or "").casefold().replace("_", "").replace(" ", "")
    if not text:
        return VisualKind.OTHER
    for hint, kind in _KIND_HINTS:
        if hint in text:
            return kind
    return VisualKind.OTHER


@dataclass
class BIVisual(SerializableMixin):
    """One visual, in platform-neutral terms."""

    id: str = ""
    title: str = ""
    kind: str = VisualKind.OTHER
    #: The platform's own type, kept for the report so a reader can trace back.
    native_type: str = ""
    page: str = ""
    dimensions: list[str] = field(default_factory=list)   # grouping fields
    measures: list[str] = field(default_factory=list)     # aggregated fields
    filters: list[str] = field(default_factory=list)

    @property
    def is_data_visual(self) -> bool:
        """True when a grouped data validation would prove something."""
        return self.kind in VisualKind.DATA_KINDS

    @property
    def fields(self) -> list[str]:
        return list(self.dimensions) + list(self.measures)


@dataclass
class BIPage(SerializableMixin):
    """One page/sheet/dashboard tab."""

    name: str = ""
    display_name: str = ""
    ordinal: int = 0
    visuals: list[BIVisual] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    hidden: bool = False
    is_tooltip: bool = False
    is_drillthrough: bool = False


@dataclass
class BIReport(SerializableMixin):
    """A dashboard from any BI platform, in one shape."""

    platform: BIPlatform = BIPlatform.POWER_BI
    report_name: str = ""
    source_file: str = ""
    pages: list[BIPage] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    #: Pages reachable by a navigation action, for navigation tests.
    navigation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def visuals(self) -> list[BIVisual]:
        return [v for page in self.pages for v in page.visuals]

    @property
    def data_visuals(self) -> list[BIVisual]:
        """Visuals worth a grouped data validation."""
        return [v for v in self.visuals if v.is_data_visual]

    def counts(self) -> dict[str, int]:
        return {
            "pages": len(self.pages),
            "visuals": len(self.visuals),
            "data_visuals": len(self.data_visuals),
            "measures": len(self.measures),
            "tables": len(self.tables),
            "filters": len(self.filters),
        }

    # --- adapters ---------------------------------------------------------
    @classmethod
    def from_power_bi(cls, metadata) -> "BIReport":
        """Normalise Power BI metadata without changing or losing it.

        A view over the existing extraction rather than a replacement: the
        Power BI path keeps working exactly as it does, and the engine gains a
        vocabulary a Tableau adapter can also speak.
        """
        if metadata is None:
            return cls()

        report = cls(
            platform=getattr(metadata, "platform", BIPlatform.POWER_BI),
            report_name=getattr(metadata, "model_name", "") or "",
            source_file=getattr(metadata, "source_file", "") or "",
            measures=[m.name for m in metadata.all_measures if m.name],
            tables=[t.name for t in metadata.tables if t.name],
            relationships=[
                {"from_table": r.from_table, "from_column": r.from_column,
                 "to_table": r.to_table, "to_column": r.to_column,
                 "cardinality": r.cardinality, "active": r.is_active}
                for r in (metadata.relationships or [])
            ],
            filters=[f.name or f"{f.target_table}[{f.target_column}]"
                     for f in (metadata.report_level_filters or [])],
            warnings=list(getattr(metadata, "extraction_warnings", []) or []),
        )

        measure_names = {m.casefold() for m in report.measures}
        for page in (metadata.pages or []):
            visuals = []
            for v in (page.visuals or []):
                kind = classify_visual(v.visual_type)
                # A field is a measure when the model defines one by that name;
                # everything else groups. Power BI writes both into one list.
                dims, meas = [], []
                for raw in (v.fields or []):
                    leaf = str(raw).split(".")[-1].strip("[]")
                    (meas if leaf.casefold() in measure_names else dims).append(raw)
                visuals.append(BIVisual(
                    id=v.id, title=v.title, kind=kind, native_type=v.visual_type,
                    page=page.display_name or page.name,
                    dimensions=dims, measures=meas,
                    filters=[f.name for f in (v.filters or []) if f.name],
                ))
            report.pages.append(BIPage(
                name=page.name,
                display_name=page.display_name or page.name,
                ordinal=page.ordinal,
                visuals=visuals,
            ))
        return report
