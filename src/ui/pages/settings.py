"""Settings page — machine-wide application configuration.

Edits the global ``app_config.json``: default LLM provider, optional
machine-level API keys (per-project settings always take precedence), projects
root folder, and theme preference.
"""

from __future__ import annotations

import streamlit as st

from src.core.config import save_config
from src.services.llm.factory import supported_providers
from src.ui import theme
from src.ui.state import KEY_CONTEXT, AppContext


def render(ctx: AppContext) -> None:
    theme.app_header()
    theme.section("Settings")
    st.caption(
        "Machine-wide defaults. API keys are stored locally in app_config.json; "
        "per-project LLM settings always override these."
    )

    cfg = ctx.config
    providers = supported_providers()

    with st.form("app_settings"):
        st.markdown("#### General")
        prov_index = providers.index(cfg.default_llm_provider) if cfg.default_llm_provider in providers else 0
        default_provider = st.selectbox("Default LLM provider", providers, index=prov_index)
        theme_choice = st.selectbox(
            "Theme preference", ["light", "dark"],
            index=0 if cfg.theme != "dark" else 1,
        )
        projects_root = st.text_input("Projects root folder", value=cfg.projects_root)

        st.markdown("#### Default API keys (optional)")
        st.caption("Used only when a project has no key of its own.")
        keys: dict[str, str] = {}
        for prov in providers:
            keys[prov] = st.text_input(
                f"{prov} API key", value=cfg.default_api_keys.get(prov, ""),
                type="password", key=f"key_{prov}",
            )

        if st.form_submit_button("💾 Save settings", type="primary"):
            root_changed = projects_root.strip() != cfg.projects_root
            cfg.default_llm_provider = default_provider
            cfg.theme = theme_choice
            cfg.projects_root = projects_root.strip() or cfg.projects_root
            cfg.default_api_keys = {p: k for p, k in keys.items() if k.strip()}
            save_config(cfg)
            if root_changed:
                # Rebuild the app context so the repository points at the new root.
                st.session_state.pop(KEY_CONTEXT, None)
            st.success("Settings saved.")
            st.rerun()

    theme.section("About")
    st.markdown(
        "- **Storage**: all data lives in local project folders under the projects root.\n"
        "- **Architecture**: Python performs all deterministic work (parsing, datasource "
        "access, comparison, validation); the LLM only reasons over the assembled context.\n"
        "- **Providers**: bring your own API keys; Grok is fully supported, with more "
        "providers pluggable via the abstraction layer."
    )
    st.caption("BI TestPilot AI · v0.1.0 · Local · No cloud · No authentication")
