"""Excel/CSV execution — selects the deterministic file engine."""

from __future__ import annotations

from src.domain.models import DatasourceConfig
from src.services.execution.base import ExecutionAdapter
from src.services.execution.duckdb_adapter import DuckDbAdapter


def build_file_adapter(config: DatasourceConfig, metadata=None) -> ExecutionAdapter:
    return DuckDbAdapter(config, metadata)
