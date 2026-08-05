"""SQL Server datasource connector (SQLAlchemy + pyodbc).

Builds a connection URL from a :class:`DatasourceConfig`, supporting both SQL
Login and Windows (trusted) authentication. All queries are read-only; a guard
rejects anything that is not a single SELECT/WITH statement so the connector can
never be used to mutate the source.
"""

from __future__ import annotations

import re
import urllib.parse

from src.core.exceptions import DatasourceConfigError, DatasourceConnectionError
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DataQueryResult
from src.services.datasources.base import ConnectionTestResult, DatasourceConnector

_logger = get_logger()

# Only allow read statements. Reject batches (semicolons) and write/DDL verbs.
_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|merge|exec|execute|grant|revoke)\b",
    re.IGNORECASE,
)


class SqlServerConnector(DatasourceConnector):
    """Read-only access to a SQL Server database."""

    def __init__(self, config: DatasourceConfig):
        super().__init__(config)
        self._engine = None  # lazily created SQLAlchemy engine

    # --- connection string ------------------------------------------------
    def _odbc_connect_string(self) -> str:
        from src.core.constants import SqlAuthMode

        cfg = self.config
        if not cfg.server:
            raise DatasourceConfigError("SQL Server 'server' is required.")
        if not cfg.database:
            raise DatasourceConfigError("SQL Server 'database' is required.")

        server = cfg.server
        if cfg.port and "," not in server and "\\" not in server:
            server = f"{server},{cfg.port}"

        parts = [
            f"DRIVER={{{cfg.driver}}}",
            f"SERVER={server}",
            f"DATABASE={cfg.database}",
        ]
        # Encrypt / TrustServerCertificate are only understood by the modern
        # "ODBC Driver NN for SQL Server" family. The legacy "SQL Server"
        # (DBNETLIB) driver rejects them with "Invalid connection string
        # attribute", so omit them there.
        if cfg.driver.lower().startswith("odbc driver"):
            parts.append(f"Encrypt={'yes' if cfg.encrypt else 'no'}")
            parts.append(
                f"TrustServerCertificate={'yes' if cfg.trust_server_certificate else 'no'}"
            )
        if cfg.auth_mode == SqlAuthMode.WINDOWS:
            parts.append("Trusted_Connection=yes")
        else:
            if not cfg.username:
                raise DatasourceConfigError("SQL Login requires a username.")
            parts.append(f"UID={cfg.username}")
            parts.append(f"PWD={cfg.password}")
        return ";".join(parts)

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from sqlalchemy import create_engine
        except ImportError as exc:  # pragma: no cover - environment guard
            raise DatasourceConnectionError(
                "SQLAlchemy/pyodbc not installed. Run: pip install SQLAlchemy pyodbc"
            ) from exc

        odbc = self._odbc_connect_string()
        url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)
        # fast_executemany off (read-only), short pool for a desktop app.
        self._engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
        return self._engine

    # --- interface --------------------------------------------------------
    def test_connection(self) -> ConnectionTestResult:
        try:
            from sqlalchemy import text

            engine = self._get_engine()
            with engine.connect() as conn:
                version = conn.execute(text("SELECT @@VERSION")).scalar()
                db = conn.execute(text("SELECT DB_NAME()")).scalar()
            first_line = (version or "").splitlines()[0] if version else "Connected"
            return ConnectionTestResult(
                ok=True,
                message=f"Connected to database '{db}'.",
                details={"version": first_line, "database": str(db)},
            )
        except DatasourceConfigError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface driver errors cleanly
            _logger.warning("SQL Server connection test failed: %s", exc)
            return ConnectionTestResult(ok=False, message=f"Connection failed: {exc}")

    def list_datasets(self) -> list[str]:
        from sqlalchemy import text

        engine = self._get_engine()
        sql = text(
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW') "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        )
        with engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [f"{schema}.{name}" for schema, name in rows]

    def run_query(self, query: str, *, sample_rows: int = 50) -> DataQueryResult:
        self._assert_read_only(query)
        from sqlalchemy import text

        result = DataQueryResult(label="SQL query", query=query)
        try:
            engine = self._get_engine()
            with engine.connect() as conn:
                cursor = conn.execute(text(query))
                result.columns = list(cursor.keys())
                fetched = cursor.fetchmany(max(1, sample_rows))
            result.sample_rows = [self._stringify_row(r) for r in fetched]
            result.row_count = len(result.sample_rows)
            if len(result.columns) == 1 and result.row_count == 1:
                result.scalar_value = result.sample_rows[0][0]
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            _logger.warning("SQL query failed: %s", exc)
        return result

    # --- schema helpers ---------------------------------------------------
    @staticmethod
    def _split_dataset(dataset: str) -> tuple[str, str]:
        if "." in dataset:
            schema, _, table = dataset.partition(".")
            return schema.strip(), table.strip()
        return "dbo", dataset.strip()

    @staticmethod
    def _bracket(identifier: str) -> str:
        # Safely quote a SQL Server identifier (escape closing brackets).
        return "[" + identifier.replace("]", "]]") + "]"

    def get_columns(self, dataset: str) -> list[str]:
        from sqlalchemy import bindparam, text

        schema, table = self._split_dataset(dataset)
        sql = text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table "
            "ORDER BY ORDINAL_POSITION"
        ).bindparams(bindparam("schema", schema), bindparam("table", table))
        with self._get_engine().connect() as conn:
            return [row[0] for row in conn.execute(sql).fetchall()]

    def preview_dataset(self, dataset: str, *, sample_rows: int = 50):
        """Build a safe ``SELECT TOP N *`` for the given ``schema.table``."""
        schema, table = self._split_dataset(dataset)
        qualified = f"{self._bracket(schema)}.{self._bracket(table)}"
        n = max(1, int(sample_rows))
        return self.run_query(f"SELECT TOP {n} * FROM {qualified}", sample_rows=sample_rows)

    def get_row_count(self, dataset: str) -> int:
        from sqlalchemy import text

        schema, table = self._split_dataset(dataset)
        # Identifiers come from our own INFORMATION_SCHEMA listing and are
        # bracket-quoted; values are never interpolated.
        qualified = f"{self._bracket(schema)}.{self._bracket(table)}"
        with self._get_engine().connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar() or 0)

    # --- safety -----------------------------------------------------------
    @staticmethod
    def _assert_read_only(query: str) -> None:
        q = (query or "").strip().rstrip(";")
        if ";" in q:
            raise DatasourceConfigError("Multiple statements are not allowed.")
        if not _READ_ONLY.match(q) or _FORBIDDEN.search(q):
            raise DatasourceConfigError(
                "Only a single read-only SELECT/WITH statement is permitted."
            )
