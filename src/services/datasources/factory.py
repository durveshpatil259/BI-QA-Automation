"""Factory that maps a :class:`DatasourceConfig` to the right connector.

Keeping construction in one place means the service and engines depend only on
the abstract :class:`DatasourceConnector`, not on concrete connector classes.
Adding a new datasource type later means registering it here — nothing else
changes.
"""

from __future__ import annotations

from src.core.constants import DatasourceType
from src.core.exceptions import DatasourceConfigError
from src.domain.models import DatasourceConfig
from src.services.datasources.base import DatasourceConnector
from src.services.datasources.csv import CsvConnector
from src.services.datasources.excel import ExcelConnector
from src.services.datasources.sql_server import SqlServerConnector

_REGISTRY: dict[DatasourceType, type[DatasourceConnector]] = {
    DatasourceType.SQL_SERVER: SqlServerConnector,
    DatasourceType.EXCEL: ExcelConnector,
    DatasourceType.CSV: CsvConnector,
}


def create_connector(config: DatasourceConfig) -> DatasourceConnector:
    connector_cls = _REGISTRY.get(config.type)
    if connector_cls is None:
        raise DatasourceConfigError(f"Unsupported datasource type: {config.type}")
    return connector_cls(config)
