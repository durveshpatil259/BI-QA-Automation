"""Datasource connectors.

A small abstraction over the datasources the product can read for deterministic
data validation. Each connector knows how to (a) test its connection and
(b) execute a read-only query returning a :class:`DataQueryResult`.

IMPORTANT: These connectors are used exclusively by Python-side deterministic
logic (comparison/validation). The LLM never touches them — it only receives
the resulting DataQueryResult objects inside the AnalysisContext.

Driver imports are lazy (performed inside methods) so the application starts
even if an optional driver is not installed on the machine.
"""

from src.services.datasources.base import (
    ConnectionTestResult,
    DatasourceConnector,
)
from src.services.datasources.factory import create_connector

__all__ = [
    "ConnectionTestResult",
    "DatasourceConnector",
    "create_connector",
]
