"""Excel datasource connector (pandas + openpyxl).

Treats each worksheet as a queryable dataset. ``run_query`` accepts a sheet
name (optionally the configured default) and returns a bounded preview; this
keeps the connector interface uniform with SQL Server while staying purely
read-only.
"""

from __future__ import annotations

from pathlib import Path

from src.core.exceptions import DatasourceConfigError, DatasourceConnectionError
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DataQueryResult
from src.services.datasources.base import ConnectionTestResult, DatasourceConnector

_logger = get_logger()


class ExcelConnector(DatasourceConnector):
    """Read-only access to an Excel workbook."""

    def __init__(self, config: DatasourceConfig):
        super().__init__(config)

    # --- helpers ----------------------------------------------------------
    def _resolve_path(self) -> Path:
        if not self.config.excel_path:
            raise DatasourceConfigError("Excel file path is required.")
        path = Path(self.config.excel_path)
        if not path.exists():
            raise DatasourceConfigError(f"Excel file not found: {path}")
        if not path.is_file():
            raise DatasourceConfigError(f"Not a file: {path}")
        return path

    @staticmethod
    def _require_pandas():
        try:
            import pandas as pd  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment guard
            raise DatasourceConnectionError(
                "pandas/openpyxl not installed. Run: pip install pandas openpyxl"
            ) from exc
        return pd

    # --- interface --------------------------------------------------------
    def test_connection(self) -> ConnectionTestResult:
        try:
            pd = self._require_pandas()
            path = self._resolve_path()
            with pd.ExcelFile(path) as xls:
                sheets = list(xls.sheet_names)
            return ConnectionTestResult(
                ok=True,
                message=f"Opened workbook with {len(sheets)} sheet(s).",
                details={"path": str(path), "sheets": ", ".join(sheets)},
            )
        except DatasourceConfigError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Excel connection test failed: %s", exc)
            return ConnectionTestResult(ok=False, message=f"Could not open workbook: {exc}")

    def list_datasets(self) -> list[str]:
        pd = self._require_pandas()
        path = self._resolve_path()
        with pd.ExcelFile(path) as xls:
            return list(xls.sheet_names)

    def sheet_summaries(self) -> list[dict]:
        """Return per-worksheet metadata: name, row count and column count.

        Reads the workbook once (all sheets) for an efficient overview used by
        the auto-configuring Excel UI.
        """
        pd = self._require_pandas()
        path = self._resolve_path()
        summaries: list[dict] = []
        with pd.ExcelFile(path) as xls:
            for name in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=name)
                summaries.append({
                    "name": name,
                    "rows": int(len(df)),
                    "cols": int(len(df.columns)),
                })
        return summaries

    def get_schema(self):
        """Build a DbSchema where each worksheet is a table (no PK/FK in Excel)."""
        from src.core.constants import DatasourceType
        from src.domain.models import DbColumn, DbSchema, DbTable

        pd = self._require_pandas()
        path = self._resolve_path()
        schema = DbSchema(datasource_type=DatasourceType.EXCEL, database=path.name)
        with pd.ExcelFile(path) as xls:
            for name in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=name)
                cols = [
                    DbColumn(name=str(c), data_type=str(df[c].dtype)) for c in df.columns
                ]
                schema.tables.append(DbTable(
                    schema="", name=str(name), kind="sheet",
                    columns=cols, row_count=int(len(df)),
                ))
        return schema

    def get_columns(self, dataset: str) -> list[str]:
        pd = self._require_pandas()
        path = self._resolve_path()
        sheet = (dataset or self.config.sheet_name or "").strip() or 0
        df = pd.read_excel(path, sheet_name=sheet, nrows=0)
        return [str(c) for c in df.columns]

    def get_row_count(self, dataset: str) -> int:
        pd = self._require_pandas()
        path = self._resolve_path()
        sheet = (dataset or self.config.sheet_name or "").strip() or 0
        # Read a single column to count rows without loading the whole sheet width.
        df = pd.read_excel(path, sheet_name=sheet, usecols=[0])
        return int(len(df))

    def run_query(self, query: str, *, sample_rows: int = 50) -> DataQueryResult:
        """For Excel, *query* is a sheet name (blank -> configured/first sheet)."""
        pd = self._require_pandas()
        sheet = (query or self.config.sheet_name or "").strip()
        result = DataQueryResult(label="Excel sheet", query=sheet or "(first sheet)")
        try:
            path = self._resolve_path()
            df = pd.read_excel(path, sheet_name=sheet or 0, nrows=max(1, sample_rows))
            result.columns = [str(c) for c in df.columns]
            result.sample_rows = [
                self._stringify_row(row) for row in df.itertuples(index=False, name=None)
            ]
            result.row_count = len(result.sample_rows)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            _logger.warning("Excel read failed: %s", exc)
        return result
