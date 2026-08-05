"""Shared UI styling and small presentation helpers.

Keeps all CSS and reusable render helpers (headers, metric cards, status
badges) in one place so every page has a consistent, professional look.
"""

from __future__ import annotations

import streamlit as st

from src.core.constants import (
    AnalysisStatus,
    Priority,
    Severity,
    TestStatus,
)

_CSS = """
<style>
/* --- layout polish --- */
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px; }

/* --- app header --- */
.bt-header { display:flex; align-items:center; gap:.75rem; margin-bottom:.25rem; }
.bt-header h1 { font-size:1.6rem; margin:0; font-weight:700; }
.bt-tagline { color:#6b7280; font-size:.9rem; margin:0 0 1.25rem 0; }

/* --- section headings --- */
.bt-section { font-size:1.15rem; font-weight:600; margin:1.25rem 0 .5rem 0;
              padding-bottom:.35rem; border-bottom:1px solid #e5e7eb; }

/* --- cards --- */
.bt-card { border:1px solid #e5e7eb; border-radius:12px; padding:1rem 1.15rem;
           background:#ffffff; box-shadow:0 1px 2px rgba(0,0,0,.04); height:100%; }
.bt-card h4 { margin:0 0 .35rem 0; font-size:1.05rem; }
.bt-card p  { margin:.15rem 0; color:#4b5563; font-size:.85rem; }

/* --- badges --- */
.bt-badge { display:inline-block; padding:.15rem .55rem; border-radius:999px;
            font-size:.72rem; font-weight:600; letter-spacing:.02em; }
.bt-b-green  { background:#dcfce7; color:#166534; }
.bt-b-red    { background:#fee2e2; color:#991b1b; }
.bt-b-amber  { background:#fef3c7; color:#92400e; }
.bt-b-blue   { background:#dbeafe; color:#1e40af; }
.bt-b-gray   { background:#f3f4f6; color:#374151; }
</style>
"""


def inject_css() -> None:
    """Inject the application's shared CSS once per page render."""
    st.markdown(_CSS, unsafe_allow_html=True)


def app_header(icon: str = "🧭") -> None:
    from src.core.constants import APP_NAME, APP_TAGLINE

    st.markdown(
        f'<div class="bt-header">{icon}<h1>{APP_NAME}</h1></div>'
        f'<p class="bt-tagline">{APP_TAGLINE}</p>',
        unsafe_allow_html=True,
    )


def section(title: str) -> None:
    st.markdown(f'<div class="bt-section">{title}</div>', unsafe_allow_html=True)


def show_image(target, image, **kwargs) -> None:
    """Render an image full-width across Streamlit versions.

    Newer Streamlit uses ``use_container_width`` on ``st.image``; older versions
    only accept ``use_column_width``. Try the new arg, fall back to the old one,
    then to no width arg — so the app never crashes on a version mismatch.
    """
    renderer = getattr(target, "image", st.image)
    try:
        renderer(image, use_container_width=True, **kwargs)
    except TypeError:
        try:
            renderer(image, use_column_width=True, **kwargs)
        except TypeError:
            renderer(image, **kwargs)


# --- badge helpers ---------------------------------------------------------
_BADGE_CLASS = {
    "green": "bt-b-green", "red": "bt-b-red", "amber": "bt-b-amber",
    "blue": "bt-b-blue", "gray": "bt-b-gray",
}


def badge(text: str, color: str = "gray") -> str:
    cls = _BADGE_CLASS.get(color, "bt-b-gray")
    return f'<span class="bt-badge {cls}">{text}</span>'


def status_badge(status: AnalysisStatus) -> str:
    mapping = {
        AnalysisStatus.COMPLETED: "green",
        AnalysisStatus.RUNNING: "blue",
        AnalysisStatus.FAILED: "red",
        AnalysisStatus.NOT_STARTED: "gray",
    }
    return badge(str(status), mapping.get(status, "gray"))


def severity_color(severity: Severity) -> str:
    return {
        Severity.INFO: "blue",
        Severity.WARNING: "amber",
        Severity.ERROR: "red",
        Severity.CRITICAL: "red",
    }.get(severity, "gray")


def test_status_color(status: TestStatus) -> str:
    return {
        TestStatus.PASS: "green",
        TestStatus.FAIL: "red",
        TestStatus.BLOCKED: "amber",
        TestStatus.NOT_EXECUTED: "gray",
    }.get(status, "gray")


def priority_color(priority: Priority) -> str:
    return {
        Priority.HIGH: "red",
        Priority.MEDIUM: "amber",
        Priority.LOW: "blue",
    }.get(priority, "gray")
