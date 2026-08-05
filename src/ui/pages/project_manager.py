"""Project Manager page.

Lets the user create projects, browse them as cards, open (activate) a project,
edit its details, and delete it. All operations go through
:class:`ProjectService`; this view holds no business rules.
"""

from __future__ import annotations

import streamlit as st

from src.core.constants import BIPlatform
from src.core.exceptions import BITestPilotError
from src.domain.models import Project
from src.ui import theme
from src.ui.state import (
    AppContext,
    get_active_project_id,
    set_active_project,
)

_PLATFORMS = BIPlatform.values()


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
@st.dialog("Create new project")
def _create_dialog(ctx: AppContext) -> None:
    name = st.text_input("Project name *", placeholder="e.g. Sales Performance QA")
    platform = st.selectbox("BI platform *", _PLATFORMS, index=0)
    description = st.text_area(
        "Description", placeholder="What does this dashboard cover?", height=100
    )
    if st.button("Create project", type="primary", use_container_width=True):
        try:
            project = ctx.project_service.create_project(
                name=name, bi_platform=platform, description=description
            )
        except BITestPilotError as exc:
            st.error(str(exc))
            return
        set_active_project(project.id)
        st.success(f"Created '{project.name}' and set it as active.")
        st.rerun()


@st.dialog("Edit project")
def _edit_dialog(ctx: AppContext, project: Project) -> None:
    name = st.text_input("Project name *", value=project.name)
    platform = st.selectbox(
        "BI platform *", _PLATFORMS, index=_PLATFORMS.index(project.bi_platform.value)
    )
    description = st.text_area("Description", value=project.description, height=100)
    if st.button("Save changes", type="primary", use_container_width=True):
        try:
            ctx.project_service.update_project(
                project.id, name=name, description=description, bi_platform=platform
            )
        except BITestPilotError as exc:
            st.error(str(exc))
            return
        st.success("Project updated.")
        st.rerun()


@st.dialog("Delete project")
def _delete_dialog(ctx: AppContext, project: Project) -> None:
    st.warning(
        f"This permanently deletes **{project.name}** and everything inside its "
        "folder (uploads, metadata, reports). This cannot be undone."
    )
    confirm = st.text_input("Type the project name to confirm")
    if st.button("Delete permanently", type="primary", use_container_width=True):
        if confirm.strip() != project.name:
            st.error("The name you typed does not match.")
            return
        try:
            ctx.project_service.delete_project(project.id)
        except BITestPilotError as exc:
            st.error(str(exc))
            return
        if get_active_project_id() == project.id:
            set_active_project(None)
        st.success(f"Deleted '{project.name}'.")
        st.rerun()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render_card(ctx: AppContext, project: Project) -> None:
    is_active = get_active_project_id() == project.id
    counts = (
        f"{len(project.dashboard_files)} dashboard file(s) · "
        f"{len(project.screenshot_files)} screenshot(s)"
    )
    active_tag = theme.badge("ACTIVE", "green") if is_active else ""

    with st.container(border=True):
        st.markdown(
            f"#### {project.name} &nbsp; {active_tag}", unsafe_allow_html=True
        )
        st.markdown(
            f"{theme.badge(str(project.bi_platform), 'blue')} &nbsp; "
            f"{theme.status_badge(project.status)}",
            unsafe_allow_html=True,
        )
        st.caption(project.description or "_No description_")
        st.caption(
            f"{counts}\n\nUpdated {project.updated_at:%Y-%m-%d %H:%M} · "
            f"Created {project.created_at:%Y-%m-%d}"
        )

        b1, b2, b3 = st.columns(3)
        if b1.button(
            "Open" if not is_active else "Active",
            key=f"open_{project.id}",
            disabled=is_active,
            use_container_width=True,
        ):
            set_active_project(project.id)
            st.rerun()
        if b2.button("Edit", key=f"edit_{project.id}", use_container_width=True):
            _edit_dialog(ctx, project)
        if b3.button("Delete", key=f"del_{project.id}", use_container_width=True):
            _delete_dialog(ctx, project)


def render(ctx: AppContext) -> None:
    theme.app_header()

    top_left, top_right = st.columns([3, 1])
    with top_left:
        theme.section("Projects")
    with top_right:
        st.write("")  # vertical alignment
        if st.button("➕ New project", type="primary", use_container_width=True):
            _create_dialog(ctx)

    projects = ctx.project_service.list_projects()
    if not projects:
        st.info("No projects yet. Click **New project** to create your first one.")
        return

    query = st.text_input(
        "Search", placeholder="Filter by name or platform…", label_visibility="collapsed"
    )
    if query:
        q = query.strip().casefold()
        projects = [
            p for p in projects
            if q in p.name.casefold() or q in str(p.bi_platform).casefold()
        ]
        if not projects:
            st.warning("No projects match your search.")
            return

    for row_start in range(0, len(projects), 3):
        cols = st.columns(3)
        for col, project in zip(cols, projects[row_start : row_start + 3]):
            with col:
                _render_card(ctx, project)
