"""Deterministic comparison engine.

Compares the extracted :class:`DashboardMetadata` against the configured
datasource, using only the read-only connector API. It produces:

* :class:`ComparisonResult` — one per check (table existence, column alignment,
  row-count evidence), and
* :class:`DataQueryResult` — the raw datasource facts gathered (row counts),
  preserved for traceability in the AnalysisContext.

All datasource access is Python-side; the LLM never runs a query. Every table
comparison is isolated so one failure cannot abort the whole run.
"""

from __future__ import annotations

from src.core.constants import Severity
from src.core.logger import get_logger
from src.domain.models import (
    ComparisonResult,
    DashboardMetadata,
    DatasourceConfig,
    DataQueryResult,
    Table,
)
from src.services.datasources import DatasourceConnector, create_connector

_logger = get_logger()


class ComparisonEngine:
    """Compares dashboard metadata with a datasource."""

    def compare(
        self, metadata: DashboardMetadata, datasource: DatasourceConfig
    ) -> tuple[list[ComparisonResult], list[DataQueryResult]]:
        connector = create_connector(datasource)

        comparisons: list[ComparisonResult] = []
        data_results: list[DataQueryResult] = []

        try:
            datasets = connector.list_datasets()
        except Exception as exc:  # noqa: BLE001
            comparisons.append(ComparisonResult(
                label="Datasource access",
                dashboard_value="required", datasource_value="unavailable",
                matched=False, difference=f"Could not list datasets: {exc}",
                severity=Severity.CRITICAL,
            ))
            return comparisons, data_results

        # Map normalised (schema-less, case-folded) name -> actual dataset name.
        dataset_index = {self._normalise(d): d for d in datasets}

        # Only physical tables are datasource-backed; calculated tables are not.
        physical = [t for t in metadata.tables if not t.is_calculated]
        for table in physical:
            self._compare_table(connector, table, dataset_index, comparisons, data_results)

        _logger.info(
            "Comparison complete: %d comparison(s), %d data result(s).",
            len(comparisons), len(data_results),
        )
        return comparisons, data_results

    # --- per-table --------------------------------------------------------
    def _compare_table(
        self,
        connector: DatasourceConnector,
        table: Table,
        dataset_index: dict[str, str],
        comparisons: list[ComparisonResult],
        data_results: list[DataQueryResult],
    ) -> None:
        dataset = dataset_index.get(self._normalise(table.name))

        # 1) Existence
        if dataset is None:
            comparisons.append(ComparisonResult(
                label=f"Table '{table.name}' present in datasource",
                dashboard_value="present", datasource_value="missing",
                matched=False,
                difference=f"No datasource table/sheet matches '{table.name}'.",
                severity=Severity.WARNING,
            ))
            return
        comparisons.append(ComparisonResult(
            label=f"Table '{table.name}' present in datasource",
            dashboard_value="present", datasource_value=dataset, matched=True,
            severity=Severity.INFO,
        ))

        # 2) Column alignment (exclude calculated columns — not in the source)
        try:
            ds_cols = {c.casefold() for c in connector.get_columns(dataset)}
            model_cols = [c for c in table.columns if not c.is_calculated]
            missing = [c.name for c in model_cols if c.name.casefold() not in ds_cols]
            comparisons.append(ComparisonResult(
                label=f"Columns of '{table.name}' exist in datasource",
                dashboard_value=f"{len(model_cols)} model column(s)",
                datasource_value=f"{len(ds_cols)} datasource column(s)",
                matched=not missing,
                difference="" if not missing else f"Missing in datasource: {', '.join(missing)}",
                severity=Severity.INFO if not missing else Severity.ERROR,
            ))
        except NotImplementedError:
            pass  # connector does not support schema introspection
        except Exception as exc:  # noqa: BLE001
            comparisons.append(ComparisonResult(
                label=f"Columns of '{table.name}'",
                dashboard_value="", datasource_value="",
                matched=False, difference=f"Column check failed: {exc}",
                severity=Severity.WARNING,
            ))

        # 3) Row-count evidence (and comparison if the model reported a count)
        try:
            ds_count = connector.get_row_count(dataset)
            data_results.append(DataQueryResult(
                label=f"Row count · {table.name}",
                query=f"COUNT(*) FROM {dataset}",
                scalar_value=str(ds_count), row_count=ds_count,
            ))
            if table.row_count is not None:
                comparisons.append(ComparisonResult(
                    label=f"Row count of '{table.name}'",
                    dashboard_value=str(table.row_count),
                    datasource_value=str(ds_count),
                    matched=table.row_count == ds_count,
                    difference="" if table.row_count == ds_count
                    else f"Δ {abs(table.row_count - ds_count)} rows",
                    severity=Severity.INFO if table.row_count == ds_count
                    else Severity.ERROR,
                ))
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001
            comparisons.append(ComparisonResult(
                label=f"Row count of '{table.name}'",
                dashboard_value="", datasource_value="",
                matched=False, difference=f"Row count failed: {exc}",
                severity=Severity.WARNING,
            ))

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _normalise(name: str) -> str:
        """Schema-less, case-folded table name for matching."""
        name = (name or "").strip()
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        return name.strip("[]").casefold()
