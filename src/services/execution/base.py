"""Execution adapters — the only layer that differs per datasource.

Everything upstream (PBIX analysis, visual detection, validation plan) and
everything downstream (comparison, verdicts, test cases, report) is shared.
A datasource changes *how a plan item is executed*, nothing else.

``ValidationPlanItem`` already carries the structured intent — table, column,
aggregation, filters, dimension — so an adapter that cannot run SQL can still
execute the same plan by reading those fields directly. ``generated_sql`` is an
implementation detail of the SQL Server adapter, not part of the contract.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from src.domain.models import ValidationPlanItem

__all__ = ["ExecutionOutcome", "ExecutionAdapter"]


@dataclass
class ExecutionOutcome:
    """One execution attempt: the value(s), how long it took, and the evidence.

    ``error`` set means the attempt failed and ``value``/``rows`` are unusable.
    ``evidence`` is what the report shows to make the result reproducible —
    the SQL for a database, or the file/sheet/operation for a spreadsheet.
    """

    value: str | None = None                  # scalar result
    rows: list[list[str]] = field(default_factory=list)   # grouped result
    elapsed_ms: float | None = None
    error: str | None = None
    evidence: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


class ExecutionAdapter(abc.ABC):
    """Executes validation-plan items against one kind of datasource."""

    #: Shown in the report so a reader knows which engine produced the numbers.
    name: str = "adapter"

    @abc.abstractmethod
    def is_ready(self) -> tuple[bool, str]:
        """(usable, reason). Reason explains what to fix when not usable."""

    @abc.abstractmethod
    def execute_scalar(self, item: ValidationPlanItem) -> ExecutionOutcome:
        """Compute one number for a KPI item."""

    @abc.abstractmethod
    def execute_grouped(
        self, item: ValidationPlanItem, *, max_rows: int = 500
    ) -> ExecutionOutcome:
        """Compute (dimension, value) rows for a chart/table item.

        Structural items reuse this and read only the first column.
        """

    def describe_source(self) -> str:
        """Short human label for the source, e.g. 'sales.xlsx · Sales'."""
        return self.name
