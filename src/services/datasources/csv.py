"""CSV datasource connector.

A CSV is a single flat table, so it exposes exactly one dataset named after the
file. Everything else mirrors the Excel connector: read-only, pandas-backed,
with low-cardinality column profiling so the AI sees real literal values when
generating SQL.
"""

from __future__ import annotations

from pathlib import Path

from src.core.exceptions import DatasourceConfigError, DatasourceConnectionError
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DataQueryResult
from src.services.datasources.base import ConnectionTestResult, DatasourceConnector

_logger = get_logger()


class CsvConnector(DatasourceConnector):
    """Read-only access to a single CSV file."""

    def __init__(self, config: DatasourceConfig):
        super().__init__(config)

    # --- helpers ----------------------------------------------------------
    def _resolve_path(self) -> Path:
        # Reuses ``excel_path`` so a single "file datasource" field serves both.
        if not self.config.excel_path:
            raise DatasourceConfigError("CSV file path is required.")
        path = Path(self.config.excel_path)
        if not path.exists():
            raise DatasourceConfigError(f"CSV file not found: {path}")
        if not path.is_file():
            raise DatasourceConfigError(f"Not a file: {path}")
        return path

    @staticmethod
    def _require_pandas():
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - environment guard
            raise DatasourceConnectionError(
                "pandas not installed. Run: pip install pandas"
            ) from exc
        return pd

    def _dataset_name(self) -> str:
        return self._resolve_path().stem

    def _read(self, nrows: int | None = None):
        pd = self._require_pandas()
        return pd.read_csv(self._resolve_path(), nrows=nrows)

    # --- interface --------------------------------------------------------
    def test_connection(self) -> ConnectionTestResult:
        try:
            path = self._resolve_path()
            df = self._read(nrows=5)
            return ConnectionTestResult(
                ok=True,
                message=f"Read CSV with {len(df.columns)} column(s).",
                details={"path": str(path), "columns": ", ".join(map(str, df.columns))},
            )
        except DatasourceConfigError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("CSV connection test failed: %s", exc)
            return ConnectionTestResult(ok=False, message=f"Could not read CSV: {exc}")

    def list_datasets(self) -> list[str]:
        return [self._dataset_name()]

    def get_schema(self):
        from src.core.constants import DatasourceType
        from src.domain.models import DbColumn, DbSchema, DbTable

        path = self._resolve_path()
        df = self._read()
        columns = []
        for c in df.columns:
            col = DbColumn(name=str(c), data_type=str(df[c].dtype))
            try:
                distinct = df[c].dropna().unique()
                if 0 < len(distinct) < 25:
                    col.sample_values = [str(v) for v in distinct[:25]]
            except Exception:  # noqa: BLE001 - unhashable dtypes
                pass
            columns.append(col)

        return DbSchema(
            datasource_type=DatasourceType.CSV,
            database=path.name,
            tables=[DbTable(schema="", name=path.stem, kind="csv",
                            columns=columns, row_count=int(len(df)))],
        )

    def get_columns(self, dataset: str) -> list[str]:
        return [str(c) for c in self._read(nrows=0).columns]

    def get_row_count(self, dataset: str) -> int:
        return int(len(self._read()))

    def run_query(self, query: str, *, sample_rows: int = 50) -> DataQueryResult:
        """*query* is ignored — a CSV has exactly one dataset."""
        result = DataQueryResult(label="CSV file", query=query or self._dataset_name())
        try:
            df = self._read(nrows=max(1, sample_rows))
            result.columns = [str(c) for c in df.columns]
            result.sample_rows = [
                self._stringify_row(row) for row in df.itertuples(index=False, name=None)
            ]
            result.row_count = len(result.sample_rows)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            _logger.warning("CSV read failed: %s", exc)
        return result
