"""Per-datasource execution adapters behind one interface."""

from src.services.execution.base import ExecutionAdapter, ExecutionOutcome
from src.services.execution.sql_adapter import SqlServerAdapter

__all__ = ["ExecutionAdapter", "ExecutionOutcome", "SqlServerAdapter"]
