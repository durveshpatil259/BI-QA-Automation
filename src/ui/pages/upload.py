"""Dashboard & Screenshot Upload page.

Uploads dashboard files (validated against the active project's platform) and
screenshots, lists/removes existing assets, and shows the automatically
determined analysis mode (Metadata / Visual / Complete).
"""

from __future__ import annotations

import streamlit as st

from src.core.constants import AnalysisMode
from src.core.exceptions import BITestPilotError
from src.domain.models import Project
from src.services.upload_service import AssetInfo, SaveResult
from src.ui import theme
from src.ui.state import AppContext, get_active_project


def _no_project_notice() -> None:
    theme.app_header()
    theme.section("Upload")
    st.warning(
        "No active project. Go to **Project Manager**, create or open a project, "
        "then return here to upload assets."
    )


def _mode_banner(mode: AnalysisMode | None) -> None:
    if mode is None:
        st.info(
            "**Analysis mode: not set** — upload a dashboard file and/or screenshots "
            "to determine it automatically."
        )
        return
    detail = {
        AnalysisMode.METADATA: "Dashboard file present, no screenshots → metadata-only analysis.",
        AnalysisMode.VISUAL: "Screenshots present, no dashboard file → visual-only analysis.",
        AnalysisMode.COMPLETE: "Dashboard file **and** screenshots present → full QA analysis.",
    }[mode]
    st.success(f"**Analysis mode: {mode}** — {detail}")


def _show_results(results: list[SaveResult]) -> None:
    saved = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    if saved:
        st.success(f"Saved {len(saved)} file(s): " + ", ".join(r.file_name for r in saved))
    for r in failed:
        st.error(f"{r.file_name}: {r.message}")


def _to_pairs(uploaded) -> list[tuple[str, bytes]]:
    return [(f.name, f.getvalue()) for f in (uploaded or [])]


def _render_dashboard_section(ctx: AppContext, project: Project) -> None:
    theme.section("Dashboard files")
    allowed = ctx.upload_service.allowed_dashboard_extensions(project)
    st.caption(
        f"Platform: **{project.bi_platform}** · allowed types: "
        f"{', '.join(allowed) if allowed else 'n/a'}"
    )

    with st.form("dashboard_upload", clear_on_submit=True):
        uploaded = st.file_uploader(
            "Add dashboard file(s)",
            type=[e.lstrip(".") for e in allowed] or None,
            accept_multiple_files=True,
        )
        if st.form_submit_button("Upload dashboard file(s)", type="primary"):
            if not uploaded:
                st.warning("Choose at least one file first.")
            else:
                try:
                    results = ctx.upload_service.save_dashboard_files(
                        project, _to_pairs(uploaded)
                    )
                    _show_results(results)
                except BITestPilotError as exc:
                    st.error(str(exc))
                st.rerun()

    _render_asset_list(
        ctx.upload_service.list_dashboard_files(project),
        on_remove=lambda name: ctx.upload_service.remove_dashboard_file(project, name),
        key_prefix="dash",
        empty_msg="No dashboard files uploaded yet.",
    )


def _render_screenshot_section(ctx: AppContext, project: Project) -> None:
    theme.section("Screenshots (optional)")
    allowed = ctx.upload_service.allowed_screenshot_extensions()
    st.caption(f"Allowed types: {', '.join(allowed)}")

    with st.form("screenshot_upload", clear_on_submit=True):
        uploaded = st.file_uploader(
            "Add screenshot(s)",
            type=[e.lstrip(".") for e in allowed],
            accept_multiple_files=True,
        )
        if st.form_submit_button("Upload screenshot(s)", type="primary"):
            if not uploaded:
                st.warning("Choose at least one image first.")
            else:
                try:
                    results = ctx.upload_service.save_screenshots(
                        project, _to_pairs(uploaded)
                    )
                    _show_results(results)
                except BITestPilotError as exc:
                    st.error(str(exc))
                st.rerun()

    screenshots = ctx.upload_service.list_screenshots(project)
    if screenshots:
        paths = ctx.projects.paths_for(project)
        cols = st.columns(4)
        for i, asset in enumerate(screenshots):
            with cols[i % 4]:
                theme.show_image(st, str(paths.screenshots_dir / asset.name))
                st.caption(f"{asset.name} · {asset.size_human}")
                if st.button("Remove", key=f"shot_rm_{asset.name}", use_container_width=True):
                    ctx.upload_service.remove_screenshot(project, asset.name)
                    st.rerun()
    else:
        st.caption("No screenshots uploaded yet.")


def _render_asset_list(
    assets: list[AssetInfo], on_remove, key_prefix: str, empty_msg: str
) -> None:
    if not assets:
        st.caption(empty_msg)
        return
    for asset in assets:
        c1, c2, c3 = st.columns([6, 2, 2])
        c1.write(f"📄 {asset.name}")
        c2.caption(asset.size_human)
        if c3.button("Remove", key=f"{key_prefix}_rm_{asset.name}", use_container_width=True):
            on_remove(asset.name)
            st.rerun()


def render(ctx: AppContext) -> None:
    project = get_active_project()
    if project is None:
        _no_project_notice()
        return

    theme.app_header()
    theme.section(f"Upload · {project.name}")
    _mode_banner(project.analysis_mode)

    left, right = st.columns(2)
    with left:
        _render_dashboard_section(ctx, project)
    with right:
        _render_screenshot_section(ctx, project)
