"""Render the HTML report to PDF.

Two engines, tried in order, because neither is universally available:

1. **WeasyPrint** — best CSS fidelity, but on Windows it needs the **GTK3
   native runtime** (``libgobject-2.0-0``), which pip cannot install. When GTK
   is missing the import raises ``OSError`` at call time, not import time.
2. **xhtml2pdf** — pure Python, no native dependencies, works everywhere. CSS
   support is more limited, so the report degrades in styling, not content.

The caller gets a PDF whenever either engine works, and a clear, actionable
error only when both are unavailable.
"""

from __future__ import annotations

import io
import re

from src.core.exceptions import BITestPilotError
from src.core.logger import get_logger

_logger = get_logger()


class PdfRenderError(BITestPilotError):
    """Raised when no PDF engine is usable."""


def _try_weasyprint(html: str) -> bytes | None:
    try:
        from weasyprint import HTML
    except ImportError:
        return None
    except OSError as exc:            # GTK runtime missing (typical on Windows)
        _logger.info("WeasyPrint unavailable (%s); falling back.", exc)
        return None
    try:
        return HTML(string=html).write_pdf()
    except OSError as exc:
        _logger.info("WeasyPrint failed (%s); falling back.", exc)
        return None


_ROOT_BLOCK = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
_VAR_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_VAR_USE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*?)\s*)?\)")


def inline_css_variables(html: str) -> str:
    """Substitute ``var(--x)`` with its literal value.

    xhtml2pdf predates CSS custom properties and raises on ``var(...)``. The
    HTML template keeps variables (the browser version needs them for theming),
    so they are resolved here only for the PDF path.
    """
    values: dict[str, str] = {}
    for block in _ROOT_BLOCK.findall(html):
        for name, value in _VAR_DECL.findall(block):
            values[name] = value.strip()

    def replace(match: re.Match) -> str:
        name, fallback = match.group(1), match.group(2)
        return values.get(name, (fallback or "").strip() or "inherit")

    # Repeat so variables defined in terms of other variables resolve.
    for _ in range(3):
        html, count = _VAR_USE.subn(replace, html)
        if not count:
            break
    return html


def _try_xhtml2pdf(html: str) -> bytes | None:
    try:
        from xhtml2pdf import pisa
    except ImportError:
        return None
    buffer = io.BytesIO()
    try:
        result = pisa.CreatePDF(inline_css_variables(html), dest=buffer)
    except Exception as exc:  # noqa: BLE001 - unsupported CSS should not 500
        _logger.warning("xhtml2pdf failed: %s", exc)
        return None
    if result.err:
        _logger.warning("xhtml2pdf reported %s error(s).", result.err)
        return None
    return buffer.getvalue()


def render_pdf(html: str) -> tuple[bytes, str]:
    """Return ``(pdf_bytes, engine_name)``."""
    for name, engine in (("weasyprint", _try_weasyprint), ("xhtml2pdf", _try_xhtml2pdf)):
        pdf = engine(html)
        if pdf:
            _logger.info("PDF rendered with %s (%d bytes)", name, len(pdf))
            return pdf, name

    raise PdfRenderError(
        "No PDF engine available. Install either:\n"
        "  • xhtml2pdf  — pip install xhtml2pdf   (pure Python, works anywhere)\n"
        "  • WeasyPrint — pip install weasyprint  (better styling; on Windows "
        "also install the GTK3 runtime)"
    )


def pdf_engine_available() -> str | None:
    """Name of the engine that would be used, or None."""
    try:
        from weasyprint import HTML  # noqa: F401
        return "weasyprint"
    except Exception:  # noqa: BLE001 - ImportError or missing GTK
        pass
    try:
        from xhtml2pdf import pisa  # noqa: F401
        return "xhtml2pdf"
    except ImportError:
        return None
