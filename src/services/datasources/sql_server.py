"""SQL Server datasource connector (SQLAlchemy + pyodbc).

Builds a connection URL from a :class:`DatasourceConfig`, supporting both SQL
Login and Windows (trusted) authentication. All queries are read-only; a guard
rejects anything that is not a single SELECT/WITH statement so the connector can
never be used to mutate the source.
"""

from __future__ import annotations

import urllib.parse

from src.core.exceptions import DatasourceConfigError, DatasourceConnectionError
from src.core.logger import get_logger
from src.domain.models import DatasourceConfig, DataQueryResult
from src.services.datasources.base import ConnectionTestResult, DatasourceConnector
from src.services.validation.sql_guard import is_read_only

_logger = get_logger()


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

    # --- schema introspection --------------------------------------------
    def get_schema(self):
        """Read tables, columns, primary keys and foreign keys via catalog views."""
        from sqlalchemy import text

        engine = self._get_engine()
        with engine.connect() as conn:
            db = conn.execute(text("SELECT DB_NAME()")).scalar()
            tables = conn.execute(text(
                "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')"
            )).fetchall()
            columns = conn.execute(text(
                "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
                "ORDINAL_POSITION FROM INFORMATION_SCHEMA.COLUMNS "
                "ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
            )).fetchall()
            pks = conn.execute(text(
                "SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                "  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "  AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA "
                "WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'"
            )).fetchall()
            fks = conn.execute(text(
                "SELECT fk.name, sch.name, tp.name, cp.name, rsch.name, tr.name, cr.name "
                "FROM sys.foreign_keys fk "
                "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
                "JOIN sys.tables tp ON tp.object_id = fk.parent_object_id "
                "JOIN sys.schemas sch ON sch.schema_id = tp.schema_id "
                "JOIN sys.columns cp ON cp.object_id = tp.object_id AND cp.column_id = fkc.parent_column_id "
                "JOIN sys.tables tr ON tr.object_id = fk.referenced_object_id "
                "JOIN sys.schemas rsch ON rsch.schema_id = tr.schema_id "
                "JOIN sys.columns cr ON cr.object_id = tr.object_id AND cr.column_id = fkc.referenced_column_id"
            )).fetchall()
        return self._assemble_schema(str(db), tables, columns, pks, fks)

    @staticmethod
    def _assemble_schema(database, table_rows, column_rows, pk_rows, fk_rows):
        """Pure assembly of catalog rows into a DbSchema (unit-testable)."""
        from src.core.constants import DatasourceType
        from src.domain.models import DbColumn, DbForeignKey, DbSchema, DbTable

        def key(schema, name):
            return f"{schema}.{name}"

        kinds = {
            key(s, n): ("view" if str(t).upper() == "VIEW" else "table")
            for s, n, t in table_rows
        }
        pk_set = {key(s, n): set() for s, n in {(r[0], r[1]) for r in pk_rows}}
        for s, n, col in pk_rows:
            pk_set.setdefault(key(s, n), set()).add(col)

        tables: dict[str, DbTable] = {}
        for s, n, col, dtype, nullable, _ordinal in column_rows:
            k = key(s, n)
            if k not in kinds:
                continue  # skip columns of non-base tables/views
            tbl = tables.get(k)
            if tbl is None:
                tbl = DbTable(schema=s, name=n, kind=kinds.get(k, "table"),
                              primary_keys=sorted(pk_set.get(k, set())))
                tables[k] = tbl
            is_pk = col in pk_set.get(k, set())
            tbl.columns.append(DbColumn(
                name=col, data_type=str(dtype),
                nullable=str(nullable).upper() == "YES", is_primary_key=is_pk,
            ))

        for _name, s, n, col, rsch, rtbl, rcol in fk_rows:
            tbl = tables.get(key(s, n))
            if tbl is not None:
                tbl.foreign_keys.append(DbForeignKey(
                    column=col, ref_table=key(rsch, rtbl), ref_column=rcol,
                    constraint_name=str(_name),
                ))

        return DbSchema(
            datasource_type=DatasourceType.SQL_SERVER, database=database,
            tables=sorted(tables.values(), key=lambda t: t.full_name.lower()),
        )

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
        if not is_read_only(query):
            raise DatasourceConfigError(
                "Only a single read-only SELECT/WITH statement is permitted."
            )
