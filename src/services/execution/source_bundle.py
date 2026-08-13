"""Load an Excel workbook or CSV once, and resolve model names against it.

Every plan item names things in the *model's* vocabulary — ``Sales[Sales
Amount]``, ``Date[Fiscal Year]`` — while the source has sheets and columns of
its own. Resolving that per item would re-read the file hundreds of times, so
the bundle loads each dataset once and caches the name resolution.

Nothing here guesses: a name that cannot be resolved comes back as ``None`` and
the caller reports it, rather than validating against the wrong column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.core.constants import DatasourceType
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig
from src.services.validation.column_mapper import (
    ColumnMatch, map_columns, map_table_to_dataset)

_logger = get_logger()

__all__ = ["SourceBundle", "ResolvedField"]


@dataclass
class ResolvedField:
    """A model column located in the source."""

    dataset: str
    column: str
    confidence: float
    method: str


@dataclass
class SourceBundle:
    """Datasets from one Excel workbook or CSV folder, plus name resolution."""

    label: str = ""
    frames: dict = field(default_factory=dict)          # dataset -> DataFrame
    _table_map: dict = field(default_factory=dict)      # model table -> dataset
    _column_map: dict = field(default_factory=dict)     # (dataset) -> [ColumnMatch]

    # --- loading ----------------------------------------------------------
    @classmethod
    def load(cls, config: DatasourceConfig) -> "SourceBundle":
        """Read every sheet of a workbook, or every CSV beside the configured one."""
        import pandas as pd

        path = Path(config.excel_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found: {path}")

        frames: dict = {}
        if config.type == DatasourceType.EXCEL:
            book = pd.ExcelFile(path)
            for sheet in book.sheet_names:
                frames[str(sheet)] = book.parse(sheet)
            label = path.name
        else:
            # A dashboard usually needs several CSVs (fact + dimensions), so
            # take every CSV in the same folder, not just the configured one.
            for csv in sorted(path.parent.glob("*.csv")):
                try:
                    frames[csv.stem] = pd.read_csv(csv)
                except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
                    _logger.warning("Could not read %s: %s", csv.name, exc)
            label = path.parent.name if len(frames) > 1 else path.name

        if not frames:
            raise ValueError(f"No readable data found at {path}")
        _logger.info("Loaded %d dataset(s) from %s", len(frames), label)
        return cls(label=label, frames=frames)

    # --- name resolution --------------------------------------------------
    @property
    def datasets(self) -> dict[str, list[str]]:
        return {name: [str(c) for c in df.columns] for name, df in self.frames.items()}

    def dataset_for(self, model_table: str, model_columns: list[str] | None = None) -> str:
        """Which sheet/file backs a model table. Cached per table."""
        key = (model_table or "").casefold()
        if key in self._table_map:
            return self._table_map[key]

        bare = (model_table or "").rsplit(".", 1)[-1].strip()
        # Column coverage is the reliable signal; fall back to the name when the
        # caller has no column list to compare with.
        chosen = ""
        if model_columns:
            chosen, coverage = map_table_to_dataset(model_columns, self.datasets)
            if coverage < 0.3:
                chosen = ""
        if not chosen:
            from src.domain.models import normalise_table_name

            target = normalise_table_name(bare)
            for name in self.frames:
                if normalise_table_name(name) == target:
                    chosen = name
                    break
        self._table_map[key] = chosen
        return chosen

    def columns_for(self, dataset: str, model_columns: list[str]) -> list[ColumnMatch]:
        cache_key = (dataset, tuple(model_columns))
        if cache_key not in self._column_map:
            self._column_map[cache_key] = map_columns(
                model_columns, self.datasets.get(dataset, []), table=dataset
            )
        return self._column_map[cache_key]

    def resolve(self, model_table: str, model_column: str,
                model_columns: list[str] | None = None) -> ResolvedField | None:
        """Locate one model column in the source, or None when unresolvable."""
        dataset = self.dataset_for(model_table, model_columns)
        if not dataset:
            return None
        matches = self.columns_for(dataset, [model_column])
        match = matches[0] if matches else None
        if not match or not match.usable:
            return None
        return ResolvedField(dataset, match.source_field, match.confidence, match.method)

    def find_column(self, model_column: str) -> ResolvedField | None:
        """Locate a column when its table is unknown — search every dataset.

        Used for filters, whose table is named in the model's vocabulary and may
        not correspond to the dataset holding the measure.
        """
        best: ResolvedField | None = None
        for dataset in self.frames:
            match = self.columns_for(dataset, [model_column])[0]
            if match.usable and (best is None or match.confidence > best.confidence):
                best = ResolvedField(dataset, match.source_field,
                                     match.confidence, match.method)
        return best
