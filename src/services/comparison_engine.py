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
    normalise_table_name,
)
from src.services.datasources import DatasourceConnector, create_connector

#: Hidden date tables Power BI auto-creates per date column. Model-only.
_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")

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

        # Map normalised name -> actual dataset name. Warehouse tables rarely
        # match the model name exactly (Sales -> Sales_data), so an exact match
        # reported five present tables as "missing".
        # A key can have several candidates (SalesLT.Customer *and*
        # dbo.customer_data both normalise to "customer"); keep them all and
        # let column overlap decide, rather than whichever came back first.
        dataset_index: dict[str, list[str]] = {}
        for d in datasets:
            for key in {self._normalise(d), normalise_table_name(d)}:
                dataset_index.setdefault(key, []).append(d)

        # Only physical tables are datasource-backed; calculated tables are not.
        # Power BI also generates a hidden date table per date column; those
        # exist only inside the model and can never be in a datasource, so
        # reporting them as missing is pure noise.
        physical = [
            t for t in metadata.tables
            if not t.is_calculated and not t.name.startswith(_AUTO_DATE_PREFIXES)
        ]
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
        dataset_index: dict[str, list[str]],
        comparisons: list[ComparisonResult],
        data_results: list[DataQueryResult],
    ) -> None:
        candidates = (
            dataset_index.get(self._normalise(table.name))
            or dataset_index.get(normalise_table_name(table.name))
            or []
        )
        dataset = self._best_dataset(connector, table, candidates)

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
    def _best_dataset(
        connector: DatasourceConnector, table: Table, candidates: list[str]
    ) -> str | None:
        """Pick the candidate sharing the most column names with the model.

        Name similarity alone picks an unrelated sample table over the real
        one; column overlap settles it (Customer shares 7 columns with
        customer_data and 1 with SalesLT.Customer).
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        model_cols = {c.name.casefold() for c in table.columns}
        best, best_score = candidates[0], -1
        for name in candidates:
            try:
                cols = {c.casefold() for c in connector.get_columns(name)}
            except Exception:  # noqa: BLE001 - unreadable candidate loses
                continue
            score = len(model_cols & cols)
            if score > best_score:
                best, best_score = name, score
        return best

    @staticmethod
    def _normalise(name: str) -> str:
        """Schema-less, case-folded table name for matching."""
        name = (name or "").strip()
        if "." in name:
            name = name.rsplit(".", 1)[-1]
        return name.strip("[]").casefold()
