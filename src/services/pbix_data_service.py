"""Evaluate DAX measures against the data stored inside a PBIX.

``pbixray`` exposes not just the model definition but the **row data** of the
VertiPaq tables. That means the number a KPI card renders can be recomputed in
pandas directly from the file — no screenshot, no Power BI Desktop, no live
Analysis Services instance.

Scope, stated plainly: this is a *pragmatic* DAX subset, not a DAX engine.

Supported
    SUM, AVERAGE, MIN, MAX, COUNT, COUNTROWS, DISTINCTCOUNT over a column or
    table, plus measures derived from other measures via ``-``, ``+``, ``*``
    and ``DIVIDE(a, b)`` — resolved iteratively so chains chain correctly.

Not supported (reported as unevaluated, never guessed)
    CALCULATE with filter context, time intelligence (SAMEPERIODLASTYEAR, DATEADD),
    row-context iterators (SUMX/AVERAGEX), and anything else requiring a real
    evaluation engine.

Unsupported measures are simply absent from the result, so the validation
engine degrades to executability/consistency for them rather than comparing
against a fabricated number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.exceptions import MetadataExtractionError
from src.core.logger import get_logger
from src.domain.models import DashboardMetadata, Project
from src.storage import file_manager as fm
from src.storage.project_repository import ProjectRepository

_logger = get_logger()

# SUM(Table[Column]) / AVERAGE('My Table'[Col]) — table name may be quoted.
_AGG_COLUMN = re.compile(
    r"^(SUM|AVERAGE|AVG|MIN|MAX|COUNT|DISTINCTCOUNT)\s*\(\s*"
    r"'?([^'\[\]]+?)'?\s*\[\s*([^\]]+?)\s*\]\s*\)$",
    re.IGNORECASE,
)
# COUNTROWS(Table)
_AGG_TABLE = re.compile(r"^(COUNTROWS)\s*\(\s*'?([^'()\[\]]+?)'?\s*\)$", re.IGNORECASE)

# Derived-from-other-measures shapes.
_DIVIDE = re.compile(
    r"^DIVIDE\s*\(\s*\[([^\]]+)\]\s*,\s*\[([^\]]+)\]\s*(?:,[^)]*)?\)$", re.IGNORECASE
)
_BINARY = re.compile(r"^\[([^\]]+)\]\s*([-+*])\s*\[([^\]]+)\]$")


@dataclass
class MeasureValue:
    name: str
    value: float
    method: str          # "aggregate" | "derived"
    expression: str = ""


@dataclass
class FilterOption:
    """A slicer on the report and the distinct values it can take."""

    table: str
    column: str
    values: list[str]

    @property
    def field(self) -> str:
        return f"{self.table}[{self.column}]"


class PbixDataService:
    """Computes true measure values from the data inside a PBIX file."""

    def __init__(self, repository: ProjectRepository):
        self._repo = repository

    # --- slicer discovery -------------------------------------------------
    def detect_filters(
        self,
        project: Project,
        metadata: DashboardMetadata | None = None,
        *,
        max_values: int = 8,
    ) -> list[FilterOption]:
        """Return the report's slicers together with their real distinct values.

        The values come from the PBIX data itself, so every condition a user
        could select on the dashboard becomes a testable scenario — without a
        single screenshot.
        """
        metadata = metadata or self._repo.load_metadata(project)
        if metadata is None:
            return []

        wanted: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for visual in metadata.all_visuals:
            if (visual.visual_type or "").casefold() != "slicer" or not visual.fields:
                continue
            table, _, column = visual.fields[0].rpartition(".")
            key = (table.casefold(), column.casefold())
            if table and column and key not in seen:
                seen.add(key)
                wanted.append((table, column))
        if not wanted:
            return []

        model = self._open(project)
        if model is None:
            return []
        try:
            cache: dict[str, object] = {}
            options: list[FilterOption] = []
            for table, column in wanted:
                df = self._table(model, table, cache)
                if df is None or column not in df.columns:
                    continue
                try:
                    distinct = sorted(
                        str(v) for v in df[column].dropna().unique()
                    )
                except Exception:  # noqa: BLE001 - unhashable/odd dtypes
                    continue
                # A slicer over thousands of customers is not a useful test
                # axis; only low-cardinality slicers expand into scenarios.
                if 1 < len(distinct) <= max_values:
                    options.append(FilterOption(table, column, distinct))
            return options
        finally:
            self._close(model)

    # --- entry point ------------------------------------------------------
    def evaluate(
        self,
        project: Project,
        metadata: DashboardMetadata | None = None,
        *,
        filter_spec: tuple[str, str, str] | None = None,
    ) -> dict[str, str]:
        """Return ``{measure_name: value}`` for every measure we can compute.

        ``filter_spec`` is ``(table, column, value)`` — the equivalent of
        selecting that value in a slicer. Measures are then evaluated over the
        filtered data, exactly as Power BI would render them.
        """
        metadata = metadata or self._repo.load_metadata(project)
        if metadata is None or not metadata.all_measures:
            return {}

        model = self._open(project)
        if model is None:
            return {}
        try:
            values = self._evaluate_measures(model, metadata, filter_spec)
        finally:
            self._close(model)

        _logger.info(
            "Evaluated %d/%d measure(s) from PBIX data%s",
            len(values), len(metadata.all_measures),
            f" under {filter_spec[0]}[{filter_spec[1]}]={filter_spec[2]}"
            if filter_spec else "",
        )
        return {name: self._format(v.value) for name, v in values.items()}

    # --- pbixray lifecycle ------------------------------------------------
    def _open(self, project: Project):
        path = self._pbix_path(project)
        if path is None:
            _logger.info("No .pbix available; skipping data-backed evaluation.")
            return None
        try:
            from pbixray import PBIXRay
        except ImportError:
            _logger.info("pbixray not installed; skipping data-backed evaluation.")
            return None
        try:
            return PBIXRay(str(path))
        except Exception as exc:  # noqa: BLE001
            raise MetadataExtractionError(f"Could not open '{path.name}': {exc}") from exc

    @staticmethod
    def _close(model) -> None:
        close = getattr(model, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # --- evaluation -------------------------------------------------------
    # --- filter propagation ----------------------------------------------
    def _filtered_table(self, model, table_name: str, cache: dict, metadata, filter_spec):
        """Return *table_name* narrowed by a slicer selection.

        Mirrors how Power BI propagates a filter: if the slicer sits on the
        same table, filter directly; otherwise follow the relationship between
        the fact table and the dimension table and keep only matching keys.
        """
        df = self._table(model, table_name, cache)
        if df is None or filter_spec is None:
            return df

        dim_table, dim_column, value = filter_spec

        # Slicer is on this very table.
        if table_name.casefold() == dim_table.casefold():
            if dim_column not in df.columns:
                return df
            return df[df[dim_column].astype(str) == str(value)]

        dim_df = self._table(model, dim_table, cache)
        if dim_df is None or dim_column not in dim_df.columns:
            return df

        # Find the relationship joining this table to the dimension.
        for rel in (metadata.relationships if metadata else []):
            pairs = [
                (rel.from_table, rel.from_column, rel.to_table, rel.to_column),
                (rel.to_table, rel.to_column, rel.from_table, rel.from_column),
            ]
            for f_tbl, f_col, t_tbl, t_col in pairs:
                if (f_tbl.casefold() == table_name.casefold()
                        and t_tbl.casefold() == dim_table.casefold()
                        and f_col in df.columns and t_col in dim_df.columns):
                    keys = set(dim_df.loc[dim_df[dim_column].astype(str) == str(value), t_col])
                    return df[df[f_col].isin(keys)]

        # No path from this table to the slicer — the filter does not apply.
        return df

    def _evaluate_measures(
        self, model, metadata, filter_spec=None
    ) -> dict[str, MeasureValue]:
        measures = {m.name: m for m in metadata.all_measures if m.name}
        values: dict[str, MeasureValue] = {}
        table_cache: dict[str, object] = {}
        # Cache of filtered frames, keyed by table name.
        filtered_cache: dict[str, object] = {}

        def get_table(name: str):
            if filter_spec is None:
                return self._table(model, name, table_cache)
            if name not in filtered_cache:
                filtered_cache[name] = self._filtered_table(
                    model, name, table_cache, metadata, filter_spec
                )
            return filtered_cache[name]


        # Pass 1 — base aggregates that read a column/table directly.
        for name, measure in measures.items():
            expr = " ".join((measure.dax_expression or "").split())
            result = self._eval_aggregate(model, expr, table_cache, get_table)
            if result is not None:
                values[name] = MeasureValue(name, result, "aggregate", expr)

        # Pass 2 — derived measures, resolved iteratively so that a measure
        # depending on another derived measure still resolves.
        lookup = {n.casefold(): n for n in measures}
        for _ in range(len(measures)):
            progressed = False
            for name, measure in measures.items():
                if name in values:
                    continue
                expr = " ".join((measure.dax_expression or "").split())
                result = self._eval_derived(expr, values, lookup)
                if result is not None:
                    values[name] = MeasureValue(name, result, "derived", expr)
                    progressed = True
            if not progressed:
                break
        return values

    def _eval_aggregate(self, model, expr: str, cache: dict, get_table=None) -> float | None:
        get_table = get_table or (lambda n: self._table(model, n, cache))
        match = _AGG_COLUMN.match(expr)
        if match:
            func, table_name, column = (g.strip() for g in match.groups())
            df = get_table(table_name)
            if df is None or column not in df.columns:
                return None
            series = df[column]
            func = func.upper()
            try:
                if func == "SUM":
                    return float(series.sum())
                if func in ("AVERAGE", "AVG"):
                    return float(series.mean())
                if func == "MIN":
                    return float(series.min())
                if func == "MAX":
                    return float(series.max())
                if func == "COUNT":
                    return float(series.count())
                if func == "DISTINCTCOUNT":
                    return float(series.nunique())
            except (TypeError, ValueError):
                return None      # non-numeric column
            return None

        match = _AGG_TABLE.match(expr)
        if match:
            df = get_table(match.group(2).strip())
            return float(len(df)) if df is not None else None
        return None

    @staticmethod
    def _eval_derived(expr: str, values: dict, lookup: dict) -> float | None:
        def resolve(ref: str) -> float | None:
            key = lookup.get(ref.strip().casefold())
            found = values.get(key) if key else None
            return found.value if found else None

        match = _DIVIDE.match(expr)
        if match:
            left, right = resolve(match.group(1)), resolve(match.group(2))
            if left is None or right is None:
                return None
            return None if right == 0 else left / right

        match = _BINARY.match(expr)
        if match:
            left, op, right = resolve(match.group(1)), match.group(2), resolve(match.group(3))
            if left is None or right is None:
                return None
            if op == "-":
                return left - right
            if op == "+":
                return left + right
            return left * right
        return None

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _table(model, name: str, cache: dict):
        if name in cache:
            return cache[name]
        try:
            df = model.get_table(name)
        except Exception:  # noqa: BLE001 - table not in the model
            df = None
        cache[name] = df
        return df

    @staticmethod
    def _format(value: float) -> str:
        """Raw numeric string — the comparison engine handles display formats."""
        if value == int(value):
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _pbix_path(self, project: Project):
        paths = self._repo.paths_for(project)
        files = fm.list_dir(paths.dashboard_dir)
        for f in files:
            if f.suffix.lower() == ".pbix":
                return f
        return None
