"""Placeholder view for modules not yet built.

Each navigation entry maps to a real page as its module ships. Until then this
renders an honest "coming next" notice so the app is fully runnable at every
stage of the incremental build.
"""

from __future__ import annotations

import streamlit as st

from src.ui import theme
from src.ui.state import AppContext


def make(title: str, description: str, build_step: str):
    def render(ctx: AppContext) -> None:  # noqa: ARG001 - uniform signature
        theme.app_header()
        theme.section(title)
        st.info(f"**{title}** — {description}")
        st.caption(f"Planned build step: {build_step}")
    return render
