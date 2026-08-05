"""Datasource Configuration page.

Configure the active project's datasource (SQL Server or Excel), persist it,
and test the connection deterministically. All access is Python-side via the
connector abstraction; the LLM is never involved.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.core.constants import DatasourceType, SqlAuthMode
from src.core.exceptions import BITestPilotError
from src.domain.models import DatasourceConfig, Project
from src.services.datasources import ConnectionTestResult
from src.ui import theme
from src.ui.state import AppContext, get_active_project

_COMMON_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]


def _installed_drivers() -> list[str]:
    """Best-effort list of ODBC drivers installed on this machine."""
    try:
        import pyodbc

        found = [d for d in pyodbc.drivers() if "SQL Server" in d]
        return found or _COMMON_DRIVERS
    except Exception:  # noqa: BLE001 - pyodbc missing or platform quirk
        return _COMMON_DRIVERS


def _last_status(cfg: DatasourceConfig) -> None:
    if cfg.last_test_ok is None:
        return
    when = f" ({cfg.last_tested_at:%Y-%m-%d %H:%M})" if cfg.last_tested_at else ""
    if cfg.last_test_ok:
        st.success(f"Last test OK{when}: {cfg.last_test_message}")
    else:
        st.error(f"Last test failed{when}: {cfg.last_test_message}")


def _render_result(result: ConnectionTestResult) -> None:
    if result.ok:
        st.success(result.message)
        if result.details:
            with st.expander("Connection details"):
                for k, v in result.details.items():
                    st.write(f"**{k}**: {v}")
    else:
        st.error(result.message)


def _sql_server_form(ctx: AppContext, project: Project, cfg: DatasourceConfig) -> None:
    drivers = _installed_drivers()
    driver_index = drivers.index(cfg.driver) if cfg.driver in drivers else 0
    auth_options = SqlAuthMode.values()
    auth_index = auth_options.index(cfg.auth_mode.value)

    with st.form("sql_form"):
        c1, c2 = st.columns([3, 1])
        server = c1.text_input("Server *", value=cfg.server, placeholder="host\\INSTANCE or host")
        port = c2.number_input("Port", value=int(cfg.port or 1433), min_value=1, max_value=65535)
        database = st.text_input("Database *", value=cfg.database)

        auth = st.selectbox("Authentication *", auth_options, index=auth_index)
        is_sql_login = auth == SqlAuthMode.SQL_LOGIN.value
        c3, c4 = st.columns(2)
        username = c3.text_input("Username", value=cfg.username, disabled=not is_sql_login)
        password = c4.text_input(
            "Password", value=cfg.password, type="password", disabled=not is_sql_login
        )

        driver = st.selectbox("ODBC Driver", drivers, index=driver_index)
        c5, c6 = st.columns(2)
        encrypt = c5.checkbox("Encrypt connection", value=cfg.encrypt)
        trust = c6.checkbox("Trust server certificate", value=cfg.trust_server_certificate)

        save_clicked = st.form_submit_button("💾 Save configuration", type="primary")
        test_clicked = st.form_submit_button("🔌 Save & test connection")

    if save_clicked or test_clicked:
        new_cfg = DatasourceConfig(
            type=DatasourceType.SQL_SERVER,
            name=cfg.name or "Primary Datasource",
            server=server.strip(),
            database=database.strip(),
            auth_mode=SqlAuthMode.from_value(auth),
            username=username.strip(),
            password=password,
            driver=driver,
            port=int(port),
            encrypt=encrypt,
            trust_server_certificate=trust,
        )
        _handle_submit(ctx, project, new_cfg, test=test_clicked)


def _load_workbook_info(ctx: AppContext, cfg: DatasourceConfig, project: Project):
    """Read (and cache) worksheet metadata for the configured workbook.

    Returns (info, error). error == 'missing' means the file no longer exists.
    """
    path = Path(cfg.excel_path)
    if not cfg.excel_path or not path.exists():
        return None, "missing"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    key = f"_xls_info_{project.id}"
    cached = st.session_state.get(key)
    if cached and cached.get("path") == str(path) and cached.get("mtime") == mtime:
        return cached["info"], None
    try:
        info = ctx.datasource_service.excel_workbook_info(cfg)
    except Exception as exc:  # noqa: BLE001 - surface any read failure cleanly
        return None, str(exc)
    st.session_state[key] = {"path": str(path), "mtime": mtime, "info": info}
    return info, None


def _excel_flow(ctx: AppContext, project: Project, cfg: DatasourceConfig) -> None:
    """Fully automatic Excel validation source.

    The uploaded workbook *is* the datasource: it is saved into the project,
    read, and its worksheets detected — with no file-path entry and no Save
    button. The user uploads, optionally picks a sheet, then runs Analysis.
    """
    import pandas as pd

    from src.storage import file_manager as fm

    paths = ctx.projects.paths_for(project)
    st.caption(
        "Upload your Excel workbook — it becomes the validation source "
        "automatically. No file path or manual configuration required."
    )

    uploaded = st.file_uploader(
        "Upload Excel workbook", type=["xlsx", "xls"], key=f"xls_up_{project.id}"
    )

    # Save on a *new* upload only (guarded by a name+size signature) so reruns
    # from other widgets don't re-write the file.
    sig_key = f"_xls_sig_{project.id}"
    if uploaded is not None:
        sig = f"{uploaded.name}:{uploaded.size}"
        if st.session_state.get(sig_key) != sig:
            target = paths.configuration_dir / fm.sanitize_name(
                uploaded.name, fallback="workbook.xlsx"
            )
            fm.save_bytes(target, uploaded.getvalue())
            try:
                ctx.datasource_service.save(project, DatasourceConfig(
                    type=DatasourceType.EXCEL, name="Excel Workbook",
                    excel_path=str(target), sheet_name="",
                ))
            except BITestPilotError as exc:
                st.error(str(exc))
                return
            st.session_state[sig_key] = sig
            st.session_state.pop(f"_xls_info_{project.id}", None)
            st.rerun()

    # Reload in case we just saved a fresh config.
    cfg = ctx.datasource_service.load(project)
    if cfg.type != DatasourceType.EXCEL or not cfg.excel_path:
        st.info("Upload an Excel workbook above to configure the validation source.")
        return

    info, err = _load_workbook_info(ctx, cfg, project)
    if err == "missing":
        st.warning("The previously uploaded workbook is missing. Please upload it again.")
        return
    if err:
        st.error(f"Could not read the workbook: {err}")
        return

    workbook_name = Path(cfg.excel_path).name
    st.success(f"Workbook ready: **{workbook_name}** · {len(info)} worksheet(s) detected.")

    st.markdown("**Detected worksheets**")
    st.dataframe(
        pd.DataFrame([
            {"Worksheet": s["name"], "Rows": s["rows"], "Columns": s["cols"]} for s in info
        ]),
        use_container_width=True, hide_index=True,
    )

    names = [s["name"] for s in info]
    if len(names) == 1:
        selected = names[0]
        st.caption(f"Single worksheet auto-selected: **{selected}**")
    else:
        idx = names.index(cfg.sheet_name) if cfg.sheet_name in names else 0
        selected = st.selectbox("Worksheet to validate against", names, index=idx)

    # Auto-save the selected worksheet (no button).
    if selected != cfg.sheet_name:
        cfg.sheet_name = selected
        ctx.datasource_service.save(project, cfg)

    with st.expander(f"Preview · {selected} (top 50 rows)"):
        result = ctx.datasource_service.preview(cfg, selected, sample_rows=50)
        if result.error:
            st.error(result.error)
        elif result.columns:
            st.dataframe(
                pd.DataFrame(result.sample_rows, columns=result.columns),
                use_container_width=True,
            )
            st.caption(f"{result.row_count} row(s) previewed.")
        else:
            st.info("No rows found in this worksheet.")

    st.info(
        "✅ Validation source configured automatically. Go to **Analysis** and click "
        "**Run full analysis** to compare, validate and generate the test cases & report."
    )


def _handle_submit(
    ctx: AppContext, project: Project, cfg: DatasourceConfig, *, test: bool
) -> None:
    try:
        if test:
            result = ctx.datasource_service.test_connection(project, cfg)
            _render_result(result)
        else:
            ctx.datasource_service.save(project, cfg)
            st.success("Datasource configuration saved.")
    except BITestPilotError as exc:
        st.error(str(exc))


def _render_explorer(ctx: AppContext, cfg: DatasourceConfig) -> None:
    """Optional: list datasets and preview one (only after a successful test)."""
    if not cfg.last_test_ok:
        return
    theme.section("Explore datasource")
    if st.button("List tables / sheets"):
        try:
            st.session_state["_ds_datasets"] = ctx.datasource_service.list_datasets(cfg)
        except BITestPilotError as exc:
            st.error(str(exc))

    datasets = st.session_state.get("_ds_datasets") or []
    if datasets:
        chosen = st.selectbox("Dataset", datasets)
        if st.button("Preview (top 50 rows)"):
            result = ctx.datasource_service.preview(cfg, chosen, sample_rows=50)
            if result.error:
                st.error(result.error)
            elif result.columns:
                import pandas as pd

                st.dataframe(
                    pd.DataFrame(result.sample_rows, columns=result.columns),
                    use_container_width=True,
                )
                st.caption(f"{result.row_count} row(s) previewed.")
            else:
                st.info("No rows returned.")


def _render_schema(ctx: AppContext, project) -> None:
    """Read + display the datasource schema (tables/columns/PK/FK)."""
    import pandas as pd

    theme.section("Database schema")
    st.caption(
        "Deterministically read tables, columns, primary keys and foreign keys. "
        "This schema feeds the AI semantic mapping used for SQL validation."
    )
    if st.button("🗂️ Read database schema"):
        cfg = ctx.datasource_service.load(project)
        try:
            with st.spinner("Reading schema…"):
                ctx.schema_service.read_schema(project, cfg)
            st.success("Schema read and saved.")
        except BITestPilotError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface driver errors
            st.error(f"Could not read schema: {exc}")

    schema = ctx.schema_service.load_schema(project)
    if not schema:
        st.caption("No schema read yet.")
        return

    counts = schema.summary_counts()
    c = st.columns(4)
    c[0].metric("Tables", counts["tables"])
    c[1].metric("Columns", counts["columns"])
    c[2].metric("Primary keys", counts["primary_keys"])
    c[3].metric("Foreign keys", counts["foreign_keys"])

    if not schema.tables:
        return
    names = [t.full_name for t in schema.tables]
    chosen = st.selectbox("Inspect table", names)
    table = next(t for t in schema.tables if t.full_name == chosen)
    st.dataframe(pd.DataFrame([{
        "Column": col.name, "Type": col.data_type,
        "Nullable": "Yes" if col.nullable else "No",
        "PK": "🔑" if col.is_primary_key else "",
    } for col in table.columns]), use_container_width=True, hide_index=True)
    if table.foreign_keys:
        st.markdown("**Foreign keys**")
        st.dataframe(pd.DataFrame([{
            "Column": fk.column, "References": f"{fk.ref_table}.{fk.ref_column}",
        } for fk in table.foreign_keys]), use_container_width=True, hide_index=True)


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        theme.app_header()
        theme.section("Datasource")
        st.warning("No active project. Open a project in **Project Manager** first.")
        return

    theme.app_header()
    theme.section(f"Datasource · {project.name}")

    cfg = ctx.datasource_service.load(project)

    type_options = DatasourceType.values()
    type_index = type_options.index(cfg.type.value) if cfg.is_configured else 0
    chosen_type = st.selectbox("Datasource type", type_options, index=type_index)

    if chosen_type == DatasourceType.SQL_SERVER.value:
        _last_status(cfg)
        _sql_server_form(ctx, project, cfg)
        _render_explorer(ctx, cfg)
        if cfg.is_configured:
            _render_schema(ctx, project)
    else:
        # Excel is fully automatic: upload → auto-read → select sheet. No manual
        # path, no Save button, no separate explorer.
        _excel_flow(ctx, project, cfg)
        if ctx.datasource_service.load(project).is_configured:
            _render_schema(ctx, project)
