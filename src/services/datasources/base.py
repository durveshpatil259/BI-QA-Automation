"""Abstract datasource connector contract.

Defines the interface every connector implements so the comparison/validation
engines can treat SQL Server and Excel (and future sources) uniformly:

* :meth:`test_connection` — verify reachability/credentials without side effects.
* :meth:`list_datasets`   — enumerate queryable units (tables / sheets).
* :meth:`run_query`       — execute one read-only query and return a
  :class:`~src.domain.models.DataQueryResult`.

A connector is constructed from a :class:`~src.domain.models.DatasourceConfig`
via the factory in :mod:`src.services.datasources.factory`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from src.domain.models import DatasourceConfig, DataQueryResult


@dataclass
class ConnectionTestResult:
    """Outcome of a connection test."""

    ok: bool
    message: str
    details: dict[str, str] = field(default_factory=dict)


class DatasourceConnector(abc.ABC):
    """Uniform, read-only access to a configured datasource."""

    def __init__(self, config: DatasourceConfig):
        self.config = config

    @abc.abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Attempt to connect (and run a trivial probe) without mutating data."""

    @abc.abstractmethod
    def list_datasets(self) -> list[str]:
        """Return queryable unit names (SQL: ``schema.table``; Excel: sheets)."""

    @abc.abstractmethod
    def run_query(self, query: str, *, sample_rows: int = 50) -> DataQueryResult:
        """Execute a read-only query and return a bounded result set.

        Implementations must never allow write/DDL statements to run; the
        deterministic engines only ever issue SELECT-style reads.
        """

    def preview_dataset(self, dataset: str, *, sample_rows: int = 50) -> DataQueryResult:
        """Preview a whole dataset (table/sheet) by name.

        Default treats the dataset identifier as the query, which is correct for
        Excel (sheet name). SQL-style connectors override this to build a safe
        ``SELECT`` — a bare ``schema.table`` is not a valid statement.
        """
        return self.run_query(dataset, sample_rows=sample_rows)

    # --- schema helpers used by the comparison engine --------------------
    # Concrete connectors override these. Kept non-abstract so a new connector
    # can be added minimally and only gain schema comparison when it opts in.
    def get_columns(self, dataset: str) -> list[str]:
        """Return column names for *dataset* (SQL: ``schema.table``; Excel: sheet)."""
        raise NotImplementedError

    def get_row_count(self, dataset: str) -> int:
        """Return the row count for *dataset*."""
        raise NotImplementedError

    # Shared helper: stringify a row so results are JSON-serializable.
    @staticmethod
    def _stringify_row(row) -> list[str]:
        return ["" if v is None else str(v) for v in row]
